"""v0.4.0 safety-net cascade for Agent Zero v2.5.

@extensible /end hook on Agent.process_tools. Catches the misformat
path in agent.py:1504-1512 where process_tools returns None because
the chat-model response could not be parsed as a tool request.

Why a safety net when we already have a primary cascade on
call_chat_model_turn/end? The primary cascade can miss in two cases:

  1. The utility model returned a syntactically valid JSON object but
     one that normalize_tool_request rejects (missing 'tool' / 'tool_args'
     keys, or wrong types). The is_misformat check passes because
     json_parse_dirty succeeded, but process_tools still falls into the
     misformat-else-branch.

  2. The primary cascade was rate-limited out (max_per_streak or
     max_total_per_chat reached) but the misformat is still happening.

In both cases the monologue loop will see a misformat warning and the
LLM will retry -- but we can do better. This hook:

  1. Detects data['result'] is None AND data['exception'] is None (the
     misformat path, not an exception path).
  2. Checks the ACTUAL message the framework passed to process_tools
     (v0.5.1: not the stream buffer -- the buffer is a truncated prefix
     whenever extraction succeeded mid-stream, which would misfire on
     every ordinary successful tool call). If the message parses as a
     tool request, this was a normal dispatch and we return.
  3. Calls the utility model to repair the unparseable message. If the
     repair is parseable, re-invokes the framework's process_tools with
     the repaired text.
  4. Sets data['result'] to the result of the re-invocation, masking
     the original None. The monologue loop sees a successful tool
     dispatch and continues.

The re-invocation goes through the @extensible wrapper, so this /end
hook runs again for it; a REENTRY_KEY flag in params_temporary bounds
it to a single level, and the cascade budget counters are incremented
before the utility call so failed attempts are bounded too.

Like the primary cascade, this NEVER stalls. If the utility model
fails, the original None propagates and the framework's existing
misformat warning fires -- same as today, no regression.
"""

from __future__ import annotations

import sys
from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import (
    misformat_config,
    misformat_repair,
    misformat_stats,
)


STREAM_KEY = "_misformat_guard_stream_full"
USED_STREAK_KEY = "_misformat_guard_cascade_used_in_streak"
USED_TOTAL_KEY = "_misformat_guard_cascade_used_total"
REENTRY_KEY = "_misformat_guard_fallback_active"


def _print(msg: str) -> None:
    sys.stderr.write("[misformat_guard:fallback] " + msg + "\n")
    sys.stderr.flush()


def _get_params(loop_data: Any) -> dict:
    if loop_data is None:
        return {}
    params = getattr(loop_data, "params_temporary", None)
    return params if isinstance(params, dict) else {}


