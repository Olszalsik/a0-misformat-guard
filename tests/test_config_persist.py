"""Tests for the WebUI config-persistence path (``api/config.py``).

These pin the contract that the Layer 5 (v0.5.0) tool-repeat guard keys
are accepted by the POST /config handler, coerced to the right types, and
that the 0-falsy gotcha is respected (a threshold of 0 disables that
half of the guard and must NOT be coerced back to the default).

``_SCALAR_KEYS`` + ``_coerce`` are the only things that stand between the
Alpine form and a mis-persisted config, so they get their own test file.
"""

from __future__ import annotations

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


from usr.plugins.misformat_guard.api import config as config_mod  # noqa: E402


# ---------------------------------------------------------------------------
# The new Layer 5 keys are recognised scalar keys
# ---------------------------------------------------------------------------


def test_tool_repeat_keys_are_scalar():
    """Every tool_repeat_* key the UI posts must be in _SCALAR_KEYS so the
    POST handler coerces + persists it (keys NOT in _SCALAR_KEYS pass
    through uncoerced, which is fine, but being explicit locks the
    contract)."""
    for k in (
        "tool_repeat_guard_enabled",
        "tool_repeat_warn_threshold",
        "tool_repeat_stop_threshold",
        "tool_repeat_action",
        "tool_repeat_normalize_args",
    ):
        assert k in config_mod._SCALAR_KEYS, f"{k} missing from _SCALAR_KEYS"


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def test_coerce_bool_passthrough():
    assert config_mod._coerce("tool_repeat_guard_enabled", True) is True
    assert config_mod._coerce("tool_repeat_guard_enabled", False) is False


def test_coerce_thresholds_clamp_and_allow_zero():
    # 0 (disable) must survive -- the 0-falsy gotcha.
    assert config_mod._coerce("tool_repeat_warn_threshold", 0) == 0
    assert config_mod._coerce("tool_repeat_stop_threshold", 0) == 0
    # Normal values pass through clamped to [0, 20].
    assert config_mod._coerce("tool_repeat_warn_threshold", 2) == 2
    assert config_mod._coerce("tool_repeat_stop_threshold", 4) == 4
    # String form values (the UI sends strings) are int-parsed.
    assert config_mod._coerce("tool_repeat_warn_threshold", "3") == 3
    # Out-of-range clamps.
    assert config_mod._coerce("tool_repeat_stop_threshold", 999) == 20
    assert config_mod._coerce("tool_repeat_warn_threshold", -5) == 0


def test_coerce_action_validated():
    for valid in ("warn", "stop", "warn_then_stop"):
        assert config_mod._coerce("tool_repeat_action", valid) == valid
    # An unknown action falls back to the default, never persisted as-is.
    assert config_mod._coerce("tool_repeat_action", "explode") == "warn_then_stop"
    assert config_mod._coerce("tool_repeat_action", "") == "warn_then_stop"


def test_coerce_threshold_does_not_touch_legacy_threshold():
    """The legacy ``threshold`` (Layer 3 hardened-parser) keeps its own
    [1,10] clamp and is NOT affected by the new tool-repeat coercion."""
    assert config_mod._coerce("threshold", 0) == 1  # legacy clamps to >=1
    assert config_mod._coerce("threshold", 5) == 5


# ---------------------------------------------------------------------------
# The POST handler path (build the cleaned dict the way the handler does)
# ---------------------------------------------------------------------------


def test_post_handler_accepts_tool_repeat_keys():
    """Mirror the handler's loop: coerce every key in _SCALAR_KEYS, pass
    the rest through. The tool_repeat keys must end up coerced in the
    cleaned dict that gets persisted. Alpine x-model sends real bools /
    ints (JSON-typed), not strings."""
    input_data = {
        "tool_repeat_guard_enabled": True,  # checkbox -> bool
        "tool_repeat_warn_threshold": 2,     # x-model.number -> int
        "tool_repeat_stop_threshold": 0,     # 0 (disable) must survive
        "tool_repeat_action": "warn_then_stop",
        "tool_repeat_normalize_args": False,
        "stats_enabled": True,
        "some_unknown_key": "left-as-is",
    }
    cleaned = {}
    for k, v in input_data.items():
        cleaned[k] = config_mod._coerce(k, v) if k in config_mod._SCALAR_KEYS else v
    # Unknown key passes through untouched.
    assert cleaned["some_unknown_key"] == "left-as-is"
    # Booleans stay booleans.
    assert cleaned["tool_repeat_guard_enabled"] is True
    # tool_repeat_action validated.
    assert cleaned["tool_repeat_action"] == "warn_then_stop"
    # stop_threshold=0 respected (not coerced to a default).
    assert cleaned["tool_repeat_stop_threshold"] == 0
    assert cleaned["tool_repeat_warn_threshold"] == 2