"""Layer 3 - system prompt injection for misformat_guard.

Appends a "Quoting rules" block to the system prompt on every turn, but
ONLY when the plugin is enabled. The block is de-duped per run via a
marker string in the prompt, so re-firing on every turn is safe.

The text is read from prompts/quote_rules.md so users can edit it
without touching Python. The block instructs the LLM to avoid emitting
unescaped " inside string values, which is the dominant misformat cause.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from helpers.extension import Extension

from usr.plugins.misformat_guard.api import misformat_config


# Path to the plugin root (three levels up from this file:
#   extensions/python/system_prompt/_10_quote_rules.py  -> this file
#   system_prompt/                                       -> 1 up
#   python/                                              -> 2 up
#   extensions/                                          -> 3 up
#   <plugin_root>                                        -> 4 up
PLUGIN_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


MARKER = '<misformat_guard quoting_rules="1">'


def _read_quote_rules(agent: Any | None) -> str:
    cfg = misformat_config.get_config(agent)
    rel = cfg.get("quote_rules_path") or "prompts/quote_rules.md"
    path = os.path.join(PLUGIN_DIR, rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip()
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001
        return ""


class QuoteRulesInjector(Extension):
    async def execute(
        self,
        system_prompt: list | None = None,
        loop_data: Any = None,
        **kwargs: Any,
    ):
        if not self.agent:
            return
        if system_prompt is None:
            system_prompt = []

        cfg = misformat_config.get_config(self.agent)
        if not cfg.get("enabled", True):
            return
        if not cfg.get("quote_rules_enabled", True):
            return

        # De-dupe: do not append twice in the same prompt build.
        if any(MARKER in (s or "") for s in system_prompt):
            return

        rules_text = _read_quote_rules(self.agent)
        if not rules_text:
            return

        block = f"{MARKER}\n{rules_text}\n</misformat_guard>"
        system_prompt.append(block)
