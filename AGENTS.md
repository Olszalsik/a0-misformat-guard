# misformat_guard

> Repairs misformatted chat-model responses using a cheap utility model. Intercepts the agent's response stream, detects malformed JSON / broken tool calls, and re-prompts the model (or uses a local grammar repair) to fix the output before the framework tries to parse it.

**Version:** 0.5.0 · **Plugin ID:** `misformat_guard`

## Purpose

Repairs misformatted chat-model responses using a cheap utility model. Intercepts the agent's response stream, detects malformed JSON / broken tool calls, and re-prompts the model (or uses a local grammar repair) to fix the output before the framework tries to parse it.

## Ownership / Layout

- `api/` — repair / stats / config endpoints
- `helpers/` — format detector, repair prompt, fallback grammar
- `prompts/` — repair prompt templates
- `tools/` — agent-callable diagnostic tools
- `vendor/` — vendored grammar/repair library (no network deps)

## Local Contracts

- Repair attempts are bounded (default: 2). After 2 failed repairs, the original malformed response is passed through with a warning flag — never silently dropped.
- `vendor/` is a pinned version; updates require a manual review because the grammar library is the only thing standing between a malformed response and a parse loop.

## v2.5 Status

- Repair runs on the framework's event loop; uses `asyncio.to_thread` for the utility-model call.

## Verification

Inject a deliberately malformed JSON in a chat response (via a test prompt), confirm the plugin intercepts and returns a corrected version. Check the `tools/` stats endpoint for the repair count.

For the v0.5.0 tool-repeat guard: drive the agent into a synthetic repeat (ask for a `code_editor` patch with intentionally-wrong `old_text`), confirm 1st fail → count=1 (no warning), 2nd → `system_warning` in history + inline directive on the tool result, 3rd → no new warning (`warned` flag), 4th → turn ends with the stop message and `break_loop`. Then flip `tool_repeat_action: "warn"` (warns, never stops) and `tool_repeat_guard_enabled: false` (no-op) to confirm the knobs gate correctly.

## Changelog

### v0.5.0 — Layer 5: tool-repeat guard

**Root cause.** The agent gets stuck in a "reasoning death-loop": it re-emits the *same* tool call (e.g. a `code_editor` patch with a fixed `old_text`) that fails with `"error patching <path>: old_text not found"`, then re-emits it dozens of times, silently burning tokens. None of the framework's existing breakers catch this:

- `_90_stop_unusable_response_loop` only counts `fw.msg_misformat.md` / `fw.msg_repeat.md` *warnings*. This loop emits neither: the patch is a well-formed tool call (no misformat warning), and each iteration's response differs because the tool error is appended to history (no byte-identical repeat → `fw.msg_repeat.md` never fires at agent.py:494).
- The tool error is a normal tool *result* fed back to the model, not a warning, so the breaker's counter never increments.

**Fix.** A `tool_execute_after` hook (`extensions/python/tool_execute_after/_30_detect_repeat_failures.py`, pure extension point — no core patch). On every tool result it computes a signature `(tool_name, sha1(json.dumps(args, sort_keys=True))[:16])` and classifies the result as an error by message-text regex (the framework's `helpers.tool.Response` has no `.error`/`.status` field; `text_editor` emits `"error patching <path>: ..."`). The streak lives in `loop_data.params_persistent` — **not** `params_temporary`, which `agent.py:404` wipes every iteration (a streak there would never accumulate). `params_persistent` survives across iterations and is fresh per monologue: the right lifetime for "this task is stuck repeating".

  - Same sig + error → `count += 1`. Different (tool, args) → new streak at 1. Non-error → reset to 0 (progress is never penalized).
  - `count >= tool_repeat_warn_threshold` (default 2) and not yet warned → inject a `hist_add_warning` (a `{"system_warning": ...}` history entry the model sees next turn) AND prepend a directive to `response.message` (model sees it inline with the failure this turn). One-shot per sig-streak (`warned` flag) — no spam.
  - `count >= tool_repeat_stop_threshold` (default 4) → set `response.break_loop = True` and rewrite `response.message` to a stop explanation. `process_llm_result_tools` returns it as the final result (agent.py:1222-1224 / 1497-1498), ending the turn cleanly — no exception raised.

  Args are recovered from `self.agent.loop_data.current_tool.args` (`tool_args` is NOT passed to the after-hook); `current_tool` is still set in both execution paths because the `finally` that clears it runs *after* the hook. If `current_tool` is None (parallel-tool race / already cleared) the hook no-ops: a safe miss, never a false hit. The `_30_` prefix sorts after the framework's `_10_mask_secrets` so detection sees the final masked message. The hook NEVER raises; a stale `.pyc` / partial reload degrades to a no-op (defensive `getattr` lookups at call time).

**Knobs** (`default_config.yaml`, read with inline `cfg.get(key, <default>)` because `helpers.plugins.get_plugin_config` does NOT merge `default_config.yaml` when a `config.json` exists — inline defaults are the real fallback; thresholds respect `0` via an explicit None/empty check, never `or`):

- `tool_repeat_guard_enabled: true` — master switch for this layer.
- `tool_repeat_warn_threshold: 2` / `tool_repeat_stop_threshold: 4` — 0 disables that half.
- `tool_repeat_action: warn_then_stop` — `warn` (only warn), `stop` (only stop, skip warn), `warn_then_stop` (recommended).
- `tool_repeat_error_patterns` / `tool_repeat_ignored_tools` / `tool_repeat_normalize_args` — classification + ignore-list + sig normalization (advanced; not UI-persisted).

**Scope.** `config.json` is not touched — the feature activates via the inline defaults. No core/framework file is edited. Tests: `tests/test_tool_repeat_guard.py` (~28 cases: gating/no-op, streak accumulation/reset, warn-then-stop, 0-threshold respect, classification, stale-module safety, registration/discovery). Version bumped in lockstep (`plugin.yaml`, `api/__init__.py`, `hooks.py`) and the stale `test_extension_resolution.py` version assert (0.4.0 → 0.5.0).

### v0.4.1 — Layer 4: upstream cost-circuit-breaker coordination

`_10_misformat_consume_warning.py` resets the framework's `max_consecutive_unusable_responses` counter on every misformat warning so the agent is never stopped by a streak the cascade has already handled. `install()` optionally raises the framework setting to `consecutive_unusable_floor` (default 5) as a defense-in-depth backstop.

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults (referenced by `install()` and the WebUI settings UI)
- `README.md` — user-facing docs (what the plugin does from a user's perspective)
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch), `helpers/ui_server.py` (asset serving)
