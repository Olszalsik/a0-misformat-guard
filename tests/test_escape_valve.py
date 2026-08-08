"""Tests for the misformat_guard config layer (v0.4.0).

The plugin uses Agent Zero's helpers.plugins.get_plugin_config to resolve
config. When the plugin is not registered with that helper yet (e.g. in
unit tests that run before install), the config layer falls back to
default_config.yaml. These tests pin both paths.

v0.4.0 removed the v0.2.0 escape-valve config keys (threshold,
abort_message) -- the agent never aborts in v0.4.0. These tests now
pin the v0.4.0 cascade config keys instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

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

from usr.plugins.misformat_guard.api import misformat_config  # noqa: E402


def test_get_config_returns_dict() -> None:
    cfg = misformat_config.get_config(agent=None)
    assert isinstance(cfg, dict)
    # Defaults must include the v0.4.0 major switches.
    assert "enabled" in cfg
    assert "primary_cascade_enabled" in cfg
    assert "process_tools_fallback" in cfg
    assert "quote_rules_enabled" in cfg


def test_get_config_cascade_block() -> None:
    cfg = misformat_config.get_config(agent=None)
    cascade = cfg.get("cascade")
    assert isinstance(cascade, dict), "cascade block must be a dict"
    # The cascade must default to utility_repair so out-of-the-box the
    # plugin actually does something.
    assert cascade.get("mode") == "utility_repair"
    trigger = cascade.get("trigger")
    assert isinstance(trigger, int)
    assert 1 <= trigger <= 10
    max_per = cascade.get("max_per_streak")
    assert isinstance(max_per, int)
    assert 1 <= max_per <= 20
    max_total = cascade.get("max_total_per_chat")
    assert isinstance(max_total, int)
    assert 1 <= max_total <= 100
    timeout = cascade.get("timeout_s")
    assert isinstance(timeout, (int, float))
    assert 1 <= timeout <= 300
    # The system_prompt_path must point at the shipped repair prompt
    assert "utility_repair.md" in cascade.get("system_prompt_path", "")


def test_get_config_no_escape_valve_keys() -> None:
    """v0.4.0 removed the v0.2.0 escape-valve (threshold, abort_message).
    The agent never aborts now; the cascade is the recovery path."""
    cfg = misformat_config.get_config(agent=None)
    assert "threshold" not in cfg, (
        "v0.4.0 removed the threshold config; the agent never aborts"
    )
    assert "abort_message" not in cfg, (
        "v0.4.0 removed the abort_message config; the agent never aborts"
    )


def test_is_enabled_defaults_true() -> None:
    # The default config has enabled: true.
    assert misformat_config.is_enabled(None) is True


def test_load_default_from_disk_caches() -> None:
    # Calling twice should not re-read the file.
    a = misformat_config._load_default_from_disk()
    b = misformat_config._load_default_from_disk()
    assert a is b  # cached
