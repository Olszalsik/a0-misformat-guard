"""
Hardened vendored copy of helpers/dirty_json.py.

The hardening is in DirtyJson._is_closing_quote: instead of the upstream
heuristic that consumes an unescaped " inside a long string value as the
string's closing quote whenever it is followed by anything that "looks
like" a key (which fires too eagerly on English prose containing
quotation marks), this version requires that:

  1. The unescaped " is followed (after whitespace/comments) by a structural
     JSON char: }  ]  ,  or  :
  2. If that char is  :  (a key-value separator), the char after the colon
     (after whitespace) must be " (a string value opening) or one of the
     value-prefix chars (digit, -, t, f, n, [, {). This distinguishes a
     real key from English prose ending in a colon.
  3. As a final safety net, if the parser has already accumulated a string
     value longer than MAX_STRING_LEN_WITHOUT_CLOSE, it forces the
     candidate " to be treated as escaped (appended to the string content)
     even if the structural test above would pass. This is a backstop
     against the rare case where a long value happens to be followed by a
     short key.

These changes are independently unit-testable; see
usr/plugins/misformat_guard/tests/test_hardened_dirty_json.py.

NOTE: This file is intentionally self-contained. It does NOT import from
helpers/dirty_json.py so the plugin can be developed, tested, and shipped
independently of the upstream parser. Once Agent Zero upstream merges the
fix (the planned PR-2), this file can be deleted and the plugin can
import the upstream DirtyJson directly.
"""

from __future__ import annotations

import json

# Backstop: if a string value exceeds this many characters without seeing a
# candidate close, give up and treat any subsequent " as escaped. The
# real fix is the structural check; this is just paranoia for very long
# inputs.
MAX_STRING_LEN_WITHOUT_CLOSE = 50_000


def try_parse(json_string: str):
    try:
        return json.loads(json_string)
    except json.JSONDecodeError:
        return DirtyJson.parse_string(json_string)


def parse(json_string: str):
    return DirtyJson.parse_string(json_string)


def stringify(obj, **kwargs):
    return json.dumps(obj, ensure_ascii=False, **kwargs)


