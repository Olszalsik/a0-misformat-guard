# Misformat Guard

A plugin that prevents Agent Zero from looping indefinitely on
"misformatted message" warnings, and reduces how often those warnings
happen in the first place. v0.4.1 coordinates with the framework's
upstream cost circuit breaker so the agent is never stopped by a
misformat the cascade has already seen. v0.5.0 adds a tool-repeat guard
(Layer 5) that breaks the "reasoning death-loop" where the agent re-emits
the *same* failing tool call dozens of times -- the one loop none of the
framework's existing breakers catch.

The plugin lives in `usr/plugins/misformat_guard/` and is fully
isolated from the core Agent Zero code. It can be installed, enabled,
disabled, and uninstalled without touching the framework source.

## What it does

Five layers, each addressing a different part of the misformat problem:

| Layer | Name | Lives in | What it does |
|------:|------|----------|--------------|
| 1 | Primary cascade | `extensions/python/_functions/agent/Agent/call_chat_model_turn/end/_20_repair_via_utility.py` | @extensible /end hook on `call_chat_model_turn`. When the chat model's response fails the JSON tool extractor, calls the cheap utility model to repair it. Substitutes the repaired text into the LLMResult before the monologue loop sees the broken one. |
| 2 | Safety net | `extensions/python/_functions/agent/Agent/process_tools/end/_30_repair_via_utility_fallback.py` | @extensible /end hook on `process_tools`. Catches the rare misformat that slipped past the primary cascade and re-invokes `process_tools` with the repaired text. |
| 3a | Hardened parser | `vendor/hardened_dirty_json.py` + `extensions/python/response_stream_end/_10_repair_response.py` | Vendored `DirtyJson` with a tighter `_is_closing_quote` heuristic. Runs as a fast pre-filter before the cascade. |
| 3b | Quoting rules appended to the system prompt | `extensions/python/system_prompt/_10_quote_rules.py` + `prompts/quote_rules.md` | Tells the LLM to avoid unescaped `"` in string values (the dominant misformat cause). |
| 3c | Misformat warning rewritten to name the common cause | `extensions/python/hist_add_before/_10_clarify_misformat.py` | Appends a one-liner to the framework's `fw.msg_misformat.md` warning so the LLM self-corrects on the next try. |
| 4 | Upstream cost-circuit-breaker coordination (v0.4.1) | `extensions/python/_functions/agent/Agent/hist_add_warning/end/_10_misformat_consume_warning.py` | Sits at the same extension point as the framework's `_90_stop_unusable_response_loop` and runs before it. Resets the upstream's `max_consecutive_unusable_responses` counter on every misformat warning so the agent is never stopped by a streak the cascade has seen. |
| 5 | Tool-repeat guard (v0.5.0) | `extensions/python/tool_execute_after/_30_detect_repeat_failures.py` | A `tool_execute_after` hook that detects the agent re-emitting the *same* tool call with the *same* args and getting an error each time (e.g. a `code_editor` patch with a stale `old_text`). The framework's misformat/repeat breakers miss this: it's a well-formed tool call (no misformat warning), each iteration's response differs (the error is appended to history, so no byte-identical repeat), and the tool error is a normal result, not a warning. The guard tracks a per-context streak in `loop_data.params_persistent` (not `params_temporary`, which is wiped every iteration), warns at `tool_repeat_warn_threshold` (system_warning + inline tag) and hard-stops the turn at `tool_repeat_stop_threshold` (`break_loop`). |

The default behaviour:

- When the chat model produces an unparseable response, the cheap
  utility model is called to repair it. The repaired text replaces the
  broken one and the agent continues. The chat model is never called
  for repairs.
- Repair attempts are bounded (default: 2 per streak, 6 per chat) so
  a truly broken chat model cannot burn the utility budget.
- The framework's `max_consecutive_unusable_responses` cost circuit
  breaker is bypassed for misformat warnings: the consume hook resets
  the counter before the upstream reads it. The circuit breaker still
  fires on `fw.msg_repeat.md` and on any other warning the consume
  hook does not recognize.
- A "Quoting rules" block is appended to the system prompt, telling
  the LLM to avoid unescaped `"` in string values.
- The misformat warning that lands in history is augmented with a
  one-liner naming the common cause so the model self-corrects on
  the next try.
