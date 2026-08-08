"""misformat_diagnose: agent-callable tool to inspect misformat history.

The agent (or the user, via the chat) can call this tool to:
  - show the recent misformat count for the current chat
  - dump the last few misformat warnings from history
  - test the hardened parser against a sample string
  - toggle the plugin on/off for the current chat

Actions:
  - stats       : return current counter snapshot
  - history     : return the most recent misformat warnings from history
  - test_parser : parse a user-supplied string with the hardened parser
  - reset_stats : clear the in-memory counters
"""

from __future__ import annotations

import json
from typing import Any

from helpers.tool import Tool, Response

from usr.plugins.misformat_guard.api import misformat_config, misformat_stats


def _ok(message: str, **data: Any) -> Response:
    payload = {"message": message, **data}
    return Response(message=json.dumps(payload, ensure_ascii=False, indent=2), break_loop=False)


def _err(message: str) -> Response:
    return Response(message=json.dumps({"error": message}, ensure_ascii=False), break_loop=False)


class MisformatDiagnose(Tool):
    async def execute(self, **kwargs) -> Response:
        action = (self.args.get("action") or "stats").strip().lower()
        cfg = misformat_config.get_config(agent=self.agent)

        if action == "stats":
            snap = misformat_stats.snapshot(self.agent)
            return _ok(
                "misformat_guard stats",
                enabled=bool(cfg.get("enabled", True)),
                threshold=int(cfg.get("threshold", 3) or 3),
                counters=snap,
            )

        if action == "history":
            # Walk the agent's history and surface recent warnings whose
            # content looks like a misformat warning.
            warnings: list[str] = []
            try:
                history = getattr(self.agent, "history", None)
                messages = getattr(history, "messages", None) if history else None
                for msg in (messages or []):
                    content = getattr(msg, "content", "")
                    if isinstance(content, str) and "misformatted your message" in content.lower():
                        warnings.append(content)
                        if len(warnings) >= 5:
                            break
            except Exception as exc:  # noqa: BLE001
                return _err(f"could not read history: {exc}")
            return _ok(
                "recent misformat warnings",
                count=len(warnings),
                warnings=warnings,
            )

        if action == "test_parser":
            sample = self.args.get("sample") or self.args.get("text") or ""
            if not isinstance(sample, str) or not sample:
                return _err("test_parser requires a 'sample' string argument")
            try:
                from usr.plugins.misformat_guard.vendor.hardened_dirty_json import (
                    DirtyJson as HardenedDirty,
                )
                from helpers.dirty_json import DirtyJson as UpstreamDirty
                from helpers import extract_tools
            except Exception as exc:  # noqa: BLE001
                return _err(f"parsers not available: {exc}")

            h_result = HardenedDirty.parse_string(sample)
            try:
                u_result = UpstreamDirty.parse_string(sample)
            except Exception as exc:  # noqa: BLE001
                u_result = f"ERROR: {exc}"
            core_ok = extract_tools.json_parse_dirty(sample) is not None
            return _ok(
                "parser test result",
                hardened_parsed=h_result is not None,
                upstream_parsed=u_result is not None if not isinstance(u_result, str) else False,
                core_parser_accepted=core_ok,
                hardened_value=h_result,
            )

        if action == "reset_stats":
            misformat_stats.reset()
            return _ok("counters reset")

        return _err(
            f"unknown action {action!r}; use stats, history, test_parser, or reset_stats"
        )
