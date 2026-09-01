"""
Misformat Guard - runtime hooks for Agent Zero v2.5.

Lifecycle:
  - install()    : set up the plugin state, ensure required directories,
                   and (v0.4.1) optionally raise the framework's
                   max_consecutive_unusable_responses setting to the
                   plugin's consecutive_unusable_floor.
  - pre_update() : snapshot the plugin state before the plugin is updated.
  - uninstall()  : clear class attributes, remove toggle files, and
                   (v0.4.1) restore the original framework setting if
                   install() changed it.

The plugin uses Agent Zero v2.5's @extension.extensible +
_functions/.../end hooks to repair misformatted chat-model responses
with the cheap utility model. NO core patch is required (and none
should be applied). The v0.3.0 core patch that was previously
maintained in usr/patches/ is no longer needed and is not applied.

v0.4.1 also installs a hook at
extensions/python/_functions/agent/Agent/hist_add_warning/end/
_10_misformat_consume_warning.py that coordinates with the framework's
upstream cost circuit breaker at .../end/_90_stop_unusable_response_loop.py.
That coordination is implemented as a runtime hook, not a framework
patch, so the plugin remains a pure extension-point consumer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = PLUGIN_DIR.parent.parent.parent  # usr/plugins/<name> -> usr/plugins -> usr -> repo root

PLUGIN_VERSION = "0.5.1"

# Framework setting key (see helpers/settings.py:59 and the upstream
# circuit breaker at extensions/python/_functions/agent/Agent/hist_add_warning/end/
# _90_stop_unusable_response_loop.py).
FRAMEWORK_CONSECUTIVE_KEY = "max_consecutive_unusable_responses"


def _log(message: str) -> None:
    sys.stdout.write(f"[misformat_guard] {message}\n")
    sys.stdout.flush()


def _plugin_state_path() -> Path:
    return PLUGIN_DIR / ".plugin_state.json"


def _save_state(state: dict[str, Any]) -> None:
    try:
        _plugin_state_path().write_text(json.dumps(state, indent=2))
    except Exception as exc:  # noqa: BLE001 - best-effort
        _log(f"warning: could not write plugin state: {exc}")


def _load_state() -> dict[str, Any]:
    path = _plugin_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text() or "{}")
    except Exception:  # noqa: BLE001
        return {}


def apply_core_patch() -> bool:
    """v0.4.0: no core patch is required.

    Agent Zero v2.5 (commit bf2046ff, "Expose unified model calls to
    extensions") added @extension.extensible decorators and the
    _functions/.../end hook convention, which give the plugin a clean
    seam to rewrite the chat-model return value without touching
    agent.py. We keep this function as a no-op so the lifecycle is
    symmetrical and so uninstall()'s revert_core_patch() stays a
    safe no-op.
    """
    return True


def revert_core_patch() -> bool:
    """v0.4.0: nothing to revert (apply_core_patch is a no-op)."""
    return True


def _read_plugin_config() -> dict[str, Any]:
    """Read the plugin's own config (not the framework's settings).

    Returns the effective config dict. Falls back to defaults if the
    plugin config layer is unavailable.
    """
    try:
        from usr.plugins.misformat_guard.api import misformat_config
        return misformat_config.get_config(None) or {}
    except Exception:  # noqa: BLE001
        return {}


def _install_consecutive_floor() -> dict[str, Any] | None:
    """v0.4.1: raise the framework's max_consecutive_unusable_responses
    to the plugin's floor, if configured to do so.

    Writes the original value to .plugin_state.json BEFORE changing
    the framework setting, so a crash mid-install leaves the original
    recoverable. Returns a small dict describing what was done, or
    None if the install was skipped.
    """
    cfg = _read_plugin_config()
    if not cfg.get("install_overrides_consecutive_floor", True):
        return None

    floor_raw = cfg.get("consecutive_unusable_floor", 5)
    try:
        floor = max(1, int(floor_raw))
    except Exception:  # noqa: BLE001
        floor = 5

    try:
        from helpers import settings as settings_helper
        current = settings_helper.get_settings().get(FRAMEWORK_CONSECUTIVE_KEY, 2)
    except Exception as exc:  # noqa: BLE001
        _log(f"install: could not read framework settings ({exc!r}); skipping floor install")
        return None

    try:
        current_i = int(current)
    except Exception:  # noqa: BLE001
        current_i = 2

    if current_i >= floor:
        # Already at or above the floor. Nothing to do, no rollback needed.
        return {
            "previous_value": current_i,
            "new_value": current_i,
            "floor": floor,
            "applied": False,
        }

    new_value = floor

    # Persist the original value FIRST. If the next step crashes, we
    # can recover the user's setting from .plugin_state.json.
    state = _load_state()
    state["framework_consecutive_original"] = current_i
    state["framework_consecutive_applied"] = new_value
    _save_state(state)

    try:
        from helpers import settings as settings_helper
        s = settings_helper.get_settings()
        s[FRAMEWORK_CONSECUTIVE_KEY] = new_value
        settings_helper.normalize_settings(s)
        # Persist the change via the framework's settings API.
        try:
            settings_helper.update_settings(s)
        except AttributeError:
            # Older framework API. Best-effort: leave the in-memory
            # setting; the user can still adjust from the WebUI.
            _log("install: helpers.settings has no update_settings(); "
                 "in-memory change applied, persisted change skipped")
    except Exception as exc:  # noqa: BLE001
        _log(f"install: failed to apply consecutive floor ({exc!r}); "
             f"original value preserved in .plugin_state.json")
        return {
            "previous_value": current_i,
            "new_value": current_i,
            "floor": floor,
            "applied": False,
            "error": repr(exc),
        }

    return {
        "previous_value": current_i,
        "new_value": new_value,
        "floor": floor,
        "applied": True,
    }


def _uninstall_consecutive_floor() -> dict[str, Any] | None:
    """v0.4.1: restore the framework's max_consecutive_unusable_responses
    to the value the plugin recorded at install() time.

    No-op if the plugin never raised the setting (no .plugin_state.json
    entry, or the saved entry says 'not applied').
    """
    state = _load_state()
    original = state.get("framework_consecutive_original")
    if original is None:
        return None

    try:
        from helpers import settings as settings_helper
        s = settings_helper.get_settings()
        s[FRAMEWORK_CONSECUTIVE_KEY] = int(original)
        settings_helper.normalize_settings(s)
        try:
            settings_helper.update_settings(s)
        except AttributeError:
            _log("uninstall: helpers.settings has no update_settings(); "
                 "in-memory change applied, persisted change skipped")
    except Exception as exc:  # noqa: BLE001
        _log(f"uninstall: failed to restore consecutive setting ({exc!r}); "
             f"original value still in .plugin_state.json: {original!r}")
        return {
            "restored": False,
            "previous_value": int(original),
            "error": repr(exc),
        }

    # Clear the entry so a future install can record a fresh original.
    state.pop("framework_consecutive_original", None)
    state.pop("framework_consecutive_applied", None)
    _save_state(state)

    return {
        "restored": True,
        "previous_value": int(original),
    }


def install() -> None:
    """Called by Agent Zero after the plugin has been copied into place."""
    _log("install() - preparing plugin")

    (PLUGIN_DIR / "stats").mkdir(parents=True, exist_ok=True)

    # No core patch required in v0.4.0. The @extension.extensible +
    # _functions/.../end hooks provide the repair seam.
    applied = apply_core_patch()

    floor_result = _install_consecutive_floor()

    state = {
        "installed": True,
        "version": PLUGIN_VERSION,
        "core_patch_applied": applied,
        "architecture": "v2.5_extensible_end_hooks",
    }
    if floor_result is not None:
        state["consecutive_floor"] = floor_result
    _save_state(state)
    _log(f"install() complete (version {PLUGIN_VERSION}, "
         f"consecutive_floor={floor_result})")


def pre_update() -> dict[str, Any]:
    """Called immediately before the plugin is updated."""
    state = _load_state()
    state["snapshot_at"] = "snapshot"
    return state


def uninstall() -> None:
    """Called just before the plugin directory is removed."""
    _log("uninstall() - cleaning up")
    revert_core_patch()
    floor_result = _uninstall_consecutive_floor()
    if floor_result is not None:
        _log(f"uninstall: consecutive setting restored: {floor_result}")
    for filename in (".toggle-0", ".toggle-1", ".plugin_state.json"):
        path = PLUGIN_DIR / filename
        if path.exists():
            try:
                path.unlink()
            except Exception as exc:  # noqa: BLE001
                _log(f"warning: could not remove {filename}: {exc}")
    _log("uninstall() complete")
