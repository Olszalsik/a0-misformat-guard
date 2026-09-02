"""Layer 3b - rewrite the misformat warning text in history.

When the core fires a misformat warning (via self.hist_add_warning ->
self.hist_add_message), the message that ends up in history is
`prompts/fw.msg_misformat.md`. That text doesn't tell the LLM the
common cause, so the model tends to re-emit the same broken response.
This hook intercepts the warning content and appends a one-liner naming
the dominant cause.

Extension point: hist_add_before
"""

from __future__ import annotations

from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config


CLARIFY_FRAGMENT = (
    "\n\n(misformat_guard) The most common cause is an unescaped "
    'double-quote `"` inside a long `text`/`message` value. Re-emit '
    "the response with inner quotes replaced by `'` or escaped as `\\\"`."
)


def _extract_text(content: Any) -> str | None:
    """Return a plain-text view of a MessageContent value, or None.

    hist_add_warning stores fw.warning.md's parsed JSON template, whose
    only text key is "system_warning" (v0.5.2 fix -- without it this
    hook never fired on real warnings).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("system_warning", "content", "message", "text"):
            v = content.get(key)
            if isinstance(v, str):
                return v
    return None


def _set_text(content: Any, new_text: str) -> Any:
    if isinstance(content, str):
        return new_text
    if isinstance(content, dict):
        for key in ("system_warning", "content", "message", "text"):
            if key in content and isinstance(content[key], str):
                content[key] = new_text
                return content
    return content


class ClarifyMisformatWarning(Extension):
    def execute(self, content_data: dict | None = None, ai: bool = False, **kwargs: Any):
        if not self.agent:
            return
        if content_data is None or not isinstance(content_data, dict):
            return
        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        if not cfg.get("clarify_misformat_warning", True):
            return

        content = content_data.get("content")
        text = _extract_text(content)
        if text is None:
            return
        # Only fire on the misformat warning text. Detect by a stable
        # substring from the upstream prompt.
        if "misformatted your message" not in text.lower():
            return
        if "(misformat_guard)" in text:
            return  # already rewritten this turn
        new_text = text + CLARIFY_FRAGMENT
        content_data["content"] = _set_text(content, new_text)
