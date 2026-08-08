"""GET the merged plugin config, or POST overrides to persist them.

GET  -> { ok, config, defaults, stored_keys, source }
POST -> body is a JSON object; its keys are merged into the framework
        plugin config and persisted. The framework's helpers.plugins
        handles the per-project / per-agent / global precedence chain
        and the on-disk write.

Config keys accepted (all optional; omitted keys are not changed):
  enabled, threshold, repair_enabled, repair_only_on_misformat,
  quote_rules_enabled, clarify_misformat_warning, stats_enabled,
  cascade.mode, cascade.trigger, cascade.max_per_streak,
  cascade.max_total_per_chat, cascade.timeout_s, cascade.system_prompt
"""

from datetime import datetime, timezone
from helpers.api import ApiHandler
from helpers import plugins as plugins_helper
from usr.plugins.misformat_guard.api import misformat_config


PLUGIN_NAME = "misformat_guard"

# Top-level scalar keys (cascade is a sub-dict, handled separately).
_SCALAR_KEYS = (
    "enabled",
    "threshold",
    "repair_enabled",
    "repair_only_on_misformat",
    "quote_rules_enabled",
    "clarify_misformat_warning",
    "stats_enabled",
)


def _coerce(key, value):
    """Coerce the UI form value to the correct Python type."""
    if isinstance(value, bool):
        return value
    if key == "threshold":
        n = int(value)
        return max(1, min(10, n))
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
            plugins_helper.clear_plugin_cache([PLUGIN_NAME], python_change=False)
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
