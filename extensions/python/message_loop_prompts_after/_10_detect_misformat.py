from helpers.extension import Extension
from usr.plugins.misformat_guard.api.misformat_config import (
    CASCADE_USED_STREAK_KEY,
    CASCADE_USED_TOTAL_KEY,
)


def _warning_text(content) -> str | None:
    """Extract comparable text from a history MessageContent.

    hist_add_warning renders prompts/fw.warning.md, a FULL JSON template,
    so parse_prompt returns a dict {"system_warning": <text>} and the
    message content stored in history is that dict -- not a str. The
    v0.5.2 fix: without the dict branch the detector never matched and
    the primary cascade could never fire (the streak stayed at 0).
    Plain str content (ordinary messages) is compared directly.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        v = content.get("system_warning")
        if isinstance(v, str):
            return v
    return None


class DetectMisformat(Extension):
    def execute(self, loop_data=None, **kwargs):
        try:
            if not self.agent or loop_data is None:
                return
            # v0.6.0: the streak lives in params_persistent, NOT
            # params_temporary -- agent.py wipes params_temporary at the
            # start of EVERY message-loop iteration, so a streak there was
            # capped at 1 and a cascade trigger > 1 could never fire.
            # params_persistent survives across iterations (fresh per
            # monologue), which is exactly the streak's lifetime.
            params = getattr(loop_data, 'params_persistent', None)
            if not isinstance(params, dict):
                return
            history = getattr(self.agent, 'history', None)
            if history is None:
                return
            messages = getattr(history, 'messages', None)
            if not messages:
                params['_mg_streak'] = 0
                # A fresh context is a fresh streak: clear the per-streak
                # repair budget so `max_per_streak` means "per streak".
                params[CASCADE_USED_STREAK_KEY] = 0
                return
            last = messages[-1]
            text = _warning_text(getattr(last, 'content', ''))
            if text is None:
                # Unknown message shape (not a warning we can read):
                # leave the streak untouched, matching the pre-v0.5.2
                # behaviour for non-str content.
                return
            if 'misformatted your message' in text.lower():
                params['_mg_streak'] = int(params.get('_mg_streak', 0) or 0) + 1
            else:
                params['_mg_streak'] = 0
                # The misformat streak ended, so the per-streak repair budget
                # must reset with it. Without this, USED_STREAK_KEY was only
                # ever incremented, so `max_per_streak` silently degraded
                # into a per-monologue cap: after two repairs anywhere in a
                # monologue the cascade was dead for the rest of it, even
                # though the config describes it as per streak.
                #
                # CASCADE_USED_TOTAL_KEY is deliberately NOT reset here — it is
                # the `max_total_per_chat` bound and spans the whole monologue.
                params[CASCADE_USED_STREAK_KEY] = 0
        except Exception:
            pass