## Quoting rules (misformat_guard)

Inside any JSON string value, especially `tool_args.text`, `tool_args.message`, and `tool_args.content`:

- **Never write a literal `"` in prose.** Use `\"`, single quotes `'...'`, or typographic quotes `「…」` / `«…»`.
- **When embedding JSON examples in a string**, escape every inner `"` as `\"`.
- **If a string value would exceed ~500 characters**, prefer a structured form (separate tool call, file, or list item) instead of one giant string.
- **Code samples inside string values** must escape inner quotes too, e.g. write `\"hello\"` not `"hello"`.

These rules exist because the dirty JSON parser can misread an unescaped `"` inside a long string as the closing quote of that string, which causes "misformatted message" warnings that compound across retries.
