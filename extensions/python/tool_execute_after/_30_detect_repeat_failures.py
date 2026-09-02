"""v0.5.0 tool-repeat guard -- the reasoning death-loop breaker.

A ``tool_execute_after`` hook that detects the agent re-emitting the
*same* tool call with the *same* arguments and getting an error result
each time -- the loop none of the framework's existing breakers catch:

  - The patch is a well-formed tool call, so no ``fw.msg_misformat.md``
    warning fires.
  - Each iteration's response differs (the tool error is appended to
    history), so the byte-identical check that fires
    ``fw.msg_repeat.md`` (agent.py:494) never triggers.
  - The tool error is a normal tool *result*, not a warning, so the
    upstream ``_90_stop_unusable_response_loop`` counter never
    increments and the agent loops forever, burning tokens.

Why ``tool_execute_after``: this is the only point where we see both
the executed args and the tool's response. ``tool_args`` is NOT passed
to the after-hook (unlike ``tool_execute_before``), so args are read
from ``loop_data.current_tool.args`` -- still populated in BOTH
execution paths (the ``finally`` that clears ``current_tool`` runs
*after* this hook; see agent.py `_execute_tool_request` ~1206-1226 and
`process_tools` ~1487-1500). If ``current_tool`` is None (a parallel-
tool race or an already-cleared slot), the hook no-ops: a safe miss,
never a false hit. Sort prefix ``_30_`` orders it after the framework's
own ``_10_mask_secrets`` so detection sees the final masked message.

State lives in ``loop_data.params_persistent`` (NOT
``params_temporary`` -- ``agent.py:408`` wipes ``params_temporary`` every
iteration, so a streak there would never accumulate).
``params_persistent`` survives across iterations and is fresh per
monologue: the right lifetime for "this task is stuck repeating".

Default action (``warn_then_stop``):
  - At ``warn_threshold`` (default 2): inject a corrective
    ``hist_add_warning`` (a ``{"system_warning": ...}`` history entry the
    model sees next turn) AND prepend a directive to ``response.message``
    so the model also sees it inline with the failure this turn. One-shot
    per sig-streak (``warned`` flag) -- no spam.
  - At ``stop_threshold`` (default 4): set ``response.break_loop = True``
    and rewrite ``response.message`` to a clear stop explanation.
    ``process_llm_result_tools`` returns it as the final result
    (agent.py:1222-1224 / 1497-1498), ending the turn cleanly -- no
    exception raised. This is the real backstop: a truly stuck model
    ignores soft warnings and would loop forever otherwise.

The hook NEVER raises. Every failure path is an early ``return`` leaving
``response`` untouched except the two intended mutations. A top-level
try/except guarantees a stale ``.pyc`` / partial reload degrades to a
no-op rather than crashing the tool loop.
"""

from __future__ import annotations

import sys
from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config, tool_repeat


