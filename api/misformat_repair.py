"""Repair entry points for the misformat_guard plugin.

Public surface:

    is_misformat(text) -> bool
        - True if the framework's core JSON tool extractor cannot parse
          `text`. Cheap; no LLM call. Used by the cascade hooks to
          short-circuit the happy path.

    try_repair(text) -> tuple[str | None, bool]
        - If `text` is already valid, returns (None, False).
        - If the vendored hardened parser can repair it, returns
          (repaired, True).
        - Otherwise returns (None, False). The caller falls through to
          the utility-model cascade or to the framework's misformat
          warning.

    try_repair_via_utility(agent, text) -> tuple[str | None, bool]
        - Async. Calls agent.call_utility_model (the cheap model) to
          repair `text` into valid JSON. Bounded by cascade.timeout_s via
          asyncio.wait_for. Strips surrounding markdown fences. Validates
          the result with the core JSON tool extractor. Never raises;
          any failure (timeout, exception, invalid JSON after strip)
          returns (None, False). The agent must never stall.

All functions are intentionally pure or near-pure: they do not mutate
state, do not log to the agent's context, and do not depend on the
agent beyond the utility call itself. Callers (the extension hooks)
handle agent-specific concerns (stat recording, loop_data state, etc.).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Tuple

PLUGIN_DIR = Path(__file__).resolve().parent.parent

# Use a try/except so the plugin can be imported even if the venv has
# the helpers package at a different path. The hardened parser is a
# vendored copy and does not depend on helpers.dirty_json.
try:
    from usr.plugins.misformat_guard.vendor.hardened_dirty_json import DirtyJson
    from helpers import extract_tools
except Exception:  # noqa: BLE001
    DirtyJson = None  # type: ignore[assignment]
    extract_tools = None  # type: ignore[assignment]


def _print(msg: str) -> None:
    sys.stderr.write(f"[misformat_guard:repair] {msg}\n")
    sys.stderr.flush()


def _core_parses(text: str) -> bool:
    if extract_tools is None:
        return False
    try:
        return extract_tools.json_parse_dirty(text) is not None
    except Exception:  # noqa: BLE001
        return False


def is_misformat(text: str) -> bool:
    """Return True if `text` cannot be parsed as a tool request.

    Used by the cascade hooks as a cheap pre-check before invoking the
    utility model. False positives are acceptable (we'd just call the
    utility model unnecessarily); false negatives are not (we'd let a
    bad response through).
    """
    if not text or extract_tools is None:
        return True
    try:
        parsed = extract_tools.json_parse_dirty(text)
    except Exception:  # noqa: BLE001
        return True
    if parsed is None or not isinstance(parsed, dict):
        return True
    try:
        extract_tools.normalize_tool_request(parsed)
    except Exception:  # noqa: BLE001
        return True
    return False


def try_repair(text: str) -> Tuple[str | None, bool]:
    """Attempt to repair `text` using the vendored hardened parser.

    Returns (repaired_text_or_None, was_repaired).
    """
    if not text or DirtyJson is None:
        return None, False
    if _core_parses(text):
        # Happy path: the core parser already handles this. Don't pay the
        # hardened-parser cost.
        return None, False
    try:
        result = DirtyJson.parse_string(text)
    except Exception:  # noqa: BLE001
        return None, False
    if result is None:
        return None, False
    try:
        repaired = json.dumps(result, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return None, False
    if not _core_parses(repaired):
        return None, False
    return repaired, True


# ---------------------------------------------------------------------------
# Utility-model cascade
# ---------------------------------------------------------------------------

DEFAULT_REPAIR_PROMPT = (
    "You are a JSON repair specialist. Take the broken response and return "
    "ONLY valid JSON with two keys: tool (string) and tool_args (object). "
    "If unrecoverable, return "
    '{"tool": "response_tool", "tool_args": {"text": "<<unrecoverable: brief reason>>"}}.'
)


def _resolve_system_prompt(agent: Any | None) -> str:
    """Resolve the system prompt for the utility repair call.

    Precedence (highest first):
      1. config.cascade.system_prompt_path  (file relative to PLUGIN_DIR)
      2. the default prompts/utility_repair.md shipped with the plugin
      3. DEFAULT_REPAIR_PROMPT (last-resort inline string)
    """
    try:
        from usr.plugins.misformat_guard.api import misformat_config
        cfg = misformat_config.get_config(agent)
    except Exception:  # noqa: BLE001
        cfg = {}
    cascade = cfg.get("cascade") if isinstance(cfg, dict) else None
    if isinstance(cascade, dict):
        rel = cascade.get("system_prompt_path")
        if rel:
            try:
                path = PLUGIN_DIR / rel
                if path.is_file():
                    return path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    # Default
    default_path = PLUGIN_DIR / "prompts" / "utility_repair.md"
    try:
        if default_path.is_file():
            return default_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_REPAIR_PROMPT


def _resolve_timeout_s(agent: Any | None) -> float:
    try:
        from usr.plugins.misformat_guard.api import misformat_config
        cfg = misformat_config.get_config(agent)
    except Exception:  # noqa: BLE001
        cfg = {}
    cascade = cfg.get("cascade") if isinstance(cfg, dict) else None
    if isinstance(cascade, dict):
        try:
            return max(1.0, float(cascade.get("timeout_s", 30) or 30))
        except Exception:  # noqa: BLE001
            return 30.0
    return 30.0


def _strip_code_fences(text: str) -> str:
    """Strip surrounding ```json ... ``` fences if present.

    Some small utility models wrap JSON in fences despite instructions.
    The strip is intentionally narrow: only strips if the first non-blank
    line begins with ``` and the last non-blank line is exactly ```.
    """
    if not text:
        return text
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if not lines or not lines[0].lstrip().startswith("```"):
        return s
    # Drop the opening fence line
    lines = lines[1:]
    # Drop the closing fence if present
    if lines and lines[-1].lstrip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def try_repair_via_utility(agent: Any, text: str) -> Tuple[str | None, bool]:
    """Call the utility model to repair `text` into valid JSON.

    Returns (repaired_text_or_None, ok).

    Never raises. On any failure (timeout, exception, invalid JSON after
    strip, agent has no call_utility_model, agent is None) returns
    (None, False). The caller should let the framework's existing
    misformat warning fire and the LLM retry on its own -- same as
    today, no regression.
    """
    if not text or agent is None:
        return None, False
    call = getattr(agent, "call_utility_model", None)
    if call is None:
        return None, False
    system = _resolve_system_prompt(agent)
    user_msg = "Repair this broken response into valid JSON: " + text
    timeout_s = _resolve_timeout_s(agent)
    try:
        repaired = await asyncio.wait_for(
            call(system=system, message=user_msg, background=False),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        _print("utility model call timed out after " + str(timeout_s) + "s")
        return None, False
    except Exception as exc:  # noqa: BLE001
        _print("utility model call raised: " + repr(exc))
        return None, False
    if not isinstance(repaired, str) or not repaired:
        return None, False
    cleaned = _strip_code_fences(repaired)
    # Look up is_misformat defensively at call time, not as a module
    # global. This way a stale .pyc that overwrites this module without
    # the symbol still works (we fall back to a local core-parser check).
    check = globals().get("is_misformat")
    if check is None:
        check = _core_parses  # False means "not parseable" = misformat
        if not check(cleaned):
            return None, False
    else:
        if check(cleaned):
            return None, False
    return cleaned, True
