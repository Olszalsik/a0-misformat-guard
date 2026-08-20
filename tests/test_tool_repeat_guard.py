"""Tests for the v0.5.0 tool-repeat guard.

The guard is a ``tool_execute_after`` hook that detects the agent
re-emitting the SAME tool call with the SAME args and getting an error
each time -- the "reasoning death-loop" the framework's misformat/repeat
breakers do NOT catch (those only fire on warnings; a well-formed-but-
failing tool call emits no warning). The hook tracks a streak of
identical (tool, args) error results in ``loop_data.params_persistent``,
warns at ``warn_threshold`` (system_warning + inline tag) and hard-stops
at ``stop_threshold`` (``break_loop``).

These tests mock the agent / loop_data / response so the hook can be
exercised without the real framework. They mirror the style of
``test_cascade_utility_repair.py``: monkeypatch ``misformat_config.get_config``
to return a fixed dict, instantiate the ``Extension`` subclass directly
with ``agent=agent``, and ``await ext.execute(response=..., tool_name=...)``.
The hook NEVER raises; several tests assert the no-op paths.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# Anchor the repo root (install root /a0 in the container; override via
# REPO_ROOT_OVERRIDE when running on the host).
REPO_ROOT = Path("/a0")
_override = os.environ.get("REPO_ROOT_OVERRIDE")
if _override:
    REPO_ROOT = Path(_override)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from usr.plugins.misformat_guard.api import misformat_config  # noqa: E402
from usr.plugins.misformat_guard.api import tool_repeat  # noqa: E402
from usr.plugins.misformat_guard.extensions.python.tool_execute_after import (  # noqa: E402
    _30_detect_repeat_failures as hook_mod,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _response(message: str, break_loop: bool = False):
    """A mutable stand-in for helpers.tool.Response. The hook reads/writes
    .message and .break_loop via attribute access, so a SimpleNamespace
    is sufficient and lets tests assert the mutations."""
    return SimpleNamespace(message=message, break_loop=break_loop, additional=None)


class _FakeLoopData:
    def __init__(self, params_persistent: dict | None = None, current_tool=None):
        self.params_persistent = params_persistent if params_persistent is not None else {}
        self.current_tool = current_tool  # a SimpleNamespace(.args) or None
        self.iteration = 0


class _FakeAgent:
    def __init__(self, args: dict | None = None, current_tool=None,
                 params_persistent: dict | None = None):
        if current_tool is None and args is not None:
            current_tool = SimpleNamespace(args=args)
        self.loop_data = _FakeLoopData(
            params_persistent=params_persistent, current_tool=current_tool
        )
        self.warnings: list[str] = []  # recorded hist_add_warning messages

    def hist_add_warning(self, message=None, id="", **kwargs):
        self.warnings.append(message)
        return SimpleNamespace(id="w" + str(len(self.warnings)))

    def get_data(self, key, default=None):
        return default

    def set_data(self, key, value):
        return None


def _make_extension(agent: _FakeAgent):
    return hook_mod.DetectRepeatFailures(agent=agent)


def _base_cfg(**overrides) -> dict:
    """Build a config dict from the plugin's default_config.yaml, then
    force the plugin + guard on and apply per-test overrides.

    Deep-copies the cached default: ``_load_default_from_disk`` returns the
    SAME dict object on every call (it caches in ``_DEFAULT_CACHE``), so
    mutating it in place here would leak per-test overrides (e.g.
    ``tool_repeat_action="stop"`` or ``tool_repeat_warn_threshold=0``)
    into every later test via the cache -- a real test-order pollution
    that silently disabled the warn branch in default-config tests."""
    base = copy.deepcopy(misformat_config._load_default_from_disk())
    base["enabled"] = True
    base["tool_repeat_guard_enabled"] = True
    for k, v in overrides.items():
        base[k] = v
    return base


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    """Force misformat_config.get_config to return a mutable per-test cfg."""
    holder = {"cfg": _base_cfg()}

    def _get(agent=None):
        return holder["cfg"]

    monkeypatch.setattr(misformat_config, "get_config", _get)
    yield holder


def _set(holder, **overrides):
    holder["cfg"] = _base_cfg(**overrides)


# A realistic text_editor patch error (from fw.text_editor.patch_error.md:
# "error patching {{path}}: {{error}}"). The default error_patterns match
# both "^error\\b" and "old_text not found".
PATCH_ERROR = "error patching /a0/src/foo.py: old_text not found"
PATCH_OK = "/a0/src/foo.py patched 3 edits applied."
OTHER_ERROR = "Error: connection refused while fetching resource"


async def _call(ext, agent, tool_name="code_editor", args=None, message=PATCH_ERROR):
    """Drive one tool_execute_after invocation and return the response.

    Only refreshes ``current_tool`` when ``args`` is provided, so a test
    that wants ``current_tool=None`` (parallel-race path) creates the
    agent with ``args=None`` and omits ``args`` here."""
    if args is not None:
        agent.loop_data.current_tool = SimpleNamespace(args=args)
    resp = _response(message)
    await ext.execute(response=resp, tool_name=tool_name)
    return resp


def _state(agent) -> dict:
    return agent.loop_data.params_persistent.get(tool_repeat.STATE_KEY, {})


# ---------------------------------------------------------------------------
# No-op / gating paths (the hook must NEVER raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_when_plugin_disabled(_patch_config):
    _set(_patch_config, enabled=False)
    agent = _FakeAgent(args={"path": "x", "old_text": "a", "new_text": "b"})
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    assert resp.message == PATCH_ERROR
    assert resp.break_loop is False
    assert agent.warnings == []
    assert tool_repeat.STATE_KEY not in agent.loop_data.params_persistent


@pytest.mark.asyncio
async def test_noop_when_guard_disabled(_patch_config):
    _set(_patch_config, tool_repeat_guard_enabled=False)
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    assert resp.message == PATCH_ERROR and resp.break_loop is False
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_noop_when_current_tool_none(_patch_config):
    """Parallel-tool race / already-cleared current_tool -> no-op (safe miss)."""
    agent = _FakeAgent(args=None)
    agent.loop_data.current_tool = None  # simulate the race
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    assert resp.message == PATCH_ERROR and resp.break_loop is False
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_noop_for_ignored_tools(_patch_config):
    """The final-answer tool (response / response_tool) is never tracked."""
    agent = _FakeAgent(args={"text": "final answer"})
    ext = _make_extension(agent)
    for name in ("response", "response_tool"):
        agent.warnings.clear()
        resp = await _call(ext, agent, tool_name=name, message="Error: bad answer")
        assert resp.break_loop is False
        assert agent.warnings == []


@pytest.mark.asyncio
async def test_noop_when_message_not_string(_patch_config):
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    resp = SimpleNamespace(message=None, break_loop=False, additional=None)
    await ext.execute(response=resp, tool_name="code_editor")
    assert resp.break_loop is False
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_never_raises_on_garbage_response(_patch_config):
    """A response that is not even a SimpleNamespace must not crash."""
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    # response=None -> early return (no crash)
    await ext.execute(response=None, tool_name="code_editor")
    # response is a bare int -> getattr(response,"message",None) is None -> return
    await ext.execute(response=12345, tool_name="code_editor")


@pytest.mark.asyncio
async def test_unusable_params_persistent_no_crash(_patch_config):
    """If params_persistent is not a dict, load_state returns None -> no-op."""
    agent = _FakeAgent(args={"path": "x"}, params_persistent=None)
    agent.loop_data.params_persistent = "not-a-dict"  # type: ignore[assignment]
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    assert resp.message == PATCH_ERROR and resp.break_loop is False
    assert agent.warnings == []


# ---------------------------------------------------------------------------
# Streak accumulation + reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_resets_streak(_patch_config):
    """A non-error result is progress -> streak resets to count 0."""
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    # First, accumulate one error.
    await _call(ext, agent, args={"path": "x"}, message=PATCH_ERROR)
    assert _state(agent).get("count") == 1
    # Now a success with the same sig -> reset to 0.
    await _call(ext, agent, args={"path": "x"}, message=PATCH_OK)
    assert _state(agent).get("count") == 0
    assert _state(agent).get("warned") is False
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_first_error_starts_streak_at_1_no_warn(_patch_config):
    """count=1 is below warn_threshold=2 -> no warning, no stop."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    assert resp.message == PATCH_ERROR and resp.break_loop is False
    assert _state(agent).get("count") == 1
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_same_sig_error_accumulates(_patch_config):
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    await _call(ext, agent, args=a, message=PATCH_ERROR)
    await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert _state(agent).get("count") == 2


