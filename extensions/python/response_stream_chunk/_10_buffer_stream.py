"""Layer 2 (companion) - buffer the latest streamed response text.

The upstream `response_stream_end` extension point does NOT receive the
final response text as a kwarg; it only receives `loop_data`. This hook
mirrors the `last_response_stream_full` value from agent.py:454 into
`loop_data.params_temporary["_misformat_guard_stream_full"]` so that
`_10_repair_response.py` can read it at the end of the stream.

The hook is a thin no-op when the plugin is disabled.
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config


KEY = "_misformat_guard_stream_full"


class StreamBuffer(Extension):
    async def execute(self, loop_data: Any = None, stream_data: dict | None = None, **kwargs: Any):
        if not self.agent:
            return
        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        # Default mirrors default_config.yaml (repair_enabled: false);
        # get_plugin_config does not merge defaults into config.json.
        if not cfg.get("repair_enabled", False):
            return
        if loop_data is None or not isinstance(stream_data, dict):
            return
        full = stream_data.get("full")
        if not isinstance(full, str) or not full:
            return
        params = getattr(loop_data, "params_temporary", None)
        if params is None:
            return
        params[KEY] = full
