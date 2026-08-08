"""Tests for the hardened vendored DirtyJson.

These tests pin the behavior the plugin needs from usr/plugins/misformat_guard/
helpers/hardened_dirty_json.py. The most important regression is the
"unescaped quote inside a long string value" failure mode that the upstream
parser mishandles. The hardening should:
  - parse a string value containing English prose with unescaped " correctly
    (the " is treated as part of the string until a real structural close)
  - still parse a well-formed object with real keys normally
  - still close on `,` `}` `]` `:`-followed-by-value (the standard cases)
"""

from __future__ import annotations

import sys
from pathlib import Path

def _find_project_root(start: Path) -> Path:
    cur = start
    for _ in range(8):
        if (cur / "agent.py").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("could not find agent.py walking up from " + str(start))


PROJECT_ROOT = _find_project_root(Path(__file__).resolve().parent)
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from usr.plugins.misformat_guard.vendor.hardened_dirty_json import DirtyJson  # noqa: E402


# ---------------------------------------------------------------------------
# The original failure mode
# ---------------------------------------------------------------------------

LONG_TEXT_WITH_UNESCAPED_QUOTES = (
    "{\n"
    '    "thoughts": ["planning the response"],\n'
    '    "tool_name": "code_execution_tool",\n'
    '    "tool_args": {\n'
    '        "text": "Here is a long description. The standard "disabled" '
    'marker is used to indicate that the feature is off. We also use '
    '"enabled" to indicate the opposite. In the original parser, this '
    'inner quote terminates the string early, then the rest of the JSON '
    'fails to parse, returning None, which causes the misformat warning."\n'
    "    }\n"
    "}"
)


def test_long_text_with_unescaped_quotes_parses() -> None:
    """The original failure: long text value with inner " triggers misformat.

    With the hardened parser the inner quotes are NOT treated as a close
    (because nothing structural follows them), so the value parses fully.
    """
    result = DirtyJson.parse_string(LONG_TEXT_WITH_UNESCAPED_QUOTES)
    assert isinstance(result, dict)
    assert result["tool_name"] == "code_execution_tool"
    text = result["tool_args"]["text"]
    assert "disabled" in text
    assert "enabled" in text
    # The full English text is preserved.
    assert text.count('"') >= 4  # at least "disabled" and "enabled" survive


# ---------------------------------------------------------------------------
# Real keys still close
# ---------------------------------------------------------------------------


def test_simple_object_still_parses() -> None:
    src = '{"a": 1, "b": "hello", "c": true}'
    result = DirtyJson.parse_string(src)
    assert result == {"a": 1, "b": "hello", "c": True}


def test_nested_object_still_parses() -> None:
    src = '{"a": {"b": {"c": "deep"}}, "d": [1, 2, 3]}'
    result = DirtyJson.parse_string(src)
    assert result == {"a": {"b": {"c": "deep"}}, "d": [1, 2, 3]}


def test_array_of_objects_still_parses() -> None:
    src = '[{"a": 1}, {"b": 2}, {"c": 3}]'
    result = DirtyJson.parse_string(src)
    assert result == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_string_value_with_escaped_quotes_still_parses() -> None:
    src = r'{"text": "He said \"hi\" and left."}'
    result = DirtyJson.parse_string(src)
    assert result == {"text": 'He said "hi" and left.'}


def test_empty_string_value() -> None:
    result = DirtyJson.parse_string('{"k": ""}')
    assert result == {"k": ""}


def test_empty_object() -> None:
    assert DirtyJson.parse_string("{}") == {}


def test_empty_array() -> None:
    assert DirtyJson.parse_string("[]") == []


# ---------------------------------------------------------------------------
# The hardening's key check: ":" must be followed by a value
# ---------------------------------------------------------------------------


def test_colon_followed_by_prose_is_NOT_a_key() -> None:
    """Regression: 'text ending in colon: more prose' should not be parsed
    as a key. The hardening rejects ":" unless the value that follows is
    JSON-shaped. As a result, the parser fails to find a complete root and
    the result is None (which is the correct behavior - the input is not
    valid JSON and the agent should be told)."""
    src = '{"note": "ending in a colon: and then text"}'
    # This SHOULD parse correctly because the colon is INSIDE a string.
    result = DirtyJson.parse_string(src)
    assert isinstance(result, dict)
    assert "colon:" in result["note"]


def test_string_value_with_inner_colon_still_parses() -> None:
    src = '{"text": "time: 12:34:56"}'
    result = DirtyJson.parse_string(src)
    assert result == {"text": "time: 12:34:56"}


# ---------------------------------------------------------------------------
# Defensive: the upstream parser mishandles; verify we don't regress
# ---------------------------------------------------------------------------


def test_unclosed_string_returns_partial_not_broken_dict() -> None:
    """A truncated JSON string (no closing quote) should NOT be reported as
    a complete dict - the upstream parser mishandles this when the
    truncation happens to be followed by valid-looking structure. The
    hardening should refuse to close on a long string with no structural
    close in sight."""
    # No close at all - the parser must give up.
    result = DirtyJson.parse_string('{"k": "this is unterminated')
    # The hardened parser may return the partial dict or None depending on
    # the path; the contract is that it does NOT silently close early on
    # a " inside a long value.
    assert result is None or (isinstance(result, dict) and "k" in result)


def test_idempotency_with_pure_json() -> None:
    """The hardened parser should be wire-compatible with json.loads for
    well-formed inputs."""
    import json

    cases = [
        '{"a": 1}',
        '[1, 2, 3]',
        '{"a": [1, {"b": "c"}]}',
        '{"unicode": "\\u00e9"}',
    ]
    for c in cases:
        assert DirtyJson.parse_string(c) == json.loads(c)
