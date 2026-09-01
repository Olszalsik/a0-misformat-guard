"""Tests for the v0.4.1 consume-misformat-warning hook.

The hook sits at
extensions/python/_functions/agent/Agent/hist_add_warning/end/
_10_misformat_consume_warning.py and runs before the framework's
upstream cost circuit breaker at .../end/_90_stop_unusable_response_loop.py.

These tests pin the contract:
  - the hook is a no-op on non-misformat warnings
  - the hook is a no-op when the plugin is disabled
  - the hook is a no-op when reset_unusable_loop_on_warning is false
  - the hook resets the upstream's params_persistent counter on a
    misformat warning when the plugin is enabled
  - the hook does not raise on any of the edge cases (missing state,
    wrong args shape, missing loop_data)
  - end-to-end: with both extensions loaded, the upstream does not
    raise HandledException after 2 misformat warnings when the
    consume hook is active
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MISFORMAT_TEXT = "you have misformatted your message"
REPEAT_TEXT = "you have repeated the same response"


def _make_agent(enabled: bool = True, reset_on_warning: bool = True,
                state: dict | None = None, iteration: int = 0):
    """Build a mock agent with read_prompt, loop_data, and a
    params_persistent dict the consume hook can mutate."""
    params_persistent = state if state is not None else {}
    loop_data = SimpleNamespace(
        iteration=iteration,
        params_persistent=params_persistent,
    )

    prompts = {
        "fw.msg_misformat.md": MISFORMAT_TEXT,
        "fw.msg_repeat.md": REPEAT_TEXT,
        # Upstream (v2.10+) treats the empty-response warning as a third
        # unusable marker and reads it whenever it classifies a warning.
        "fw.msg_empty_response.md": "your response was empty",
        # The upstream stop path reads this (with limit=) when the
        # counter hits the ceiling. Must be present so read_prompt does
        # not KeyError in the end-to-end "hook disabled -> upstream
        # raises" test.
        "fw.msg_unusable_response_limit.md": "stop: too many unusable responses",
    }

    def read_prompt(name, **kwargs):
        return prompts[name]

    cfg = {
        "enabled": enabled,
        "reset_unusable_loop_on_warning": reset_on_warning,
        "verbose": False,
    }

    agent = SimpleNamespace(
        loop_data=loop_data,
        read_prompt=read_prompt,
        _cfg=cfg,
        # The upstream stop path (_90_stop_unusable_response_loop.py:54)
        # calls self.agent.context.log.log(type=, content=) right before
        # it sets data["exception"]. Give it a no-op log so the
        # disabled-hook end-to-end test can reach the exception set.
        context=SimpleNamespace(log=SimpleNamespace(log=lambda **kw: None)),
    )
    return agent


def _patch_config(monkeypatch, cfg):
    """Force misformat_config.get_config to return our test config."""
    from usr.plugins.misformat_guard.api import misformat_config

    def _get(agent=None):
        return cfg

    monkeypatch.setattr(misformat_config, "get_config", _get)


def _load_hook():
    """Import the hook class. Done lazily so a single import error
    fails the whole module (not each test)."""
    from usr.plugins.misformat_guard.extensions.python._functions.agent.Agent.hist_add_warning.end import (  # noqa: E501
        _10_misformat_consume_warning as hook_mod,
    )
    return hook_mod


def _run_hook(agent, message: str, data: dict | None = None):
    """Run the hook synchronously and return the data payload (mutated)."""
    hook_mod = _load_hook()
    if data is None:
        # hist_add_warning is called as self.hist_add_warning(message),
        # so args = (self, message) and kwargs has id="".
        data = {"args": (agent, message), "kwargs": {"id": ""}, "result": None, "exception": None}
    hook = hook_mod.ConsumeMisformatWarning(agent=agent)
    hook.execute(data=data)
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_noop_on_non_misformat_warning(monkeypatch):
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={"_unusable_response_failures": {"iteration": 0, "count": 1}})
    _run_hook(agent, REPEAT_TEXT)
    # State unchanged -- we only consume misformat warnings, not repeat warnings.
    assert agent.loop_data.params_persistent == {"_unusable_response_failures": {"iteration": 0, "count": 1}}


def test_noop_when_plugin_disabled(monkeypatch):
    _patch_config(monkeypatch, {"enabled": False, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=False, state={"_unusable_response_failures": {"iteration": 0, "count": 1}})
    _run_hook(agent, MISFORMAT_TEXT)
    assert agent.loop_data.params_persistent == {"_unusable_response_failures": {"iteration": 0, "count": 1}}


def test_noop_when_reset_flag_false(monkeypatch):
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": False})
    agent = _make_agent(enabled=True, reset_on_warning=False, state={"_unusable_response_failures": {"iteration": 0, "count": 1}})
    _run_hook(agent, MISFORMAT_TEXT)
    assert agent.loop_data.params_persistent == {"_unusable_response_failures": {"iteration": 0, "count": 1}}


def test_resets_counter_on_misformat_warning(monkeypatch):
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={"_unusable_response_failures": {"iteration": 0, "count": 1}}, iteration=2)
    _run_hook(agent, MISFORMAT_TEXT)
    # Counter is reset to count=0 at the current iteration; the upstream
    # will then increment to 1, which is below max_consecutive_unusable_responses (2).
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 2, "count": 0}


def test_resets_counter_from_exhausted_state(monkeypatch):
    """If the upstream counter somehow reached the limit already, we
    still reset -- this is the precise scenario the user reported."""
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={"_unusable_response_failures": {"iteration": 1, "count": 2}}, iteration=2)
    _run_hook(agent, MISFORMAT_TEXT)
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 2, "count": 0}


def test_initializes_state_when_missing(monkeypatch):
    """If the upstream's state entry doesn't exist yet, we still set
    it (the upstream will then read count=0 and increment to 1)."""
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={}, iteration=5)
    _run_hook(agent, MISFORMAT_TEXT)
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 5, "count": 0}


def test_noop_on_missing_loop_data(monkeypatch):
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={})
    agent.loop_data = None
    # Must not raise.
    _run_hook(agent, MISFORMAT_TEXT)


def test_noop_on_non_dict_data():
    """Defensive: the framework guarantees data is a dict, but if a
    future change breaks that, the hook must no-op, not raise."""
    agent = _make_agent(enabled=True, state={})
    hook_mod = _load_hook()
    hook = hook_mod.ConsumeMisformatWarning(agent=agent)
    # No-args call: kwargs['data'] = None.
    hook.execute(data=None)
    # No state mutation.
    assert "_unusable_response_failures" not in agent.loop_data.params_temporary if hasattr(agent.loop_data, "params_temporary") else True
    assert agent.loop_data.params_persistent == {}


def test_noop_on_empty_message(monkeypatch):
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={})
    _run_hook(agent, "")
    assert agent.loop_data.params_persistent == {}


def test_reads_message_from_kwargs_first(monkeypatch):
    """hist_add_warning can be called with message= as a kwarg. The
    hook should pick that up before falling back to args[1]."""
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    agent = _make_agent(enabled=True, state={}, iteration=7)
    data = {
        "args": (agent, "this-is-arg-1-and-should-be-ignored"),
        "kwargs": {"id": "", "message": MISFORMAT_TEXT},
        "result": None,
        "exception": None,
    }
    _run_hook(agent, None, data=data)
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 7, "count": 0}


# ---------------------------------------------------------------------------
# End-to-end: hook + upstream together
# ---------------------------------------------------------------------------

def test_upstream_does_not_raise_when_consume_hook_active(monkeypatch):
    """Simulate the user's reported failure: 2 consecutive misformat
    warnings. With the v0.4.0 plugin (no consume hook) the upstream
    would raise HandledException on the second one. With v0.4.1
    (consume hook active) the upstream must not raise."""
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": True})
    from helpers.errors import HandledException
    from extensions.python._functions.agent.Agent.hist_add_warning.end import (
        _90_stop_unusable_response_loop as upstream_mod,
    )

    # Patch the framework's settings so the upstream reads limit=2.
    monkeypatch.setattr(
        upstream_mod,
        "get_settings",
        lambda: {"max_consecutive_unusable_responses": 2},
    )

    # A fresh agent, fresh state.
    agent = _make_agent(enabled=True, state={}, iteration=0)

    # Misformat 1: the consume hook writes {iteration: 0, count: 0} to
    # params_persistent. The upstream then runs on the SAME extension-
    # point invocation (same iteration=0), sees previous_iteration == iteration,
    # and hits its same-iteration guard (_90_stop_unusable_response_loop.py
    # line 38-39) -> returns early WITHOUT incrementing. So the counter
    # stays at 0 (not 1) and no exception is set. The outcome the user
    # cares about -- "do not raise" -- holds; the counter is simply held
    # at 0 rather than nudged to 1.
    agent.loop_data.iteration = 0
    data = _run_hook(agent, MISFORMAT_TEXT, data={
        "args": (agent, MISFORMAT_TEXT),
        "kwargs": {"id": ""},
        "result": None,
        "exception": None,
    })
    # Now run the upstream on the same data (this is the real ordering
    # in production: _10 hook -> _90 hook).
    upstream_mod.StopUnusableResponseLoop(agent=agent).execute(data=data)
    assert data["exception"] is None, "upstream should not raise on first misformat"
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 0, "count": 0}

    # Misformat 2 (next iteration): same story -- consume hook writes
    # {iteration: 1, count: 0}, upstream same-iteration guard returns
    # early, counter stays 0, no raise.
    agent.loop_data.iteration = 1
    data = _run_hook(agent, MISFORMAT_TEXT, data={
        "args": (agent, MISFORMAT_TEXT),
        "kwargs": {"id": ""},
        "result": None,
        "exception": None,
    })
    upstream_mod.StopUnusableResponseLoop(agent=agent).execute(data=data)
    assert data["exception"] is None, "upstream must not raise on second misformat when consume hook is active"
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 1, "count": 0}

    # Misformat 3: same, still OK. The user can keep going.
    agent.loop_data.iteration = 2
    data = _run_hook(agent, MISFORMAT_TEXT, data={
        "args": (agent, MISFORMAT_TEXT),
        "kwargs": {"id": ""},
        "result": None,
        "exception": None,
    })
    upstream_mod.StopUnusableResponseLoop(agent=agent).execute(data=data)
    assert data["exception"] is None, "upstream must not raise on third misformat when consume hook is active"


def test_upstream_still_raises_when_consume_hook_disabled(monkeypatch):
    """Sanity: with the consume hook disabled, the existing upstream
    test scenario still produces HandledException at the configured
    limit. This proves we haven't broken the upstream's contract."""
    _patch_config(monkeypatch, {"enabled": True, "reset_unusable_loop_on_warning": False})
    from helpers.errors import HandledException
    from extensions.python._functions.agent.Agent.hist_add_warning.end import (
        _90_stop_unusable_response_loop as upstream_mod,
    )

    monkeypatch.setattr(
        upstream_mod,
        "get_settings",
        lambda: {"max_consecutive_unusable_responses": 2},
    )

    agent = _make_agent(enabled=True, reset_on_warning=False, state={}, iteration=0)

    # Misformat 1: consume hook is a no-op (reset flag off). Upstream
    # has no prior state -> count=1 (the else branch, since
    # previous_iteration None != iteration-1). 1 < limit(2) -> no raise.
    agent.loop_data.iteration = 0
    data = _run_hook(agent, MISFORMAT_TEXT, data={
        "args": (agent, MISFORMAT_TEXT),
        "kwargs": {"id": ""},
        "result": None,
        "exception": None,
    })
    upstream_mod.StopUnusableResponseLoop(agent=agent).execute(data=data)
    assert data["exception"] is None
    assert agent.loop_data.params_persistent["_unusable_response_failures"] == {"iteration": 0, "count": 1}

    # Misformat 2 (next iteration): consume hook still a no-op. Upstream
    # sees previous_iteration(0) == iteration-1(0) -> consecutive, so
    # count = 1 + 1 = 2. count(2) >= limit(2) -> upstream reads
    # fw.msg_unusable_response_limit.md (now in the mock prompts) and
    # raises HandledException. This is exactly the contract the consume
    # hook exists to prevent -- and here it is NOT preventing it because
    # we turned the hook off.
    agent.loop_data.iteration = 1
    data = _run_hook(agent, MISFORMAT_TEXT, data={
        "args": (agent, MISFORMAT_TEXT),
        "kwargs": {"id": ""},
        "result": None,
        "exception": None,
    })
    upstream_mod.StopUnusableResponseLoop(agent=agent).execute(data=data)
    assert isinstance(data["exception"], HandledException), (
        "with the consume hook disabled, the upstream must still raise "
        "at its configured limit (2 consecutive misformats) -- this "
        "proves the consume hook is what suppresses the raise in the "
        "active test, not a change to the upstream itself"
    )
