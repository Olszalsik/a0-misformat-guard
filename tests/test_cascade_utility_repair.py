"""Tests for the v0.4.0 utility-model repair cascade.

Mocks the agent's call_utility_model and loop_data so the cascade can be
exercised without touching the real framework or burning tokens.

The cascade under test is the PRIMARY cascade, an @extensible /end
hook on Agent.call_chat_model_turn. It rewrites data['result'] when
the chat-model response fails the JSON tool extractor and the
utility model can repair it. The hook NEVER raises; failures fall
through to the framework's existing misformat warning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Make sure the agent-zero repo root is on sys.path so the extension
# imports cleanly. Tests run from the plugin directory inside the
# container; the convention used elsewhere in this plugin's tests is
# to anchor to /a0 (the install root). When running outside Docker
# (e.g. via `pytest` on the host), the runner can override
# `REPO_ROOT_OVERRIDE` in the environment.
REPO_ROOT = Path("/a0")
import os  # noqa: E402
_override = os.environ.get("REPO_ROOT_OVERRIDE")
if _override:
    REPO_ROOT = Path(_override)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from usr.plugins.misformat_guard.api import misformat_config  # noqa: E402
from usr.plugins.misformat_guard.extensions.python._functions.agent.Agent.call_chat_model_turn.end import (  # noqa: E402
    _20_repair_via_utility as cascade_mod,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

class _FakeLoopData:
    def __init__(self, streak: int = 0, params: dict | None = None):
        self.consecutive_misformats = streak
        self.params_temporary = params if params is not None else {}


class _FakeAgent:
    def __init__(
        self,
        utility_response: str = "",
        utility_raises: Exception | None = None,
        streak: int = 0,
    ):
        self.utility_response = utility_response
        self.utility_raises = utility_raises
        self.loop_data = _FakeLoopData(streak=streak)
        self.utility_calls: list[tuple[str, str]] = []

    async def call_utility_model(self, system: str, message: str, background: bool = False):
        self.utility_calls.append((system, message))
        if self.utility_raises is not None:
            raise self.utility_raises
        return self.utility_response

    def get_data(self, key, default=None):
        return default

    def set_data(self, key, value):
        return None


def _make_extension(agent: _FakeAgent):
    """Instantiate the Extension subclass with a mock agent."""
    return cascade_mod.CascadeUtilityRepair(agent=agent)


# Two kinds of misformats the cascade must handle:
#  1. TRUNCATED: the response is cut off mid-string (model hit max_tokens).
#  2. NO_TOOL: the response parses as JSON but has no `tool` key.
TRUNCATED_RESPONSE = '{"tool": "response_tool", "tool_args": {"text": "He said hello to me.'
NO_TOOL_RESPONSE = '{"result": "just some prose, no tool call"}'

# The default test target is the truncated one (the most common real-world case).
BROKEN_RESPONSE = TRUNCATED_RESPONSE

# A clearly valid chat-model response.
VALID_RESPONSE = json.dumps(
    {"tool": "response_tool", "tool_args": {"text": "He said hello to me."}}
)

# A valid utility-model repair output.
GOOD_REPAIR = json.dumps(
    {"tool": "response_tool", "tool_args": {"text": "He said hello to me."}}
)

# A utility output that is valid JSON but missing the required `tool` key.
BAD_SHAPE_REPAIR = json.dumps({"foo": "bar"})

# A utility output that is not even valid JSON.
BAD_TEXT_REPAIR = "sorry i cannot help"


def _config_with(cascade: dict, **overrides):
    """Build a full config dict by overlaying the cascade block on defaults."""
    base = misformat_config._load_default_from_disk()
    base["enabled"] = True
    base["primary_cascade_enabled"] = True
    base["process_tools_fallback"] = True
    base["cascade"] = cascade
    for k, v in overrides.items():
        base[k] = v
    return base


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    """Force misformat_config.get_config to return our test config."""
    holder = {"cfg": _config_with({"mode": "off"})}

    def _get(agent=None):
        return holder["cfg"]

    monkeypatch.setattr(misformat_config, "get_config", _get)
    yield holder


def _set_cascade(holder, **cascade_kwargs):
    base = {
        "mode": "utility_repair",
        "trigger": 1,
        "max_per_streak": 2,
        "max_total_per_chat": 6,
        "timeout_s": 30,
        "system_prompt_path": "prompts/utility_repair.md",
    }
    base.update(cascade_kwargs)
    holder["cfg"] = _config_with(base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_does_not_call_utility(_patch_config):
    """A parseable response must not trigger a utility call."""
    agent = _FakeAgent(streak=0)
    ext = _make_extension(agent)
    data = {"result": (VALID_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0, "utility model must not be called on the happy path"
    assert data["result"][0] == VALID_RESPONSE, "result must be unchanged on the happy path"


@pytest.mark.asyncio
async def test_misformat_triggers_utility_and_substitutes(_patch_config):
    """A broken response must trigger utility repair and substitute the result."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 1, "utility model must be called once on misformat"
    assert "JSON repair specialist" in agent.utility_calls[0][0]
    assert BROKEN_RESPONSE in agent.utility_calls[0][1]
    assert data["result"][0] == GOOD_REPAIR
    assert agent.loop_data.params_temporary["_misformat_guard_cascade_used_in_streak"] == 1
    assert agent.loop_data.params_temporary["_misformat_guard_cascade_used_total"] == 1


