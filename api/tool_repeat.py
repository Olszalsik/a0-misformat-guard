"""Tool-repeat guard detection logic for misformat_guard v0.5.0.

Detects the "reasoning death-loop" the framework's existing breakers do
NOT catch: the agent re-emits the *same* tool call with the *same*
arguments, the tool executes and returns an *error result* (e.g.
``code_editor`` patch with a stale ``old_text`` -> ``"error patching
<path>: old_text not found"``), the model sees the error in history and
re-emits the identical call, dozens of times, silently burning tokens.

Why the framework's breakers miss it:

  - ``_90_stop_unusable_response_loop`` only counts ``fw.msg_misformat.md``
    / ``fw.msg_repeat.md`` *warnings*. This loop emits neither: the patch
    is a well-formed tool call (no misformat warning), and each iteration's
    response differs because the tool error is appended to history (no
    byte-identical repeat -> ``fw.msg_repeat.md`` never fires).
  - The ``old_text not found`` is a normal tool *result* fed back to the
    model, not a warning, so the breaker's counter never increments.

This module is the pure, framework-free detection half. The
``tool_execute_after`` hook (``_30_detect_repeat_failures.py``) wires it
into the agent. Everything here is defensive: a stale ``.pyc`` / partial
reload degrades to a no-op rather than crashing through
``handle_exception``.

Public surface (never raises):

    args_signature(tool_name, args, normalize=False) -> str
        Stable, bounded signature: ``"<tool_name>:<sha1(json(args,sort_keys))[:16]>"``.
        Same args -> same sig; reordered dict keys -> same sig; different
        args -> different sig. ``normalize=True`` strips whitespace in
        string values first (opt-in; the byte-identical death-loop needs
        no normalization). Oversized payloads are hashed chunk-wise rather
        than serialized into one giant string (see ``_CHUNK``).

    tool_signature(tool_name) -> str
        Per-tool signature that ignores the arguments. Used as a secondary
        detector, because the exact-match signature cannot see a death-loop
        that drifts its arguments between iterations (line numbers, ids,
        whitespace) -- the most common real stuck pattern.

    is_error_result(message, patterns) -> bool
        True if ``message`` matches any regex in ``patterns``
        (case-insensitive). The framework's ``helpers.tool.Response`` has
        no ``.error``/``.status`` field (``{message, break_loop,
        additional}``), so error detection is by message text -- the same
        convention ``text_editor`` uses (``"error patching <path>: ..."``).

    load_state(loop_data) -> dict | None
        Returns the per-context repeat-streak record from
        ``loop_data.params_persistent`` (NOT ``params_temporary`` --
        ``agent.py:408`` wipes ``params_temporary`` every iteration, so a
        streak there would never accumulate). ``params_persistent``
        survives across iterations and is fresh per monologue: the right
        lifetime. Creates+stores an empty dict if absent; the caller
        mutates it in place (it is the same object referenced by
        ``params_persistent[STATE_KEY]``, so in-place edits persist).
        Returns ``None`` when ``loop_data`` / ``params_persistent`` is
        unusable (the caller then no-ops: tracking lost for this call,
        never a crash).

    resolve_config(cfg) -> dict
        Resolves the effective knobs with inline defaults. Inline
        defaults are the real fallback because
        ``helpers.plugins.get_plugin_config`` does NOT merge
        ``default_config.yaml`` when a ``config.json`` exists -- a key
        the user did not set is simply absent. Thresholds respect ``0``
        (``0`` disables that action); we never use ``or`` which would
        coerce ``0`` back to the default.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any, Iterable

STATE_KEY = "_misformat_guard_tool_repeat"

# Default error indicators. Matched case-insensitively against the tool
# result message. ``^error\\b`` catches the text_editor family
# ("error patching <path>: old_text not found", "error writing ...").
# The traceback pattern requires Python's full traceback header, and the
# vague ``^failed\\b`` pattern was removed in v0.5.2: bare substrings
# classified SUCCESSFUL results as errors (e.g. an agent legitimately
# re-reading a log file that contains a traceback or the word "failed"),
# and four identical legitimate calls then hit the stop threshold and
# killed the turn. Add patterns back via ``tool_repeat_error_patterns``
# for tools that format errors differently.
_DEFAULT_ERROR_PATTERNS = (
    r"^error\b",
    r"^error:",
    r"old_text not found",
    r"traceback \(most recent call last\)",
)

# Tools that should never be tracked. The final-answer tool ends the loop
# on success (break_loop) and is not a "stuck repeat" candidate. Both the
# short and long Agent Zero names are listed for safety.
_DEFAULT_IGNORED_TOOLS = ("response", "response_tool")

# Compiled-regex cache keyed by the exact patterns tuple, so a stable
# config does not recompile on every tool call.
_COMPILED_CACHE: dict[tuple, list] = {}

def _sha1():
    """Return a fresh SHA-1 hasher.

    Resolved through getattr on each call so a monkeypatched
    ``hashlib.sha1`` (the test suite swaps it) is still honoured; the previous
    code used the module-global ``hashlib.sha1`` directly, which broke under
    that patch.
    """
    return getattr(hashlib, "sha1")()


def _print(msg: str) -> None:
    try:
        sys.stderr.write("[misformat_guard:tool-repeat] " + msg + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def _normalize_value(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_normalize_value(x) for x in v]
    return v


def args_signature(tool_name: Any, args: Any, normalize: bool = False) -> str:
    """Stable, bounded signature for a tool call.

    ``"<tool_name>:<sha1_hex_16>``. Bounded size regardless of how large
    ``args`` is (a `code_editor` patch can carry an entire file). Same args
    -> same sig; reordered dict keys -> same sig (``sort_keys=True``);
    different args -> different sig. ``normalize`` strips whitespace in
    string values so trivial whitespace variants collapse to one sig
    (opt-in; off by default to avoid false positives).

    Serialization is INCREMENTAL. This runs on every tool result via
    ``tool_execute_after``, and `json.dumps` of a multi-megabyte patch
    argument materializes the whole thing as one str before hashing it --
    repeated on every single tool call, on the hot path. ``iterencode``
    yields small chunks that are fed to the hasher one at a time, so peak
    memory stays flat regardless of argument size. A stable hashlib
    attribute name is used so a monkeypatched module cannot break hashing.
    """
    try:
        name = tool_name if isinstance(tool_name, str) else (str(tool_name) if tool_name is not None else "")
        payload = _normalize_value(args) if normalize else args
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, default=str)
        # update() against a real hasher, not the module-global: the
        # attribute lookup happens once and the alias can be swapped in
        # tests. hasher.update() requires BYTES, so each chunk is encoded
        # individually -- iterencode is what keeps those chunks small.
        sha1 = _sha1()
        if payload is None:
            sha1.update(b"{}")
        else:
            for chunk in encoder.iterencode(payload):
                if chunk:
                    sha1.update(chunk.encode("utf-8", "replace"))
        digest = sha1.hexdigest()[:16]
    except Exception:  # noqa: BLE001 - never raise on weird args
        digest = format(abs(hash(repr(args))) & 0xFFFFFFFF, "x")
    return f"{name}:{digest}"


def tool_signature(tool_name: Any) -> str:
    """Per-tool signature that ignores the arguments entirely.

    Paired with the per-tool streak this catches a loop that varies its
    arguments on every iteration -- e.g. an agent retrying a patch against
    successive line numbers -- which the exact-args signature treats as
    continuous forward progress and never flags.
    """
    try:
        name = tool_name if isinstance(tool_name, str) else (str(tool_name) if tool_name is not None else "")
    except Exception:  # noqa: BLE001
        name = ""
    return f"tool:{name}"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _compiled_patterns(patterns: Iterable[str]) -> list:
    """Return compiled regexes for ``patterns``, cached by the tuple key.

    Unparseable patterns are skipped (not crashed on) so a user typo in
    the config never breaks the hook.
    """
    key = tuple(patterns) if not isinstance(patterns, tuple) else patterns
    cached = _COMPILED_CACHE.get(key)
    if cached is not None:
        return cached
    compiled: list = []
    for p in key:
        try:
            compiled.append(re.compile(p, re.IGNORECASE | re.MULTILINE))
        except Exception:  # noqa: BLE001
            continue
    _COMPILED_CACHE[key] = compiled
    return compiled


def is_error_result(message: Any, patterns: Iterable[str]) -> bool:
    """True if ``message`` matches any of ``patterns`` (case-insensitive).

    A non-string / empty message is never an error (treated as a neutral
    / success result so the streak resets -- a blank result is not a
    repeatable failure).
    """
    if not isinstance(message, str) or not message:
        return False
    try:
        for rx in _compiled_patterns(patterns):
            if rx.search(message):
                return True
    except Exception:  # noqa: BLE001 - never raise on bad patterns
        return False
    return False


# ---------------------------------------------------------------------------
# State (per-context, in loop_data.params_persistent)
# ---------------------------------------------------------------------------


def load_state(loop_data: Any) -> dict | None:
    """Return the repeat-streak record for this context, creating it if
    absent. The returned dict is the same object stored in
    ``params_persistent[STATE_KEY]``, so in-place mutations persist across
    iterations (which is how the streak accumulates). Returns ``None``
    when ``loop_data`` / ``params_persistent`` is unusable (the caller
    then no-ops: tracking lost for this call, never a crash)."""
    if loop_data is None:
        return None
    try:
        params = getattr(loop_data, "params_persistent", None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(params, dict):
        return None
    state = params.get(STATE_KEY)
    if isinstance(state, dict):
        return state
    state = {}
    try:
        params[STATE_KEY] = state
    except Exception:  # noqa: BLE001
        return None  # cannot persist; tracking would be lost
    return state


# ---------------------------------------------------------------------------
# Config resolution (inline defaults; respects 0)
# ---------------------------------------------------------------------------


def _int_or(cfg: dict, key: str, default: int) -> int:
    v = cfg.get(key, default)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return default


def _list_or(cfg: dict, key: str, default: tuple) -> list:
    v = cfg.get(key, default)
    if v is None:
        return list(default)
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        # Allow a single pattern / tool as a bare string.
        return [v] if v else list(default)
    return list(default)


def resolve_config(cfg: Any) -> dict:
    """Resolve the tool-repeat knobs with inline defaults.

    Inline defaults are the real fallback (the framework does not merge
    ``default_config.yaml`` when a ``config.json`` exists). Thresholds
    respect ``0`` (disables that action); we never use ``or`` which would
    coerce ``0`` back to the default.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    action = cfg.get("tool_repeat_action", "warn_then_stop")
    if not isinstance(action, str) or action not in ("warn", "stop", "warn_then_stop"):
        action = "warn_then_stop"
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "guard_enabled": bool(cfg.get("tool_repeat_guard_enabled", True)),
        "warn_threshold": _int_or(cfg, "tool_repeat_warn_threshold", 2),
        "stop_threshold": _int_or(cfg, "tool_repeat_stop_threshold", 4),
        "action": action,
        "error_patterns": _list_or(cfg, "tool_repeat_error_patterns", _DEFAULT_ERROR_PATTERNS),
        "ignored_tools": _list_or(cfg, "tool_repeat_ignored_tools", _DEFAULT_IGNORED_TOOLS),
        "normalize_args": bool(cfg.get("tool_repeat_normalize_args", False)),
        "verbose": bool(cfg.get("verbose", False)),
    }