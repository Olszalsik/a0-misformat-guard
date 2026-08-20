from helpers.api import ApiHandler
from usr.plugins.misformat_guard.api import misformat_config, misformat_stats
from usr.plugins.misformat_guard.api import __version__ as PLUGIN_VERSION


def _int_or(cfg, key, default):
    """Read an int config value respecting 0 (the `or` fallback would
    coerce 0 -> default, which misreports a threshold the user disabled)."""
    v = cfg.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


class Health(ApiHandler):
    async def process(self, input_data, request):
        try:
            cfg = misformat_config.get_config(None)
            snap = misformat_stats.snapshot(None)
            cascade = cfg.get("cascade") or {}
            return {
                'ok': True,
                'version': PLUGIN_VERSION,
                'enabled': bool(cfg.get('enabled', True)),
                'primary_cascade_enabled': bool(cfg.get('primary_cascade_enabled', True)),
                'process_tools_fallback': bool(cfg.get('process_tools_fallback', True)),
                'reset_unusable_loop_on_warning': bool(
                    cfg.get('reset_unusable_loop_on_warning', True)
                ),
                'consecutive_unusable_floor': int(
                    cfg.get('consecutive_unusable_floor', 5) or 5
                ),
                'tool_repeat_guard_enabled': bool(
                    cfg.get('tool_repeat_guard_enabled', True)
                ),
                'tool_repeat_warn_threshold': _int_or(cfg, 'tool_repeat_warn_threshold', 2),
                'tool_repeat_stop_threshold': _int_or(cfg, 'tool_repeat_stop_threshold', 4),
                'tool_repeat_action': cfg.get('tool_repeat_action', 'warn_then_stop'),
                'cascade': {
                    'mode': cascade.get('mode', 'off'),
                    'trigger': int(cascade.get('trigger', 1) or 1),
                    'max_per_streak': int(cascade.get('max_per_streak', 2) or 2),
                    'max_total_per_chat': int(cascade.get('max_total_per_chat', 6) or 6),
                    'timeout_s': int(cascade.get('timeout_s', 30) or 30),
                },
                'counters': snap,
            }
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}