@pytest.mark.asyncio
async def test_different_args_resets_streak(_patch_config):
    """Different args -> different signature -> new streak at count 1."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    await _call(ext, agent, args={"path": "x", "old_text": "a"}, message=PATCH_ERROR)
    assert _state(agent).get("count") == 1
    # Different old_text -> new streak.
    await _call(ext, agent, args={"path": "x", "old_text": "DIFFERENT"}, message=PATCH_ERROR)
    assert _state(agent).get("count") == 1
    assert _state(agent).get("warned") is False


@pytest.mark.asyncio
async def test_different_tool_resets_streak(_patch_config):
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    await _call(ext, agent, tool_name="code_editor", args={"path": "x"}, message=PATCH_ERROR)
    assert _state(agent).get("count") == 1
    await _call(ext, agent, tool_name="terminal", args={"path": "x"}, message=OTHER_ERROR)
    # Different tool -> new streak at 1 (and last_tool updated).
    assert _state(agent).get("count") == 1
    assert _state(agent).get("last_tool") == "terminal"


@pytest.mark.asyncio
async def test_args_signature_key_order_invariant(_patch_config):
    """Reordered dict keys serialize to the same signature (sort_keys)."""
    agent = _FakeAgent(args={})
    ext = _make_extension(agent)
    a1 = {"path": "x", "old_text": "a", "new_text": "b"}
    a2 = {"new_text": "b", "old_text": "a", "path": "x"}  # reordered
    sig1 = tool_repeat.args_signature("code_editor", a1)
    sig2 = tool_repeat.args_signature("code_editor", a2)
    assert sig1 == sig2
    # Drive two calls with reordered-key args -> streak counts them as the same sig.
    await _call(ext, agent, args=a1, message=PATCH_ERROR)
    await _call(ext, agent, args=a2, message=PATCH_ERROR)
    assert _state(agent).get("count") == 2


# ---------------------------------------------------------------------------
# Warn + stop actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warn_fires_once_at_threshold(_patch_config):
    """At warn_threshold=2: inject a system_warning + tag the tool result
    inline. The warned flag means a 3rd identical error does NOT re-warn."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    # count=1 -> no warn
    r1 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r1.message == PATCH_ERROR
    assert agent.warnings == []
    # count=2 -> warn (inline tag + system_warning)
    r2 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert "[TOOL-REPEAT-GUARD]" in r2.message
    assert PATCH_ERROR in r2.message  # original error preserved below the tag
    assert len(agent.warnings) == 1
    assert _state(agent).get("warned") is True
    # count=3 -> no NEW warning (warned flag), still no stop (stop_threshold=4)
    r3 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert len(agent.warnings) == 1  # still one
    assert r3.break_loop is False


