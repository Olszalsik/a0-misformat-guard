from helpers.extension import Extension


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
            params = getattr(loop_data, 'params_temporary', None)
            if not isinstance(params, dict):
                return
            history = getattr(self.agent, 'history', None)
            if history is None:
                return
            messages = getattr(history, 'messages', None)
            if not messages:
                params['_mg_streak'] = 0
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
        except Exception:
            pass