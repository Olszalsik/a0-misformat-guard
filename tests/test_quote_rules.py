"""Tests for the misformat_guard quote rules prompt injection.

These tests pin the contract of the system_prompt extension: it appends
the quoting rules block to the system_prompt list, exactly once, and
only when the plugin is enabled.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

def _find_project_root(start: Path) -> Path:
    """Walk up from `start` until we find agent.py (the framework root)."""
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

from usr.plugins.misformat_guard.extensions.python.system_prompt._10_quote_rules import (  # noqa: E402
    QuoteRulesInjector,
    MARKER,
)


def _make_agent(enabled: bool = True) -> MagicMock:
    agent = MagicMock()
    # misformat_config.get_config is called with the agent; stub the
    # helper to return a controlled config.
    import usr.plugins.misformat_guard.api.misformat_config as cfg_mod

    def fake_get_config(_agent):
        return {
            "enabled": enabled,
            "quote_rules_enabled": True,
            "quote_rules_path": "prompts/quote_rules.md",
        }

    cfg_mod.get_config = fake_get_config
    return agent


def test_injection_appends_marker() -> None:
    agent = _make_agent(enabled=True)
    ext = QuoteRulesInjector(agent=agent)
    prompt: list = []
    asyncio.run(ext.execute(system_prompt=prompt))
    assert any(MARKER in (s or "") for s in prompt)


def test_injection_skips_when_disabled() -> None:
    agent = _make_agent(enabled=False)
    ext = QuoteRulesInjector(agent=agent)
    prompt: list = []
    asyncio.run(ext.execute(system_prompt=prompt))
    assert prompt == []


def test_injection_is_idempotent() -> None:
    """Calling execute twice does not append twice."""
    agent = _make_agent(enabled=True)
    ext = QuoteRulesInjector(agent=agent)
    prompt: list = []
    asyncio.run(ext.execute(system_prompt=prompt))
    asyncio.run(ext.execute(system_prompt=prompt))
    matches = [s for s in prompt if MARKER in (s or "")]
    assert len(matches) == 1


def test_injection_handles_none_prompt() -> None:
    agent = _make_agent(enabled=True)
    ext = QuoteRulesInjector(agent=agent)
    # Should not raise when system_prompt is None.
    asyncio.run(ext.execute(system_prompt=None))