@pytest.mark.asyncio
async def test_stop_fires_at_stop_threshold(_patch_config):
    """At stop_threshold=4 the turn ends: break_loop=True, message rewritten
    to the stop explanation, and NO hist_add_warning is emitted."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    for _i in range(3):
        await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert agent.warnings == [] or len(agent.warnings) <= 1
    # 4th call -> hard stop
    r4 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r4.break_loop is True
    assert "[TOOL-REPEAT-GUARD:STOP]" in r4.message
    # Stop is the final response; no separate system_warning added.
    assert all("STOP" not in (w or "") for w in agent.warnings)


@pytest.mark.asyncio
async def test_warn_then_stop_sequence(_patch_config):
    """Full sequence: count 1 nothing, 2 warn, 3 nothing, 4 stop."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    r1 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r1.break_loop is False and agent.warnings == []
    r2 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert "[TOOL-REPEAT-GUARD]" in r2.message and not r2.break_loop
    assert len(agent.warnings) == 1
    r3 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r3.break_loop is False and len(agent.warnings) == 1
    r4 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r4.break_loop is True


@pytest.mark.asyncio
async def test_action_warn_only_never_stops(_patch_config):
    _set(_patch_config, tool_repeat_action="warn")
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    for _i in range(6):
        r = await _call(ext, agent, args=a, message=PATCH_ERROR)
        assert r.break_loop is False  # never stops
    # Warned once at count=2; no further warnings, no stop.
    assert len(agent.warnings) == 1