def _print(msg: str) -> None:
    try:
        sys.stderr.write("[misformat_guard:tool-repeat] " + msg + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _short_error(message: str) -> str:
    """First non-empty line of the error message, truncated for the
    warning text so a giant traceback does not bloat the history entry."""
    try:
        stripped = message.strip()
        if not stripped:
            return ""
        first = stripped.splitlines()[0]
        return first[:160]
    except Exception:  # noqa: BLE001
        return ""


def _get_args(agent: Any) -> Any:
    """Recover the executed tool args from loop_data.current_tool.

    current_tool is still set during tool_execute_after in both execution
    paths (cleared in the finally *after* the hook). Returns None if it
    can't be read safely (parallel-tool race / already cleared) -- the
    caller treats None as "cannot track this call" and no-ops."""
    try:
        loop_data = getattr(agent, "loop_data", None)
        if loop_data is None:
            return None
        tool = getattr(loop_data, "current_tool", None)
        if tool is None:
            return None
        return getattr(tool, "args", None)
    except Exception:  # noqa: BLE001
        return None


_WARN_PREAMBLE = "[TOOL-REPEAT-GUARD] "
_STOP_PREAMBLE = "[TOOL-REPEAT-GUARD:STOP] "


class DetectRepeatFailures(Extension):
    async def execute(self, response: Any = None, tool_name: str = "", **kwargs: Any):
        try:
            if not self.agent:
                return

            cfg = misformat_config.get_config(self.agent)
            rc = tool_repeat.resolve_config(cfg)
            if not rc["enabled"] or not rc["guard_enabled"]:
                return

            # Defensive: a stale api module (cached .pyc from a previous
            # plugin version) could be missing one of these symbols. Fall
            # back to a no-op so the tool loop never crashes mid-flight.
            args_signature = getattr(tool_repeat, "args_signature", None)
            is_error_result = getattr(tool_repeat, "is_error_result", None)
            load_state = getattr(tool_repeat, "load_state", None)
            if args_signature is None or is_error_result is None or load_state is None:
                return

            if not tool_name or tool_name in rc["ignored_tools"]:
                return
            if response is None:
                return
            message = getattr(response, "message", None)
            if not isinstance(message, str):
                return

            args = _get_args(self.agent)
            if args is None:
                # Cannot capture args safely (parallel race / cleared
                # current_tool). A signature derived from unknown args
                # would risk false positives, so no-op for this call.
                return

            sig = args_signature(tool_name, args, rc["normalize_args"])
            is_error = is_error_result(message, rc["error_patterns"])

            loop_data = getattr(self.agent, "loop_data", None)
            state = load_state(loop_data)
            if state is None:
                # params_persistent unusable (not a dict / read-only) --
                # tracking would be lost across iterations, so no-op.
                return

            # --- update the streak ------------------------------------------
            if not is_error:
                # A non-error result means progress (the call worked, or at
                # worst produced neutral output). Reset the streak.
                state.clear()
                state.update(
                    {
                        "sig": sig,
                        "count": 0,
                        "warned": False,
                        "last_tool": tool_name,
                        "last_error": "",
                    }
                )
                return

            prev_sig = state.get("sig")
            if prev_sig == sig:
                state["count"] = int(state.get("count", 0) or 0) + 1
                # warned stays as-is for the same sig
            else:
                # A new (tool,args) is failing -- start a fresh streak.
                state["sig"] = sig
                state["count"] = 1
                state["warned"] = False
            state["last_tool"] = tool_name
            last_error = _short_error(message)
            state["last_error"] = last_error

            count = int(state.get("count", 0) or 0)
            action = rc["action"]
            warn_t = rc["warn_threshold"]
            stop_t = rc["stop_threshold"]

            # --- hard stop (stronger; checked first) ------------------------
            if stop_t > 0 and count >= stop_t and action in ("stop", "warn_then_stop"):
                stop_msg = (
                    _STOP_PREAMBLE
                    + "Stopped after "
                    + str(count)
                    + " consecutive identical failing '"
                    + str(tool_name)
                    + "' calls (last error: "
                    + (last_error or "unknown")
                    + "). The same tool was called with the same arguments "
                    + str(count)
                    + " times and failed each time. Re-read the target "
                    + "file/state and construct corrected arguments before "
                    + "continuing."
                )
                try:
                    response.message = stop_msg
                    response.break_loop = True
                except Exception:  # noqa: BLE001 - response not mutable
                    return
                # Reset the streak so a future call (if the loop somehow
                # continues) starts fresh.
                state.clear()
                state.update(
                    {
                        "sig": sig,
                        "count": 0,
                        "warned": False,
                        "last_tool": tool_name,
                        "last_error": "",
                    }
                )
                if rc["verbose"]:
                    _print("hard-stop at count=" + str(count) + " tool=" + str(tool_name))
                return

            # --- soft warn (one-shot per sig-streak) ------------------------
            if (
                warn_t > 0
                and count >= warn_t
                and action in ("warn", "warn_then_stop")
                and not state.get("warned")
            ):
                directive = (
                    "You have called '"
                    + str(tool_name)
                    + "' with IDENTICAL arguments "
                    + str(count)
                    + " times in a row and each call returned an error ("
                    + (last_error or "unknown")
                    + "). Repeating the exact same action will keep failing. "
                    + "Before retrying, RE-READ the current content of the "
                    + "target (use a view/read tool, or recall it) and change "
                    + "your arguments so they match the actual current state."
                )
                # Inline: the model sees this as part of the tool's own
                # output this turn.
                try:
                    response.message = _WARN_PREAMBLE + directive + "\n\n" + message
                except Exception:  # noqa: BLE001
                    pass
                # Separate system-level warning the model sees next turn.
                try:
                    self.agent.hist_add_warning(message=directive)
                except Exception:  # noqa: BLE001
                    pass
                state["warned"] = True
                if rc["verbose"]:
                    _print("warn at count=" + str(count) + " tool=" + str(tool_name))
                return
        except Exception:  # noqa: BLE001 - never raise out of a hook
            try:
                _print("unexpected error in tool-repeat hook (no-op)")
            except Exception:  # noqa: BLE001
                pass
            return