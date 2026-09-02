"""Stream-buffer companion - buffer the latest streamed response text.

The upstream `response_stream_end` extension point does NOT receive the
final response text as a kwarg; it only receives `loop_data`. This hook
mirrors the `last_response_stream_full` value from agent.py:454 into
`loop_data.params_temporary["_misformat_guard_stream_full"]` so the
process_tools safety-net cascade can use it as a FALLBACK repair input
when the call args are unusable (v0.5.2 made the args the primary
repair input; v0.6.0 removed the old Layer 3a consumer).

The hook is a thin no-op when the plugin or the safety net is disabled.
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
        # The buffer's only consumer is the process_tools safety net, so
        # gate on that toggle (v0.6.0; previously gated on the removed
        # repair_enabled Layer 3a key).
        if not cfg.get("process_tools_fallback", True):
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