class DirtyJson:
    def __init__(self):
        self._reset()

    def _reset(self):
        self.json_string = ""
        self.index = 0
        self.current_char = None
        self.result = None
        self.stack = []
        self.completed = False
        self._parsing_started = False
        # Hardening: track length of the current string value being parsed
        # so the backstop can fire.
        self._current_string_len = 0

    def _pop_stack(self, root_closed: bool = False):
        self.stack.pop()
        if root_closed and self._parsing_started and not self.stack:
            self.completed = True

    @staticmethod
    def parse_string(json_string):
        parser = DirtyJson()
        return parser.parse(json_string)

    def parse(self, json_string):
        self._reset()
        self.json_string = json_string
        if not json_string:
            return None
        self.index = self.get_start_pos(self.json_string)
        if self.index >= len(self.json_string):
            return None
        self.current_char = self.json_string[self.index]
        self._parse()
        return self.result

    def feed(self, chunk):
        self.json_string += chunk
        if not self.current_char and self.json_string:
            self.current_char = self.json_string[0]
        self._parse()
        return self.result

    def _advance(self, count=1):
        self.index += count
        if self.index < len(self.json_string):
            self.current_char = self.json_string[self.index]
        else:
            self.current_char = None

    def _skip_whitespace(self):
        while self.current_char is not None:
            if self.current_char.isspace():
                self._advance()
            elif (
                self.current_char == "/" and self._peek(1) == "/"
            ):
                self._skip_single_line_comment()
            elif (
                self.current_char == "/" and self._peek(1) == "*"
            ):
                self._skip_multi_line_comment()
            else:
                break

    def _skip_single_line_comment(self):
        while self.current_char is not None and self.current_char != "\n":
            self._advance()
        if self.current_char == "\n":
            self._advance()

    def _skip_multi_line_comment(self):
        self._advance(2)
        while self.current_char is not None:
            if self.current_char == "*" and self._peek(1) == "/":
                self._advance(2)
                break
            self._advance()

    def _parse(self):
        if self.completed and not self.stack:
            return
        if self.result is None:
            self.result = self._parse_value()
        else:
            self._continue_parsing()

    def _continue_parsing(self):
        while self.current_char is not None:
            if self.completed and not self.stack:
                return
            if isinstance(self.result, dict):
                self._parse_object_content()
            elif isinstance(self.result, list):
                self._parse_array_content()
            elif isinstance(self.result, str):
                self.result = self._parse_string()
            else:
                break

    def _parse_value(self):
        self._skip_whitespace()
        if self.current_char == "{":
            if not self.stack and self._peek(1) == "{":
                self._advance(2)
            return self._parse_object()
        elif self.current_char == "[":
            return self._parse_array()
        elif self.current_char in ['"', "'", "`"]:
            if self._peek(2) == self.current_char * 2:
                return self._parse_multiline_string()
            return self._parse_string()
        elif self.current_char and (
            self.current_char.isdigit() or self.current_char in ["-", "+"]
        ):
            return self._parse_number()
        elif self._match("true"):
            return True
        elif self._match("false"):
            return False
        elif self._match("null") or self._match("undefined"):
            return None
        elif self.current_char:
            return self._parse_unquoted_string()
        return None

    def _match(self, text: str) -> bool:
        if not self.current_char or self.current_char.lower() != text[0].lower():
            return False
        remaining = len(text) - 1
        if self._peek(remaining).lower() == text[1:].lower():
            self._advance(len(text))
            return True
        return False

    def _parse_object(self):
        obj = {}
        self._advance()
        self.stack.append(obj)
        self._parsing_started = True
        self._parse_object_content()
        return obj

    def _parse_object_content(self):
        while self.current_char is not None:
            self._skip_whitespace()
            if self.current_char == "}":
                if len(self.stack) == 1 and self._peek(1) == "}":
                    self._advance(2)
                else:
                    self._advance()
                self._pop_stack(root_closed=True)
                return
            if self.current_char is None:
                self._pop_stack()
                return

            key = self._parse_key()
            value = None
            self._skip_whitespace()

            if self.current_char == ":":
                self._advance()
                value = self._parse_value()
            elif self.current_char is None:
                value = None
            else:
                value = self._parse_value()

            self.stack[-1][key] = value

            self._skip_whitespace()
            if self.current_char == ",":
                self._advance()
                continue
            elif self.current_char != "}":
                if self.current_char is None:
                    self._pop_stack()
                    return
                continue

    def _parse_key(self):
        self._skip_whitespace()
        if self.current_char in ['"', "'"]:
            return self._parse_string(is_key=True)
        else:
            return self._parse_unquoted_key()

    def _parse_unquoted_key(self):
        result = ""
        while (
            self.current_char is not None
            and not self.current_char.isspace()
            and self.current_char not in [":", ",", "}", "]"]
        ):
            result += self.current_char
            self._advance()
        return result

    def _parse_array(self):
        arr = []
        self._advance()
        self.stack.append(arr)
        self._parsing_started = True
        self._parse_array_content()
        return arr

    def _parse_array_content(self):
        while self.current_char is not None:
            self._skip_whitespace()
            if self.current_char == "]":
                self._advance()
                self._pop_stack(root_closed=True)
                return
            value = self._parse_value()
            self.stack[-1].append(value)
            self._skip_whitespace()
            if self.current_char == ",":
                self._advance()
                self._skip_whitespace()
                if self.current_char is None or self.current_char == "]":
                    if self.current_char == "]":
                        self._advance()
                    self._pop_stack(root_closed=True)
                    return
            elif self.current_char != "]":
                self._pop_stack()
                return

    def _parse_string(self, is_key: bool = False):
        result = ""
        quote_char = self.current_char
        self._advance()  # Skip opening quote
        self._current_string_len = 0
        while self.current_char is not None:
            if self.current_char == quote_char:
                if self._is_closing_quote(is_key):
                    break
                result += self.current_char
                self._current_string_len += 1
                self._advance()
                continue

            if self.current_char == "\\":
                self._advance()
                if self.current_char in ['"', "'", "\\", "/", "b", "f", "n", "r", "t"]:
                    result += {
                        "b": "\b",
                        "f": "\f",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                    }.get(self.current_char, self.current_char)
                    self._current_string_len += 1
                elif self.current_char == "u":
                    self._advance()
                    unicode_char = ""
                    for _ in range(4):
                        if self.current_char is None or not self.current_char.isalnum():
                            return result + "\\u" + unicode_char
                        unicode_char += self.current_char
                        self._advance()
                    try:
                        result += chr(int(unicode_char, 16))
                    except ValueError:
                        result += "\\u" + unicode_char
                    self._current_string_len += 1
                    continue
            else:
                result += self.current_char
                self._current_string_len += 1
            self._advance()
        if self.current_char == quote_char:
            self._advance()
        return result

    # ---------------------------------------------------------------------------
    # HARDENED: only treat a candidate " as a closing quote when the
    # surrounding context actually looks like JSON structure.
    # ---------------------------------------------------------------------------
    def _is_closing_quote(self, is_key: bool) -> bool:
        # Backstop: long string value with no close in sight -> never close.
        if self._current_string_len > MAX_STRING_LEN_WITHOUT_CLOSE:
            return False

        next_index = self._skip_padding_from(self.index + 1)
        if next_index >= len(self.json_string):
            return True

        next_char = self.json_string[next_index]

        # Keys: only a structural char is valid.
        if is_key:
            return next_char in [":", ",", "}", "]"]

        # Hardening: an unescaped " inside a string value is only a close
        # if the *next structural context* is JSON-valid. Specifically:
        #   - `,` `}` `]`  -> always close (comma, end of object, end of array)
        #   - `:`          -> only close if the value that follows is a
        #                     JSON value prefix (quoted string, number, [,
        #                     {, true/false/null). English prose ending
        #                     in ":" followed by text is NOT a real key.
        if next_char in [",", "}", "]"]:
            return True
        if next_char == ":":
            return self._value_after_colon_looks_like_json(next_index + 1)
        return False

    def _value_after_colon_looks_like_json(self, index: int) -> bool:
        """True if the value starting at `index` (skipping whitespace and
        comments) begins with a JSON value prefix character. This is the
        hardening's key check: distinguishes a real key (`"text": "..."`)
        from English prose ending in a colon (`...this is "weird": and then
        more text`)."""
        index = self._skip_padding_from(index)
        if index >= len(self.json_string):
            # End of input after colon: could be a truncated real value;
            # treat as JSON-valid (the original parser would have done the
            # same).
            return True
        c = self.json_string[index]
        return c in ['"', "'", "`", "[", "{"] or c.isdigit() or c in ["-", "+", "t", "f", "n"]

    # Kept for API compatibility with the upstream parser. No longer used
    # by _is_closing_quote but referenced in the agent_zero v2.2 tree.
    def _looks_like_missing_comma_before_key(self, index: int) -> bool:
        if not self.stack or not isinstance(self.stack[-1], dict):
            return False
        if index >= len(self.json_string) or self.json_string[index] not in ['"', "'"]:
            return False
        quote_char = self.json_string[index]
        index += 1
        while index < len(self.json_string):
            char = self.json_string[index]
            if char == "\\":
                index += 2
                continue
            if char == quote_char:
                next_index = self._skip_padding_from(index + 1)
                return (
                    next_index < len(self.json_string)
                    and self.json_string[next_index] == ":"
                )
            if char in ["\n", "\r", "{", "}", "[", "]", ","]:
                return False
            index += 1
        return False

    def _skip_padding_from(self, index: int) -> int:
        while index < len(self.json_string):
            char = self.json_string[index]
            if char.isspace():
                index += 1
            elif char == "/" and index + 1 < len(self.json_string):
                next_char = self.json_string[index + 1]
                if next_char == "/":
                    index += 2
                    while index < len(self.json_string) and self.json_string[index] != "\n":
                        index += 1
                elif next_char == "*":
                    end = self.json_string.find("*/", index + 2)
                    if end == -1:
                        return len(self.json_string)
                    index = end + 2
                else:
                    break
            else:
                break
        return index

    def _parse_multiline_string(self):
        result = ""
        quote_char = self.current_char
        self._advance(3)
        while self.current_char is not None:
            if self.current_char == quote_char and self._peek(2) == quote_char * 2:
                self._advance(3)
                break
            result += self.current_char
            self._advance()
        return result.strip()

    def _parse_number(self):
        number_str = ""
        while self.current_char is not None and (
            self.current_char.isdigit()
            or self.current_char in ["-", "+", ".", "e", "E"]
        ):
            number_str += self.current_char
            self._advance()
        try:
            return int(number_str)
        except ValueError:
            return float(number_str)

    def _parse_unquoted_string(self):
        result = ""
        while self.current_char is not None and self.current_char not in [
            ":",
            ",",
            "}",
            "]",
        ]:
            result += self.current_char
            self._advance()
        self._advance()
        return result.strip()

    def _peek(self, n):
        peek_index = self.index + 1
        result = ""
        for _ in range(n):
            if peek_index < len(self.json_string):
                result += self.json_string[peek_index]
                peek_index += 1
            else:
                break
        return result

    def get_start_pos(self, input_str: str) -> int:
        chars = ["{", "[", '"']
        indices = [input_str.find(char) for char in chars if input_str.find(char) != -1]
        return min(indices) if indices else 0