@pytest.mark.asyncio
async def test_action_stop_only_skips_warn(_patch_config):
    """action=stop: no soft warning phase; at stop_threshold=4 it stops."""
    _set(_patch_config, tool_repeat_action="stop")
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    for _i in range(3):
        r = await _call(ext, agent, args=a, message=PATCH_ERROR)
        assert r.break_loop is False and agent.warnings == []  # no warn phase
    r4 = await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert r4.break_loop is True
    assert agent.warnings == []  # stop path emits no system_warning


@pytest.mark.asyncio
async def test_stop_threshold_zero_disables_stop(_patch_config):
    """stop_threshold=0 disables the hard stop; warn still fires (0 must be
    respected, not coerced back to the default by an `or` fallback)."""
    _set(_patch_config, tool_repeat_stop_threshold=0)
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    for _i in range(6):
        r = await _call(ext, agent, args=a, message=PATCH_ERROR)
        assert r.break_loop is False  # never stops despite many repeats
    assert len(agent.warnings) == 1  # warned once at 2


@pytest.mark.asyncio
async def test_warn_threshold_zero_disables_warn(_patch_config):
    _set(_patch_config, tool_repeat_warn_threshold=0, tool_repeat_action="warn")
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    for _i in range(5):
        r = await _call(ext, agent, args=a, message=PATCH_ERROR)
        assert r.break_loop is False
    assert agent.warnings == []  # warn disabled


# ---------------------------------------------------------------------------
# Error classification + config knobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_patterns_catch_text_editor_error(_patch_config):
    assert tool_repeat.is_error_result(PATCH_ERROR, tool_repeat._DEFAULT_ERROR_PATTERNS)
    assert tool_repeat.is_error_result("Error: something failed", tool_repeat._DEFAULT_ERROR_PATTERNS)
    assert tool_repeat.is_error_result("Traceback (most recent call last):", tool_repeat._DEFAULT_ERROR_PATTERNS)


@pytest.mark.asyncio
async def test_success_message_not_matched(_patch_config):
    assert not tool_repeat.is_error_result(PATCH_OK, tool_repeat._DEFAULT_ERROR_PATTERNS)
    assert not tool_repeat.is_error_result("found 3 matches", tool_repeat._DEFAULT_ERROR_PATTERNS)
    assert not tool_repeat.is_error_result("", tool_repeat._DEFAULT_ERROR_PATTERNS)
    assert not tool_repeat.is_error_result(None, tool_repeat._DEFAULT_ERROR_PATTERNS)


@pytest.mark.asyncio
async def test_custom_error_patterns(_patch_config):
    """A tool that formats errors as 'MYERR: ...' is catchable via config,
    and with that custom set a default-pattern error is NOT caught (treated
    as success -> streak resets)."""
    _set(_patch_config, tool_repeat_error_patterns=["^MYERR"])
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    a = {"path": "x"}
    # MYERR matches the custom pattern -> counted as an error (streak 1).
    await _call(ext, agent, args=a, message="MYERR: widget jammed")
    assert _state(agent).get("count") == 1
    # PATCH_ERROR does NOT match ^MYERR -> treated as success -> streak resets to 0.
    await _call(ext, agent, args=a, message=PATCH_ERROR)
    assert _state(agent).get("count") == 0
    assert _state(agent).get("warned") is False


@pytest.mark.asyncio
async def test_custom_ignored_tools(_patch_config):
    _set(_patch_config, tool_repeat_ignored_tools=["my_poller"])
    agent = _FakeAgent(args={"k": "v"})
    ext = _make_extension(agent)
    r = await _call(ext, agent, tool_name="my_poller", args={"k": "v"}, message="error: timeout")
    assert r.break_loop is False and agent.warnings == []
    assert tool_repeat.STATE_KEY not in agent.loop_data.params_persistent