@pytest.mark.asyncio
async def test_utility_raises_does_not_modify_result(_patch_config):
    """If the utility call itself raises, the extension must leave the result alone."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_raises=RuntimeError("utility down"), streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"][0] == BROKEN_RESPONSE, "result must be unchanged when utility raises"


@pytest.mark.asyncio
async def test_utility_returns_bad_shape_does_not_modify(_patch_config):
    """A utility response without a `tool` key must not be substituted in."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=BAD_SHAPE_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_utility_returns_non_json_does_not_modify(_patch_config):
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=BAD_TEXT_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_streak_below_trigger_does_not_call(_patch_config):
    """If consecutive_misformats < trigger, the cascade must not fire even on misformat."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=3)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0, "trigger gate must block the cascade"
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_per_streak_cap_stops_cascade(_patch_config):
    """After max_per_streak cascade calls in one streak, the cascade must stop."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1, max_per_streak=2)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    agent.loop_data.params_temporary["_misformat_guard_cascade_used_in_streak"] = 2
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0, "per-streak cap must block the cascade"
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_total_cap_stops_cascade(_patch_config):
    _set_cascade(_patch_config, mode="utility_repair", trigger=1, max_total_per_chat=4)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    agent.loop_data.params_temporary["_misformat_guard_cascade_used_total"] = 4
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0


@pytest.mark.asyncio
async def test_mode_off_short_circuits(_patch_config):
    _set_cascade(_patch_config, mode="off")
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_utility_strips_code_fences(_patch_config):
    """Some small utility models wrap JSON in fences despite instructions."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    fenced = "```json\n" + GOOD_REPAIR + "\n```"
    agent = _FakeAgent(utility_response=fenced, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"][0] == GOOD_REPAIR, "code fences must be stripped before substitution"


@pytest.mark.asyncio
async def test_only_utility_model_used_never_chat(_patch_config):
    """The cascade must call call_utility_model, NEVER call_chat_model."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)

    async def _explode(*a, **kw):
        raise AssertionError("chat model must not be called by the cascade")

    agent.call_chat_model = _explode  # type: ignore[attr-defined]
    agent.call_chat_model_turn = _explode  # type: ignore[attr-defined]
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert data["result"][0] == GOOD_REPAIR


# ---------------------------------------------------------------------------
# v0.4.0 user-scenario regression test
# ---------------------------------------------------------------------------

# The actual failure mode from the user's docker logs: the chat model
# was generating a `code_execution_tool` call and the response was
# TRUNCATED mid-string (most likely max_tokens). The dirty parser
# fails because the string value never closed. The cascade is
# designed exactly for this case: detect "no parseable tool request",
# call the utility model, substitute the repaired text.
DOCKER_LOG_BROKEN = (
    '{"thoughts": ["I have the full project context. The user is the '
    'senior dev giving REVISE feedback on the data_ingress_bridge.py '
    'module. I need to: (1) fix the struct pack error, (2) run the '
    'bit-perfect sweep, (3) capture SHA-256 receipts."], '
    '"headline": "Verify patches and run the bit-perfect sweep", '
    '"tool_name": "code_execution_tool", '
    '"tool_args": {"code": "pkill -9 -f \'python.*data_ingress\'; '
    'import re; p = \'test_ingress_bridge.py\'; '
    's = open(p).read(); '
    'pat1 = re.compile(r"_MD_FULL_FMT = \\"\\\\<HHi\\" + _MD_BODY_FMT'
)

DOCKER_LOG_REPAIRED = json.dumps({
    "thoughts": ["Verified patches and ran the sweep."],
    "headline": "Sweep complete",
    "tool_name": "code_execution_tool",
    "tool_args": {"code": "echo done"},
})