class ProcessToolsFallback(Extension):
    async def execute(self, data: Any = None, **kwargs: Any):
        if not self.agent or not isinstance(data, dict):
            return

        # Only act on the misformat-else-branch (data['result'] is None
        # and no exception is pending). Happy path (tool ran fine) and
        # exception path are not our concern.
        if data.get("exception") is not None:
            return
        if data.get("result") is not None:
            return

        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        if not cfg.get("process_tools_fallback", True):
            return
        cascade = cfg.get("cascade") if isinstance(cfg, dict) else None
        if not isinstance(cascade, dict) or cascade.get("mode", "off") != "utility_repair":
            return

        loop_data = getattr(self.agent, "loop_data", None)
        params = _get_params(loop_data)

        # v0.5.1 fix: decide from the ACTUAL message the framework passed
        # to process_tools, not from the stream buffer. The buffer is a
        # stale prefix whenever the tool extractor succeeded mid-stream
        # (the framework early-returns before the chunk hook runs on the
        # final chunk), so buffer-based checks misfire on every ordinary
        # successful tool call and would re-execute the tool.
        call_args = data.get("args")
        msg = (
            call_args[0]
            if isinstance(call_args, tuple) and call_args
            and isinstance(call_args[0], str)
            else None
        )

        # Buffered stream text (response_stream_chunk/_10_buffer_stream.py).
        # Only used as the repair input when the call args are unusable.
        stream_text = params.get(STREAM_KEY)
        if not isinstance(stream_text, str) or not stream_text:
            if not isinstance(msg, str) or not msg:
                return

        # Defensive: a stale .pyc or partial reload could mean
        # is_misformat is missing. Use a local fallback in that case
        # so the cascade never crashes through handle_exception.
        is_misformat = getattr(misformat_repair, "is_misformat", None)
        if is_misformat is None:
            try:
                from helpers import extract_tools as _et
                def _cheap_is_misformat(text: str) -> bool:
                    try:
                        parsed = _et.json_parse_dirty(text)
                        if parsed is None or not isinstance(parsed, dict):
                            return True
                        _et.normalize_tool_request(parsed)
                        return False
                    except Exception:  # noqa: BLE001
                        return True
                is_misformat = _cheap_is_misformat
            except Exception:  # noqa: BLE001
                return  # can't determine; let the framework handle it

        # The real misformat signal is the message the framework itself
        # could not parse. If that message parses as a tool request, this
        # was an ordinary successful dispatch (process_tools returns None
        # whenever the executed tool does not break the loop) -- there is
        # nothing to repair, and re-invoking would run the tool twice.
        if isinstance(msg, str) and not is_misformat(msg):
            return

        # Repair input: prefer the actual full message. The stream buffer
        # is a truncated mid-stream prefix (the framework stops calling
        # the chunk hook once extraction succeeds), so it is only a
        # fallback if the args shape ever changes.
        repair_text = msg if isinstance(msg, str) and msg else stream_text

        misformat_stats.record_cascade_attempt(self.agent)

        # Budget gate (shared with primary cascade)
        used_streak = int(params.get(USED_STREAK_KEY, 0) or 0)
        used_total = int(params.get(USED_TOTAL_KEY, 0) or 0)
        max_per_streak = int(cascade.get("max_per_streak", 2) or 2)
        max_total = int(cascade.get("max_total_per_chat", 6) or 6)
        if used_streak >= max_per_streak or used_total >= max_total:
            if cfg.get("verbose", False):
                _print(
                    "fallback budget exhausted: streak="
                    + str(used_streak)
                    + "/"
                    + str(max_per_streak)
                    + " total="
                    + str(used_total)
                    + "/"
                    + str(max_total)
                )
            return
        # v0.5.1 fix: count the attempt BEFORE the utility call, not only
        # on success -- otherwise failed attempts never consume budget.
        params[USED_STREAK_KEY] = used_streak + 1
        params[USED_TOTAL_KEY] = used_total + 1

        # Re-entry guard: the re-invocation below goes through the same
        # @extensible wrapper, so this /end hook runs again for it. On a
        # nested pass, bail out immediately (its own msg will parse and
        # return early above; this guard covers any residual path).
        if params.get(REENTRY_KEY):
            return

        # Call the utility model. Never raises. If the api module is
        # stale, fall back to no-op so the original None stands.
        try_repair_via_utility = getattr(
            misformat_repair, "try_repair_via_utility", None
        )
        if try_repair_via_utility is None:
            misformat_stats.record_cascade_failure(self.agent)
            return
        repaired, ok = await try_repair_via_utility(self.agent, repair_text)
        if not ok or repaired is None:
            misformat_stats.record_cascade_failure(self.agent)
            if cfg.get("verbose", False):
                _print("fallback utility model did not produce a parseable repair")
            return

        # Re-invoke process_tools with the repaired text. This re-enters
        # the @extensible wrapper (and this hook); the REENTRY_KEY above
        # bounds it to a single level.
        process_tools = getattr(self.agent, "process_tools", None)
        if process_tools is None:
            return
        params[REENTRY_KEY] = True
        try:
            new_result = await process_tools(repaired)
        except Exception as exc:  # noqa: BLE001 - never make it worse
            misformat_stats.record_cascade_failure(self.agent)
            if cfg.get("verbose", False):
                _print("fallback re-invocation raised: " + repr(exc))
            return
        finally:
            params[REENTRY_KEY] = False

        if new_result is None:
            # Repaired text still failed. Let the original None stand
            # so the framework's misformat warning fires and the LLM
            # retries.
            misformat_stats.record_cascade_failure(self.agent)
            if cfg.get("verbose", False):
                _print("fallback re-invocation returned None -- original misformat stands")
            return

        data["result"] = new_result
        misformat_stats.record_cascade_repair(self.agent)
        if cfg.get("verbose", False):
            _print("fallback repair succeeded, re-invocation returned non-None")
