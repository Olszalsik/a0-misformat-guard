"""Tests for the v0.4.0 process_tools safety-net cascade.

Mocks the agent's process_tools and call_utility_model so the safety
net can be exercised without touching the real framework.

The safety net is an @extensible /end hook on Agent.process_tools. It
catches the misformat-else-branch (where process_tools returns None
because the chat-model response could not be parsed) and tries to
repair the buffered stream text with the utility model. If the
repair succeeds, the hook re-invokes process_tools with the repaired
text and substitutes the new result. If the repair fails, the
original None stands and the framework's existing misformat warning
fires.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# Repo/install root: derived from this file's location
# (<root>/usr/plugins/misformat_guard/tests/ -> 4 levels up), which works
# both inside the container (/a0) and on the host. REPO_ROOT_OVERRIDE
# still wins for non-standard layouts.
REPO_ROOT = Path(
    os.environ.get("REPO_ROOT_OVERRIDE") or Path(__file__).resolve().parents[4]
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from usr.plugins.misformat_guard.api import misformat_config  # noqa: E402
from usr.plugins.misformat_guard.extensions.python._functions.agent.Agent.process_tools.end import (  # noqa: E402
    _30_repair_via_utility_fallback as fallback_mod,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeLoopData:
    def __init__(self, params: dict | None = None):
        self.params_temporary = params if params is not None else {}
        self.params_persistent = {}


class _FakeAgent:
    def __init__(
        self,
        utility_response: str = "",
        utility_raises: Exception | None = None,
        process_tools_return=None,
    ):
        self.loop_data = _FakeLoopData()
        self.utility_response = utility_response
        self.utility_raises = utility_raises
        self.process_tools_return = process_tools_return
        self.utility_calls: list[tuple[str, str]] = []
        self.process_tools_invocations: list[str] = []

    async def call_utility_model(self, system: str, message: str, background: bool = False):
        self.utility_calls.append((system, message))
        if self.utility_raises is not None:
            raise self.utility_raises
        return self.utility_response

    async def process_tools(self, msg: str):
        self.process_tools_invocations.append(msg)
        return self.process_tools_return


def _make_extension(agent: _FakeAgent):
    return fallback_mod.ProcessToolsFallback(agent=agent)


VALID_REPAIR = json.dumps(
    {"tool": "response_tool", "tool_args": {"text": "All done."}}
)

# A misformatted response with a missing `tool` key (the case where
# is_misformat returns True).
BROKEN_TEXT = '{"result": "just some prose, no tool call"}'


def _config_with(**overrides):
    base = misformat_config._load_default_from_disk()
    base["enabled"] = True
    base["primary_cascade_enabled"] = True
    base["process_tools_fallback"] = True
    cascade = {
        "mode": "utility_repair",
        "trigger": 1,
        "max_per_streak": 2,
        "max_total_per_chat": 6,
        "timeout_s": 30,
        "system_prompt_path": "prompts/utility_repair.md",
    }
    base["cascade"] = cascade
    for k, v in overrides.items():
        base[k] = v
    return base


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    holder = {"cfg": _config_with()}

    def _get(agent=None):
        return holder["cfg"]

    monkeypatch.setattr(misformat_config, "get_config", _get)
    yield holder


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_no_repair_needed(_patch_config):
    """If process_tools returned non-None, the safety net must no-op.
    (v0.5.2 regression: data['args'] uses the REAL @extensible shape --
    (agent_self, msg), slot 0 is `self`.)"""
    agent = _FakeAgent(process_tools_return="some tool message")
    ext = _make_extension(agent)
    data = {"result": "some tool message", "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"] == "some tool message"


@pytest.mark.asyncio
async def test_exception_path_no_repair(_patch_config):
    """If process_tools raised, the safety net must not interfere -- the
    framework's own exception handler will deal with it."""
    agent = _FakeAgent()
    ext = _make_extension(agent)
    err = RuntimeError("boom")
    data = {"result": None, "exception": err, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["exception"] is err
    assert data["result"] is None


@pytest.mark.asyncio
async def test_no_msg_and_no_buffer_no_repair(_patch_config):
    """With no str in the call args AND no stream buffer, there is no
    repair input -- the safety net must no-op."""
    agent = _FakeAgent(utility_response=VALID_REPAIR)
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(),), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"] is None  # original None stands


@pytest.mark.asyncio
async def test_misformat_triggers_repair(_patch_config):
    """The misformat-else-branch with buffered text must trigger the
    utility model and substitute a successful re-invocation."""
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 1, "utility model must be called"
    assert len(agent.process_tools_invocations) == 1, "process_tools must be re-invoked"
    assert agent.process_tools_invocations[0] == VALID_REPAIR, (
        "process_tools must be re-invoked with the repaired text"
    )
    assert data["result"] == "OK", "re-invocation result must be substituted"


@pytest.mark.asyncio
async def test_utility_returns_bad_text_no_substitution(_patch_config):
    """If the utility model returns something the framework cannot parse,
    the safety net must leave data['result'] as None so the framework's
    misformat warning still fires."""
    agent = _FakeAgent(utility_response="not json at all", process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 1
    assert len(agent.process_tools_invocations) == 0
    assert data["result"] is None, (
        "if utility returns unparseable text, original None must stand"
    )


@pytest.mark.asyncio
async def test_re_invocation_returning_none_no_substitution(_patch_config):
    """If the utility returns a parseable repair but re-invoking
    process_tools still returns None, the safety net must NOT substitute
    None. The original None stands."""
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return=None)
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"] is None, (
        "if re-invocation returns None, original None must stand -- never "
        "substitute a None for a None"
    )


