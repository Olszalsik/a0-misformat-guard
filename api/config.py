"""GET the merged plugin config, or POST overrides to persist them.

GET  -> { ok, config, defaults, stored_keys, source }
POST -> body is a JSON object; its keys are merged into the framework
        plugin config and persisted. The framework's helpers.plugins
        handles the per-project / per-agent / global precedence chain
        and the on-disk write.

Config keys accepted (all optional; omitted keys are not changed):
  enabled, quote_rules_enabled, clarify_misformat_warning, stats_enabled,
  cascade.mode, cascade.trigger, cascade.max_per_streak,
  cascade.max_total_per_chat, cascade.timeout_s, cascade.system_prompt,
  tool_repeat_guard_enabled, tool_repeat_warn_threshold,
  tool_repeat_stop_threshold, tool_repeat_action, tool_repeat_normalize_args

v0.6.0: the legacy `threshold` (v0.2.0 escape valve) and the Layer 3a
`repair_enabled` / `repair_only_on_misformat` keys are no longer
accepted -- those features were removed (the escape valve in v0.4.0,
Layer 3a in v0.6.0). Stale keys in an existing config.json are ignored
by get_config, so no migration is needed.
"""

from datetime import datetime, timezone
from helpers.api import ApiHandler
from helpers import plugins as plugins_helper
from usr.plugins.misformat_guard.api import misformat_config


PLUGIN_NAME = "misformat_guard"

# Top-level scalar keys (cascade is a sub-dict, handled separately).
_SCALAR_KEYS = (
    "enabled",
    "quote_rules_enabled",
    "clarify_misformat_warning",
    "stats_enabled",
    # Layer 5 (v0.5.0) tool-repeat guard. The list-valued keys
    # (tool_repeat_error_patterns, tool_repeat_ignored_tools) stay
    # advanced-only -- not UI-persisted here (documented in
    # default_config.yaml).
    "tool_repeat_guard_enabled",
    "tool_repeat_warn_threshold",
    "tool_repeat_stop_threshold",
    "tool_repeat_action",
    "tool_repeat_normalize_args",
)

# Valid actions for the tool-repeat guard.
_REPEAT_ACTIONS = ("warn", "stop", "warn_then_stop")


def _coerce(key, value):
    """Coerce the UI form value to the correct Python type."""
    if isinstance(value, bool):
        return value
    if key in ("tool_repeat_warn_threshold", "tool_repeat_stop_threshold"):
        # 0 disables that half of the guard -- respect it (do NOT clamp to 1).
        n = int(value)
        return max(0, min(20, n))
    if key == "tool_repeat_action":
        if value not in _REPEAT_ACTIONS:
            return "warn_then_stop"
        return value
    return value


def _flatten_for_persist(overrides):
    """Re-nest the flat dot-notation keys from the UI back into a dict
    suitable for update_plugin_config."""
    out = {}
    cascade_out = {}
    for k, v in (overrides or {}).items():
        if k.startswith("cascade."):
            cascade_out[k.split(".", 1)[1]] = v
        else:
            out[k] = v
    if cascade_out:
        out["cascade"] = cascade_out
    return out


class Config(ApiHandler):
    async def process(self, input_data, request):
        method = (getattr(request, "method", "POST") or "POST").upper()
        # Build the current effective config (defaults + stored overrides).
        try:
            defaults = misformat_config._load_default_from_disk()
        except Exception:
            defaults = {}
        try:
            stored = plugins_helper.get_plugin_config(PLUGIN_NAME) or {}
        except Exception:
            stored = {}

        if method == "GET" or not input_data:
            return {
                "ok": True,
                "config": misformat_config.get_config(None),
                "defaults": defaults,
                "stored_keys": sorted(stored.keys()) if isinstance(stored, dict) else [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # POST = persist overrides
        if not isinstance(input_data, dict):
            return {"ok": False, "error": "body must be a JSON object"}

        # Coerce types for known scalar keys
        cleaned = {}
        for k, v in input_data.items():
            if k in _SCALAR_KEYS:
                cleaned[k] = _coerce(k, v)
            else:
                cleaned[k] = v
        nested = _flatten_for_persist(cleaned)

        try:
            plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", nested)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"persist failed: {exc}",
                "stored": stored,
            }

        # Invalidate the framework's plugin config cache so the next
        # read returns the freshly-persisted values (helpers.plugins
        # caches get_plugin_config across requests).
        try:
            plugins_helper.clear_plugin_cache([PLUGIN_NAME])
        except Exception:
            pass
        try:
            misformat_config._DEFAULT_CACHE = None
        except Exception:
            pass

        return {
            "ok": True,
            "updated": sorted(cleaned.keys()),
            "config": misformat_config.get_config(None),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
