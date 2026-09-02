"""Layer 2 - response repair on response_stream_end.

Reads the buffered response text (placed there by the response_stream_chunk
hook), tries the hardened parser via api.misformat_repair, and - if a
repair is found - stashes the repaired text in
`loop_data.params_temporary["_misformat_guard_repaired_response"]` for the
core process_tools path to pick up.

The core patch we ship (usr/patches/misformat_guard_core.patch) does the
final substitution. If the patch is not applied, this extension is a
no-op for the agent (the repair is computed but not used) and the
existing escape valve catches the misformat. This keeps the plugin safe
to install even if the patch is somehow not yet applied.
"""

from __future__ import annotations

import sys
from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config, misformat_repair, misformat_stats


BUFFER_KEY = "_misformat_guard_stream_full"
REPAIR_KEY = "_misformat_guard_repaired_response"
ABORT_MSG_KEY = "_misformat_guard_abort_msg"


def _print(msg: str) -> None:
    sys.stderr.write(f"[misformat_guard] {msg}\n")
    sys.stderr.flush()


class RepairResponse(Extension):
    async def execute(self, loop_data: Any = None, **kwargs: Any):
        if not self.agent:
            return
        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        # Default mirrors default_config.yaml (repair_enabled: false);
        # get_plugin_config does not merge defaults into config.json.
        if not cfg.get("repair_enabled", False):
            return
        if loop_data is None:
            return

        params = getattr(loop_data, "params_temporary", None)
        if not isinstance(params, dict):
            return

        full_text = params.get(BUFFER_KEY)
        if not isinstance(full_text, str) or not full_text:
            return

        # Cheap path: only attempt the repair when the core parser already
        # failed. This keeps the happy path at one parse call.
        if cfg.get("repair_only_on_misformat", True):
            try:
                from helpers import extract_tools
                if extract_tools.json_parse_dirty(full_text) is not None:
                    return
            except Exception:  # noqa: BLE001
                pass

        repaired, ok = misformat_repair.try_repair(full_text)
        if not ok:
            misformat_stats.record_repair_failure(self.agent)
            if cfg.get("verbose", False):
                _print("hardened parser could not repair the response")
            return

        # Verify the repair is a real tool request (not a parsed but
        # semantically broken dict).
        try:
            from helpers import extract_tools
            normalized = extract_tools.normalize_tool_request(__import__("json").loads(repaired))
        except Exception:  # noqa: BLE001
            misformat_stats.record_repair_failure(self.agent)
            return

        params[REPAIR_KEY] = repaired
        misformat_stats.record_repair(self.agent)
        if cfg.get("verbose", False):
            _print("response repaired by hardened parser")