@pytest.mark.asyncio
async def test_normalize_args_whitespace_collapse(_patch_config):
    """normalize_args=True: trailing-whitespace variants collapse to one sig."""
    a1 = {"old_text": "line1\nline2\n"}  # trailing newline
    a2 = {"old_text": "line1\nline2"}     # no trailing newline
    assert tool_repeat.args_signature("t", a1, normalize=False) != tool_repeat.args_signature("t", a2, normalize=False)
    assert tool_repeat.args_signature("t", a1, normalize=True) == tool_repeat.args_signature("t", a2, normalize=True)


# ---------------------------------------------------------------------------
# Stale-module safety (the hook must never crash on a partial reload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_api_module_missing_args_signature_no_crash(_patch_config, monkeypatch):
    monkeypatch.delattr(tool_repeat, "args_signature", raising=False)
    agent = _FakeAgent(args={"path": "x"})
    ext = _make_extension(agent)
    resp = await _call(ext, agent, message=PATCH_ERROR)
    # Stale symbol -> no-op (no crash, no mutation, no warning).
    assert resp.message == PATCH_ERROR and resp.break_loop is False
    assert agent.warnings == []


@pytest.mark.asyncio
async def test_hist_add_warning_failure_is_swallowed(_patch_config, monkeypatch):
    """If hist_add_warning itself raises, the hook must still tag the
    inline message and not crash."""
    agent = _FakeAgent(args={"path": "x", "old_text": "a"})

    def _boom(*a, **kw):
        raise RuntimeError("history unavailable")

    agent.hist_add_warning = _boom  # type: ignore[assignment]
    ext = _make_extension(agent)
    a = {"path": "x", "old_text": "a"}
    await _call(ext, agent, args=a, message=PATCH_ERROR)  # count 1, no warn
    r2 = await _call(ext, agent, args=a, message=PATCH_ERROR)  # count 2 -> warn
    assert "[TOOL-REPEAT-GUARD]" in r2.message  # inline tag still applied
    assert r2.break_loop is False


# ---------------------------------------------------------------------------
# Registration / discovery (mirrors test_extension_resolution.py style)
# ---------------------------------------------------------------------------


def test_hook_directory_exists():
    p = (
        REPO_ROOT
        / "usr"
        / "plugins"
        / "misformat_guard"
        / "extensions"
        / "python"
        / "tool_execute_after"
    )
    assert p.is_dir(), f"tool_execute_after hook dir missing: {p}"
    files = list(p.glob("_*.py"))
    assert files, f"no _*.py hook file in {p}"
    content = files[0].read_text(encoding="utf-8")
    assert "class DetectRepeatFailures" in content


def test_hook_class_imports():
    assert hasattr(hook_mod, "DetectRepeatFailures")


def test_hook_sorts_after_mask_secrets():
    """The framework sorts hooks within an extension point by filename.
    Our _30_ prefix must sort after the framework's _10_mask_secrets so
    detection sees the final (masked) tool message."""
    our = "_30_detect_repeat_failures.py"
    theirs = "_10_mask_secrets.py"
    assert our > theirs, f"{our} must sort after {theirs}"


def test_api_module_exports_tool_repeat_symbols():
    assert callable(tool_repeat.args_signature)
    assert callable(tool_repeat.is_error_result)
    assert callable(tool_repeat.load_state)
    assert callable(tool_repeat.resolve_config)
    assert tool_repeat.STATE_KEY == "_misformat_guard_tool_repeat"


def test_resolve_config_respects_zero_thresholds():
    """The 0-falsy gotcha: thresholds of 0 must NOT be coerced to defaults."""
    rc = tool_repeat.resolve_config({"tool_repeat_stop_threshold": 0, "tool_repeat_warn_threshold": 0})
    assert rc["stop_threshold"] == 0
    assert rc["warn_threshold"] == 0
    # Defaults when absent:
    rc2 = tool_repeat.resolve_config({})
    assert rc2["warn_threshold"] == 2
    assert rc2["stop_threshold"] == 4
    assert rc2["action"] == "warn_then_stop"
    assert rc2["enabled"] is True
    assert rc2["guard_enabled"] is True


def test_resolve_config_rejects_unknown_action():
    rc = tool_repeat.resolve_config({"tool_repeat_action": "explode"})
    assert rc["action"] == "warn_then_stop"  # falls back to default