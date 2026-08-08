"""Configuration resolution for the misformat_guard plugin.

Uses Agent Zero's official helpers.plugins.get_plugin_config, which already
handles per-project, per-agent, and global config resolution with the
correct precedence and fall-back to default_config.yaml.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "misformat_guard"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_DIR / "default_config.yaml"

# Cached default config.
_DEFAULT_CACHE: dict[str, Any] | None = None


def _load_default_from_disk() -> dict[str, Any]:
    """Read default_config.yaml directly (used as a fallback only)."""
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is not None:
        return _DEFAULT_CACHE
    try:
        import yaml
        with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as f:
            _DEFAULT_CACHE = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        _DEFAULT_CACHE = {}
    return _DEFAULT_CACHE


def get_config(agent: Any | None = None) -> dict[str, Any]:
    """Resolve the effective configuration for the given agent context.

    Resolution order (highest priority first):
      1. Per-project:  <project>/.a0proj/agents/<profile>/plugins/misformat_guard/config.json
      2. Per-agent:    usr/agents/<profile>/plugins/misformat_guard/config.json
      3. Global:       usr/plugins/misformat_guard/config.json
      4. Defaults:     default_config.yaml (loaded by helpers.plugins if no
                      config.json exists at the active scope)
    """
    try:
        from helpers import plugins as plugins_helper
        cfg = plugins_helper.get_plugin_config(PLUGIN_NAME, agent=agent)
    except Exception:  # noqa: BLE001
        cfg = None

    if not isinstance(cfg, dict) or not cfg:
        cfg = _load_default_from_disk()
    return cfg


def is_enabled(agent: Any | None = None) -> bool:
    cfg = get_config(agent)
    return bool(cfg.get("enabled", True))