@pytest.mark.asyncio
async def test_re_invocation_raises_no_substitution(_patch_config):
    """If re-invoking process_tools raises, the safety net must not
    propagate the exception -- it must leave data['result'] as None."""
    agent = _FakeAgent(utility_response=VALID_REPAIR)

    async def _raise(msg: str):
        agent.process_tools_invocations.append(msg)
        raise RuntimeError("re-invocation crashed")

    agent.process_tools = _raise  # type: ignore[assignment]
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)  # must not raise
    assert data["result"] is None


@pytest.mark.asyncio
async def test_per_streak_cap_blocks_fallback(_patch_config):
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    # v0.6.0: budgets live in params_persistent (params_temporary is wiped
    # by agent.py every message-loop iteration).
    agent.loop_data.params_persistent["_misformat_guard_cascade_used_in_streak"] = 2
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"] is None


@pytest.mark.asyncio
async def test_total_cap_blocks_fallback(_patch_config):
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    agent.loop_data.params_persistent["_misformat_guard_cascade_used_total"] = 6
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"] is None


@pytest.mark.asyncio
async def test_fallback_disabled_no_op(_patch_config):
    _patch_config["cfg"]["process_tools_fallback"] = False
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"] is None


@pytest.mark.asyncio
async def test_utility_raises_no_substitution(_patch_config):
    """Utility model raising must not stall the safety net."""
    agent = _FakeAgent(
        utility_raises=RuntimeError("utility crashed"),
        process_tools_return="OK",
    )
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)  # must not raise
    assert data["result"] is None


@pytest.mark.asyncio
async def test_never_calls_chat_model(_patch_config):
    """The safety net must NEVER call the chat model -- only the
    utility model and the local process_tools re-invocation."""
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT

    async def _explode(*a, **kw):
        raise AssertionError("chat model must not be called by the safety net")

    agent.call_chat_model = _explode  # type: ignore[attr-defined]
    agent.call_chat_model_turn = _explode  # type: ignore[attr-defined]
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"] == "OK"


# ---------------------------------------------------------------------------
# v0.4.0 hardening: defensive against stale/partial api module
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_api_module_missing_is_misformat(_patch_config, monkeypatch):
    """If is_misformat is missing from a stale api module, the safety
    net must use a local fallback (or no-op) -- never crash through
    handle_exception."""
    _patch_config
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    monkeypatch.delattr(
        "usr.plugins.misformat_guard.api.misformat_repair.is_misformat",
        raising=False,
    )
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    # Must not raise
    await ext.execute(data=data)
    assert data["result"] == "OK", (
        "with a local is_misformat fallback, the safety net must still "
        "repair and re-invoke process_tools"
    )


@pytest.mark.asyncio
async def test_stale_api_module_missing_repair_function(_patch_config, monkeypatch):
    """If try_repair_via_utility is missing, the safety net must
    no-op -- never crash."""
    _patch_config
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = BROKEN_TEXT
    monkeypatch.delattr(
        "usr.plugins.misformat_guard.api.misformat_repair.try_repair_via_utility",
        raising=False,
    )
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    # Must not raise
    await ext.execute(data=data)
    assert data["result"] is None, (
        "with no try_repair_via_utility, the safety net must leave "
        "data['result'] as None"
    )


# ---------------------------------------------------------------------------
# v0.5.2 regression: msg extraction from the real extensible args shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_msg_read_from_args_not_slot0_self(_patch_config):
    """The @extensible wrapper passes data['args'] == (agent_self, msg):
    slot 0 is `self`, the message is the first str. The hook must repair
    from THAT message -- v0.5.1 read slot 0, got `self` (not a str), and
    silently fell back to the stale stream buffer."""
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    # NO stream buffer at all: the repair can only come from the args.
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), BROKEN_TEXT), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 1, (
        "the utility model must be called with the msg from args even "
        "when the stream buffer is empty"
    )
    assert BROKEN_TEXT in agent.utility_calls[0][1]
    assert data["result"] == "OK"


@pytest.mark.asyncio
async def test_parseable_msg_with_stale_buffer_no_repair(_patch_config):
    """v0.5.1 misfire scenario: a successful mid-stream extraction leaves
    a truncated JSON prefix in the stream buffer. When the framework later
    hands us a message that PARSES as a tool request, this was an ordinary
    dispatch -- the stale buffer must NOT trigger a utility repair or a
    re-invocation (which would re-execute the tool)."""
    agent = _FakeAgent(utility_response=VALID_REPAIR, process_tools_return="OK")
    agent.loop_data.params_temporary["_misformat_guard_stream_full"] = '{"tool": "resp'
    ext = _make_extension(agent)
    data = {"result": None, "exception": None, "args": (object(), VALID_REPAIR), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0, (
        "a parseable message means nothing to repair, even with a stale "
        "truncated buffer"
    )
    assert len(agent.process_tools_invocations) == 0, (
        "no re-invocation -- the tool must not be executed twice"
    )
    assert data["result"] is None