@pytest.mark.asyncio
async def test_user_docker_log_scenario(_patch_config):
    """The actual failure mode from the user's docker logs: a chat
    response that was TRUNCATED mid-string (most likely max_tokens),
    so the dirty parser fails to close the `text` value. The cascade
    must fire, call the utility model, and substitute a parseable
    response that downstream tooling can dispatch."""
    # First confirm the test fixture is actually unparseable (would be
    # embarrassing to ship a "regression test" for a parseable string).
    from usr.plugins.misformat_guard.api import misformat_repair as rep
    assert rep.is_misformat(DOCKER_LOG_BROKEN), (
        "test fixture DOCKER_LOG_BROKEN must be unparseable for this "
        "regression test to be meaningful; if the framework's parser "
        "improved and now handles this, update the fixture to a "
        "still-broken case (e.g. deeper truncation)"
    )

    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=DOCKER_LOG_REPAIRED, streak=1)
    ext = _make_extension(agent)
    data = {"result": (DOCKER_LOG_BROKEN, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 1, (
        "cascade must call the utility model on the user's docker-log "
        "misformat scenario"
    )
    assert DOCKER_LOG_BROKEN in agent.utility_calls[0][1], (
        "the broken response must be passed to the utility model for repair"
    )
    assert data["result"][0] == DOCKER_LOG_REPAIRED, (
        "the repaired response must be substituted into data['result']"
    )


@pytest.mark.asyncio
async def test_never_stalls_on_utility_timeout(_patch_config):
    """If the utility model times out, the cascade must leave the result
    alone and let the framework's existing misformat warning fire. The
    agent must NEVER stall -- this is the user's stated requirement."""
    import asyncio

    _set_cascade(_patch_config, mode="utility_repair", trigger=1, timeout_s=0.1)
    agent = _FakeAgent(streak=1)

    async def _hang(*a, **kw):
        await asyncio.sleep(5)  # longer than timeout_s

    agent.call_utility_model = _hang  # type: ignore[assignment]
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)  # must return without raising
    assert data["result"][0] == BROKEN_RESPONSE, (
        "on utility timeout, the broken response must be passed through "
        "unchanged so the framework's misformat warning can fire and the "
        "LLM can retry -- this is what 'never stall' means"
    )


@pytest.mark.asyncio
async def test_primary_disabled_short_circuits(_patch_config):
    """If primary_cascade_enabled is false, the cascade must no-op even
    on a misformat. (The safety-net cascade is a separate config flag.)"""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    holder = _patch_config
    holder["cfg"]["primary_cascade_enabled"] = False
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    assert len(agent.utility_calls) == 0
    assert data["result"][0] == BROKEN_RESPONSE


@pytest.mark.asyncio
async def test_records_cascade_attempt_stat(_patch_config):
    """The cascade must call record_cascade_attempt on every attempt
    (success or failure) so the WebUI stats card can show attempts vs
    outcomes separately."""
    from usr.plugins.misformat_guard.api import misformat_stats

    misformat_stats.reset()
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    await ext.execute(data=data)
    snap = misformat_stats.snapshot(agent)
    assert snap["cascade_attempts_total"] == 1
    assert snap["cascade_repairs_total"] == 1
    assert snap["cascade_calls_total"] == 1
    assert snap["cascade_failures_total"] == 0


# ---------------------------------------------------------------------------
# v0.4.0 hardening: defensive against stale/partial api module
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_api_module_missing_is_misformat(_patch_config, monkeypatch):
    """If the api module is stale (e.g. cached .pyc from a previous
    version without is_misformat), the cascade must not crash through
    handle_exception. It must fall back to a local is_misformat or
    no-op cleanly. This protects the agent from 'never stall'
    regressions during plugin updates / container restarts mid-flight.
    """
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    # Simulate a stale module: monkeypatch the symbol away
    monkeypatch.delattr(
        "usr.plugins.misformat_guard.api.misformat_repair.is_misformat",
        raising=False,
    )
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    # Must not raise
    await ext.execute(data=data)
    # In the stale-attribute case, the cascade should use the local
    # fallback (which correctly identifies the broken text as misformat),
    # proceed to call the utility, and substitute the repair.
    assert len(agent.utility_calls) == 1, (
        "with a local is_misformat fallback, the cascade must still "
        "call the utility model on misformat"
    )
    assert data["result"][0] == GOOD_REPAIR


@pytest.mark.asyncio
async def test_stale_api_module_missing_repair_function(_patch_config, monkeypatch):
    """If try_repair_via_utility is missing from the api module, the
    cascade must record a failure and no-op -- never crash."""
    _set_cascade(_patch_config, mode="utility_repair", trigger=1)
    monkeypatch.delattr(
        "usr.plugins.misformat_guard.api.misformat_repair.try_repair_via_utility",
        raising=False,
    )
    agent = _FakeAgent(utility_response=GOOD_REPAIR, streak=1)
    ext = _make_extension(agent)
    data = {"result": (BROKEN_RESPONSE, ""), "args": (), "kwargs": {}}
    # Must not raise
    await ext.execute(data=data)
    assert data["result"][0] == BROKEN_RESPONSE, (
        "with no try_repair_via_utility, the cascade must leave the "
        "original response untouched"
    )
