"""v0.4.0 utility-model repair cascade for Agent Zero v2.5.

This is the PRIMARY cascade. It is an @extensible /end hook on
Agent.call_chat_model_turn. After the chat model returns, this hook:

  1. Detects whether the response is parseable by the core JSON tool
     extractor (cheap, no LLM call).
  2. If not parseable AND the consecutive-misformat streak is at or
     above the configured trigger, calls agent.call_utility_model to
     ask the cheap model to repair the response into valid JSON.
  3. If the utility model returns a parseable response, rewrites
     data["result"] so the monologue loop sees a clean LLMResult.
  4. If the utility model fails, leaves data["result"] alone. The
     framework's existing misformat warning will fire and the chat
     model will retry on its own -- same as today, no regression.

The agent NEVER stalls. The agent NEVER aborts. The cascade is bounded
by cascade.max_per_streak and cascade.max_total_per_chat so a truly
broken chat model cannot burn the utility budget.

Why this hook point: Agent.call_chat_model_turn (defined in agent.py
near line 914) is what the monologue loop calls on every turn. The
function is decorated @extension.extensible, which wraps it with a
`data` payload whose `result` we can rewrite. (call_chat_model itself
is also @extensible but is NOT called from the monologue loop --
call_chat_model_turn goes straight to model.unified_turn, so a
call_chat_model/end hook would never fire.)
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


STREAK_ATTR = "consecutive_misformats"
STREAK_PARAM_KEY = "_mg_streak"
USED_STREAK_KEY = "_misformat_guard_cascade_used_in_streak"
USED_TOTAL_KEY = "_misformat_guard_cascade_used_total"


def _print(msg: str) -> None:
    sys.stderr.write("[misformat_guard:cascade] " + msg + "\n")
    sys.stderr.flush()


def _get_streak(loop_data: Any) -> int:
    """Read the misformat streak from loop_data.

    Prefer the framework attribute (if a future version of Agent Zero
    exposes it); fall back to the plugin's own params_temporary counter
    (maintained by monologue_start and message_loop_prompts_after).
    """
    if loop_data is None:
        return 0
    val = getattr(loop_data, STREAK_ATTR, None)
    if isinstance(val, int):
        return val
    params = getattr(loop_data, "params_temporary", None)
    if isinstance(params, dict):
        try:
            return int(params.get(STREAK_PARAM_KEY, 0) or 0)
        except Exception:  # noqa: BLE001
            return 0
    return 0


def _get_params(loop_data: Any) -> dict:
    if loop_data is None:
        return {}
    params = getattr(loop_data, "params_temporary", None)
    return params if isinstance(params, dict) else {}


def _extract_response_text(data: dict) -> str:
    """Pull the LLM response string out of data['result'].

    data['result'] for call_chat_model_turn is the LLMResult object.
    We accept the object form (.response) and the legacy tuple form
    (response, reasoning) so the hook is robust to a future framework
    change.
    """
    result = data.get("result")
    if result is None:
        return ""
    # tuple form
    if isinstance(result, tuple) and result:
        first = result[0]
        return first if isinstance(first, str) else ""
    # LLMResult-like object
    return getattr(result, "response", "") or ""


def _extract_reasoning_text(data: dict) -> str:
    result = data.get("result")
    if result is None:
        return ""
    if isinstance(result, tuple) and len(result) > 1:
        r = result[1]
        return r if isinstance(r, str) else ""
    return getattr(result, "reasoning", "") or ""


def _build_new_result(data: dict, repaired_text: str) -> Any:
    """Build a new LLMResult-like object with the repaired response.

    We try to import the framework's LLMResult class and use it so
    downstream code keeps type-checker / dataclass compatibility. If
    the import fails, we fall back to the same shape (tuple) the
    framework already accepts on the way in.
    """
    reasoning = _extract_reasoning_text(data)
    try:
        from helpers.llm import LLMResult  # type: ignore
        return LLMResult(response=repaired_text, reasoning=reasoning)
    except Exception:  # noqa: BLE001
        try:
            from agent import LLMResult  # type: ignore
            return LLMResult(response=repaired_text, reasoning=reasoning)
        except Exception:  # noqa: BLE001
            return (repaired_text, reasoning)


class CascadeUtilityRepair(Extension):
    async def execute(self, data: Any = None, **kwargs: Any):
        if not self.agent or not isinstance(data, dict):
            return
        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        if not cfg.get("primary_cascade_enabled", True):
            return
        cascade = cfg.get("cascade") if isinstance(cfg, dict) else None
        # Inline default mirrors default_config.yaml (mode: utility_repair).
        if not isinstance(cascade, dict) or cascade.get("mode", "utility_repair") != "utility_repair":
            return

        response_text = _extract_response_text(data)
        if not response_text:
            return

        # Defensive: a stale .pyc or a partial reload of the api module
        # could mean is_misformat is missing. Fall back to the core
        # parser check so the cascade still no-ops correctly instead
        # of crashing through handle_exception.
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

        if not is_misformat(response_text):
            # Happy path: the chat model emitted valid JSON. No utility
            # call. No stat increment.
            return

        # Count the attempt unconditionally so the UI can show
        # attempts vs outcomes.
        misformat_stats.record_cascade_attempt(self.agent)

        # Streak gate
        loop_data = getattr(self.agent, "loop_data", None)
        streak = _get_streak(loop_data)
        trigger = int(cascade.get("trigger", 1) or 1)
        if streak < trigger:
            return

        # Budget gate
        params = _get_params(loop_data)
        used_streak = int(params.get(USED_STREAK_KEY, 0) or 0)
        used_total = int(params.get(USED_TOTAL_KEY, 0) or 0)
        max_per_streak = int(cascade.get("max_per_streak", 2) or 2)
        max_total = int(cascade.get("max_total_per_chat", 6) or 6)
        if used_streak >= max_per_streak or used_total >= max_total:
            if cfg.get("verbose", False):
                _print(
                    "cascade budget exhausted: streak="
                    + str(used_streak)
                    + "/"
                    + str(max_per_streak)
                    + " total="
                    + str(used_total)
                    + "/"
                    + str(max_total)
                )
            return

        # Call the utility model. Never raises; returns (None, False) on
        # any failure so the agent never stalls. If the api module is
        # stale, fall back to a no-op so the original response goes
        # through unchanged.
        try_repair_via_utility = getattr(
            misformat_repair, "try_repair_via_utility", None
        )
        if try_repair_via_utility is None:
            misformat_stats.record_cascade_failure(self.agent)
            return
        repaired, ok = await try_repair_via_utility(self.agent, response_text)
        if not ok or repaired is None:
            misformat_stats.record_cascade_failure(self.agent)
            if cfg.get("verbose", False):
                _print("utility model did not produce a parseable repair")
            return

        # Substitute the repaired response into data['result'].
        data["result"] = _build_new_result(data, repaired)
        params[USED_STREAK_KEY] = used_streak + 1
        params[USED_TOTAL_KEY] = used_total + 1
        misformat_stats.record_cascade_repair(self.agent)
        if cfg.get("verbose", False):
            _print(
                "cascade repair substituted: streak="
                + str(used_streak + 1)
                + "/"
                + str(max_per_streak)
                + " total="
                + str(used_total + 1)
                + "/"
                + str(max_total)
            )
