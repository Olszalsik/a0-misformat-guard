"""v0.4.1 - keep the framework's cost circuit breaker from triggering on
cascade-handled misformats.

Agent Zero's framework has an upstream extension at
extensions/python/_functions/agent/Agent/hist_add_warning/end/
_90_stop_unusable_response_loop.py that watches hist_add_warning()
and, after max_consecutive_unusable_responses (default 2) consecutive
fw.msg_misformat.md or fw.msg_repeat.md warnings, raises
HandledException to stop the chat and protect the user from API burn.

The misformat_guard v0.4.0 primary cascade (call_chat_model_turn/end)
and safety net (process_tools/end) repair the broken chat-model
response so the framework never emits a misformat warning in the
first place. But when the cascade fails (utility model unreachable,
budget exhausted, or a semantically-broken-but-parseable response),
the framework does emit the warning -- and after 2 such warnings
the upstream circuit breaker stops the agent.

This hook sits at the same extension point and runs BEFORE the
upstream extension (filename sorts before _90_ lexicographically
because '_1' < '_9' in ASCII; the framework sorts hooks by file
basename via helpers/extension.py:_get_extension_classes). When the
warning is the misformat warning AND the plugin's reset_unusable_loop_on_warning
config is on, we reset the upstream's params_persistent counter to
{iteration: current, count: 0}. The upstream then sees count=0,
increments to 1, and 1 < max_consecutive_unusable_responses -- so
it does not raise. The warning is still in history (the LLM sees
it on the next iteration); only the cost circuit breaker's
bookkeeping is reset.

If the cascade is disabled, or the warning is not the misformat
warning, or the upstream's state has not been initialized yet, this
hook is a no-op.

The hook is sync (matches the upstream's sync pattern at the same
extension point). It is intentionally side-effect-light: one dict
write to loop_data.params_persistent, only when the conditions are
met. Never raises.
"""

from __future__ import annotations

import sys
from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config


# Must match the framework's upstream state key (see
# extensions/python/_functions/agent/Agent/hist_add_warning/end/
# _90_stop_unusable_response_loop.py:STATE_KEY).
UPSTREAM_STATE_KEY = "_unusable_response_failures"


def _print(msg: str) -> None:
    sys.stderr.write("[misformat_guard:consume] " + msg + "\n")
    sys.stderr.flush()


def _extract_message(data: dict) -> str | None:
    """Pull the warning message out of the hist_add_warning data payload.

    Mirrors the upstream's read order: kwargs first, then args[1].
    hist_add_warning is called as self.hist_add_warning(message), so
    args[1] is the message string. kwargs may carry message= when the
    caller used a keyword.
    """
    call_kwargs = data.get("kwargs")
    if isinstance(call_kwargs, dict):
        v = call_kwargs.get("message")
        if isinstance(v, str):
            return v
    call_args = data.get("args")
    if isinstance(call_args, tuple) and len(call_args) > 1:
        v = call_args[1]
        if isinstance(v, str):
            return v
    return None


class ConsumeMisformatWarning(Extension):
    def execute(self, data: Any = None, **kwargs: Any):
        if not self.agent or not isinstance(data, dict):
            return

        message = _extract_message(data)
        if not isinstance(message, str) or not message:
            return

        # Only act on the framework's misformat warning. The upstream's
        # filter at _90_stop_unusable_response_loop.py:22-26 also fires
        # on fw.msg_repeat.md, but we only consume misformats -- a
        # genuine repeat (same response emitted twice) is a real model
        # failure and should still count toward the upstream's budget.
        try:
            misformat_text = self.agent.read_prompt("fw.msg_misformat.md")
        except Exception:  # noqa: BLE001
            return
        if message != misformat_text:
            return

        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        if not cfg.get("reset_unusable_loop_on_warning", True):
            return

        loop_data = getattr(self.agent, "loop_data", None)
        if loop_data is None:
            return
        state = getattr(loop_data, "params_persistent", None)
        if not isinstance(state, dict):
            return
        iteration = getattr(loop_data, "iteration", None)
        if not isinstance(iteration, int):
            return

        # Reset the upstream's counter. The upstream will read this on
        # the SAME extension-point invocation (it runs after us by
        # basename sort) and see count=0, then increment to 1.
        state[UPSTREAM_STATE_KEY] = {"iteration": iteration, "count": 0}

        if cfg.get("verbose", False):
            _print(
                "reset upstream cost-circuit-breaker counter at iteration="
                + str(iteration)
            )
