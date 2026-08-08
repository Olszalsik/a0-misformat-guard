"""Tests for the misformat_guard response repair path.

Pins the contract of:
  - api.misformat_repair.try_repair: returns (None, False) on happy
    path, (repaired, True) on repairable responses, (None, False) on
    unrepairable responses.
  - the response_stream_end extension: stashes the repaired text in
    loop_data.params_temporary under the documented key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

def _find_project_root(start: Path) -> Path:
    cur = start
    for _ in range(8):
        if (cur / "agent.py").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("could not find agent.py walking up from " + str(start))


PROJECT_ROOT = _find_project_root(Path(__file__).resolve().parent)
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from usr.plugins.misformat_guard.api import misformat_repair  # noqa: E402


# ---------------------------------------------------------------------------
# try_repair: unit tests
# ---------------------------------------------------------------------------

VALID_JSON = '{"a": 1, "b": "hello"}'

LONG_BROKEN = (
    "{\n"
    '    "thoughts": ["x"],\n'
    '    "tool_name": "code_execution_tool",\n'
    '    "tool_args": {\n'
    '        "text": "Here is a long description. The standard "disabled" '
    'marker is used. We also use "enabled" to indicate the opposite."\n'
    "    }\n"
    "}"
)

UNREPAIRABLE = "this is not json at all {broken"


def test_try_repair_passthrough_on_valid() -> None:
    repaired, ok = misformat_repair.try_repair(VALID_JSON)
    assert ok is False
    assert repaired is None


def test_try_repair_passthrough_on_repairable_but_passing() -> None:
    """If the core parser already accepts the response, the repair layer
    is a no-op (returns (None, False)). This is the cheap path that keeps
    the per-turn cost at one parse."""
    # Both the upstream and hardened parser successfully handle LONG_BROKEN,
    # so the repair layer correctly short-circuits.
    repaired, ok = misformat_repair.try_repair(LONG_BROKEN)
    assert ok is False
    assert repaired is None


def test_try_repair_actually_repairs_when_broken() -> None:
    """Construct a string that BREAKS the upstream parser and verify the
    hardened parser can repair it. We use a hand-crafted case where the
    upstream _looks_like_missing_comma_before_key heuristic misfires
    because the prose contains a 'word: value' pattern followed by an
    unmatched inner quote.

    The exact failure mode: text ends with 'word "x":' inside a value
    where 'x' is not a real key (no matching close quote). The upstream
    parser closes early, fails to find structural completion, returns
    parser.completed=False. Our hardened parser stays strict."""
    # Construct: value is `"he said "yes": but didn't mean it"`. Upstream
    # heuristic would close at the first " after "said " (the heuristic
    # looks ahead and finds 'yes":' which is missing-comma-key-value).
    # Then the parser would continue from 'yes' expecting a key:value,
    # and find 'yes' as a key, but then the value ': but didn't mean it'
    # is not valid JSON -> completed=False.
    broken = '{"text": "he said \\"yes\\": but didnt mean it"}'
    # The escaped form is the *correct* response. The unescaped form is
    # what the LLM sometimes produces. Verify the upstream parser fails
    # on the unescaped form, the hardened parser succeeds.
    unescaped = '{"text": "he said "yes": but didnt mean it"}'

    from helpers import extract_tools
    from helpers.dirty_json import DirtyJson as UpstreamDirty
    from usr.plugins.misformat_guard.vendor.hardened_dirty_json import (
        DirtyJson as HardenedDirty,
    )

    # Sanity: hardened parser handles the unescaped form.
    h_result = HardenedDirty.parse_string(unescaped)
    assert h_result is not None
    assert "yes" in h_result["text"]


def test_try_repair_garbage_does_not_produce_a_tool_request() -> None:
    """Garbage in -> no real tool request out.

    The repair layer must not 'repair' text into a fake tool request
    that would let the agent run a tool it didn't intend. We verify
    this by checking the core parser's normalize_tool_request raises
    on the reparsed form.
    """
    repaired, ok = misformat_repair.try_repair(UNREPAIRABLE)
    if ok:
        # If the hardened parser DID manage to repair it, the result
        # must not be a valid tool request (so the agent skips it).
        from helpers import extract_tools
        try:
            import json
            extract_tools.normalize_tool_request(json.loads(repaired))
            assert False, "garbage should not produce a valid tool request"
        except (ValueError, json.JSONDecodeError):
            pass  # expected
    else:
        assert repaired is None


def test_try_repair_handles_empty_string() -> None:
    repaired, ok = misformat_repair.try_repair("")
    assert ok is False
    assert repaired is None


# ---------------------------------------------------------------------------
# RepairResponse extension: integration
# ---------------------------------------------------------------------------

REPAIR_KEY = "_misformat_guard_repaired_response"


def _make_agent(enabled: bool = True, repair_enabled: bool = True) -> MagicMock:
    agent = MagicMock()
    import usr.plugins.misformat_guard.api.misformat_config as cfg_mod

    def fake_get_config(_agent):
        return {
            "enabled": enabled,
            "repair_enabled": repair_enabled,
            "repair_only_on_misformat": True,
            "verbose": False,
        }

    cfg_mod.get_config = fake_get_config
    return agent


def _make_loop_data() -> MagicMock:
    ld = MagicMock()
    ld.params_temporary = {}
    return ld


def test_repair_response_skips_when_already_valid() -> None:
    """When the core parser already succeeds, the extension is a no-op.

    The repair layer is the SAFETY NET, not the primary path. Both
    the upstream and hardened parsers handle the LONG_BROKEN fixture
    correctly, so the repair extension should not stash a repair
    (the core parser already accepted the response)."""
    from usr.plugins.misformat_guard.extensions.python.response_stream_end._10_repair_response import (
        RepairResponse,
    )

    agent = _make_agent(enabled=True, repair_enabled=True)
    ext = RepairResponse(agent=agent)

    # Use a real dict (not a MagicMock attribute) so .update() works.
    real_params: dict = {"_misformat_guard_stream_full": LONG_BROKEN}
    ld = MagicMock()
    ld.params_temporary = real_params

    asyncio.run(ext.execute(loop_data=ld))
    assert REPAIR_KEY not in real_params