- The tool-repeat guard (v0.5.0) watches every `tool_execute_after`. When
  the *same* tool + *same* args returns an error N times in a row, it
  warns the model at `warn_threshold` (default 2: a `system_warning` in
  history + a directive prepended to the tool result) and hard-stops the
  turn at `stop_threshold` (default 4: `break_loop` with a stop
  message). A non-error result, or a different (tool, args), resets the
  streak -- progress is never penalized. The final-answer tool
  (`response`) is never tracked.

## Installation

The plugin is a directory in `usr/plugins/`. Agent Zero's plugin
runtime will pick it up on the next restart, or the user can enable
it from the Plugins UI. On first enable, `hooks.py:install()`:

1. Creates `stats/` and `.plugin_state.json` for counter persistence.
2. (v0.4.1) If `install_overrides_consecutive_floor: true`, raises
   the framework's `max_consecutive_unusable_responses` to at least
   `consecutive_unusable_floor` (default 5). The original value is
   recorded in `.plugin_state.json` so `uninstall()` can restore it.

The plugin needs **no core patch** in v0.4.0+. All repair seams are
framework-native extension points.

To uninstall, the Plugins UI runs `hooks.py:uninstall()` which
removes the toggle files and (v0.4.1) restores the framework's
`max_consecutive_unusable_responses` to its pre-install value.

## Configuration

`default_config.yaml` exposes the full set of knobs. All settings can
be overridden at three scopes (per-project > per-agent > global >
defaults). The `enabled: true|false` master switch, the cascade mode,
and the v0.4.1 circuit-breaker coordination are the most useful:

```yaml
enabled: true                          # master switch
primary_cascade_enabled: true          # Layer 1
process_tools_fallback: true           # Layer 1 safety net
repair_enabled: true                   # Layer 3a hardened parser
quote_rules_enabled: true              # Layer 3b
clarify_misformat_warning: true        # Layer 3c
reset_unusable_loop_on_warning: true   # v0.4.1 Layer 4
consecutive_unusable_floor: 5          # v0.4.1 install() sets this on the framework
install_overrides_consecutive_floor: true  # v0.4.1 set false to leave framework alone
# Layer 5 (v0.5.0) tool-repeat guard
tool_repeat_guard_enabled: true
tool_repeat_warn_threshold: 2          # consecutive identical fails -> warn
tool_repeat_stop_threshold: 4          # consecutive identical fails -> stop
tool_repeat_action: warn_then_stop     # warn | stop | warn_then_stop
tool_repeat_normalize_args: false      # true: strip whitespace before sig
# tool_repeat_error_patterns / tool_repeat_ignored_tools: see
# default_config.yaml (advanced; not UI-persisted).
cascade:
  mode: utility_repair
  trigger: 1                           # fire on first misformat
  max_per_streak: 2                    # bound per-streak repair attempts
  max_total_per_chat: 6                # bound per-chat repair attempts
  timeout_s: 30                        # utility-model call timeout
```

## Diagnostic tool

The plugin registers a `misformat_diagnose` tool the LLM (or the user,
via the chat) can call to inspect what's happening. Actions:

- `stats`       - return the current counter snapshot
- `history`     - return the most recent misformat warnings from history
- `test_parser` - parse a sample string with both parsers for comparison
- `reset_stats` - clear the in-memory counters

## WebUI

`webui/config.html` and `webui/dashboard-store.js` provide a live
dashboard with counter tiles, the configuration form (5 layers: Layer 4
is the v0.4.1 circuit-breaker coordination; Layer 5 is the v0.5.0
tool-repeat guard), and a 5-second polling loop that pauses when the
tab is hidden. v0.3.0 disabled polling to avoid a UI freeze; v0.4.1
re-enables it with a visibility guard so the freeze does not return.

## Testing

```bash
# Plugin unit tests
python -m pytest usr/plugins/misformat_guard/tests/ -v

# Framework-side test for the cost circuit breaker
python -m pytest tests/test_unusable_response_loop.py -v
```

`test_consume_misformat_warning.py` (added in v0.4.1) exercises the
new `hist_add_warning/end` hook end-to-end with the framework's
upstream extension, asserting that two consecutive misformat
warnings do not raise `HandledException` when the consume hook is
active and still do when the hook is disabled.
