# misformat_guard

> Repairs misformatted chat-model responses with a cheap utility model, and breaks the "reasoning death-loop" where an agent re-emits a failing tool call indefinitely. No core patch — pure v2.5 extension hooks.

**Version:** 0.7.0 · **Plugin ID:** `misformat_guard` · **Requires:** framework v2.5+ (`@extensible` end hooks)

## Purpose

Two independent failure modes, four layers:

1. The chat model emits text the JSON tool extractor cannot parse. Layer 1 repairs it with the utility model before the loop sees it; Layer 2 is a safety net that re-runs `process_tools` if one slips through.
2. The agent re-emits a failing tool call. Layer 5 warns, then hard-stops the turn. Neither the framework's cost breaker nor its repeat detector sees this: the tool error is a normal *result*, not a warning, and the response differs each iteration because the error is appended to history.

## Ownership / Layout

- `api/` — `config`, `stats`, `health`, `reset` (`ApiHandler`s) + `misformat_config` (config layering), `misformat_stats` (counters), `misformat_repair` (detection + repair prompt), `tool_repeat` (guard helpers).
- `extensions/python/_functions/agent/Agent/…` — Layers 1, 2 and 4 on the `@extensible` end hooks. The retired flattened `extensions/python/<module>_<qualname>_<start|end>/` form does not resolve; always use the nested `_functions/<module>/<qualname>/<start|end>/` path.
- `extensions/python/` (named points) — `message_loop_prompts_after/_10_detect_misformat` (streak), `hist_add_before/_10_clarify_misformat`, `response_stream_chunk/_10_buffer_stream` (safety-net buffer), `system_prompt/_10_quote_rules`, `tool_execute_after/_30_detect_repeat_failures` (Layer 5), `banners/`.
- `tools/misformat_diagnose.py` — agent-callable diagnostics.
- `vendor/hardened_dirty_json.py` — dependency-free JSON salvage. **No production path imports it**; it exists with its tests only. Do not reintroduce a use without a caller.
- `prompts/`, `webui/` (config panel + `dashboard-store.js`), `extensions/webui/page-head/` (loads the store).

## Local Contracts

- **Extension ordering is file-basename sort**, so `_10_` runs before `_90_`. Layer 4 depends on this: it must write the framework breaker's state before `_90_stop_unusable_response_loop` reads it in the same invocation. De-duplication is by module filename, first occurrence wins.
- **Streaks and budgets live in `params_persistent`, never `params_temporary`.** `agent.py` wipes `params_temporary` at the start of every inner iteration, so a streak there can never exceed 1. The stream buffer (`_misformat_guard_stream_full`) is the one exception: it is written and consumed within a single iteration, so it stays on `params_temporary` deliberately.
- **The two cascade budget keys are shared** and defined once in `api/misformat_config.py` as `CASCADE_USED_STREAK_KEY` / `CASCADE_USED_TOTAL_KEY`. Layers 1 and 2 read and write the same counters.
  - `CASCADE_USED_TOTAL_KEY` (`max_total_per_chat`) spans the whole monologue and is never reset mid-monologue.
  - `CASCADE_USED_STREAK_KEY` (`max_per_streak`) is reset by the streak detector when a streak ends. It used to only ever be incremented, which silently degraded `max_per_streak` into a per-monologue cap — after two repairs anywhere in a monologue the cascade was dead, despite the config describing it as per streak.
- **Never rebuild the `LLLMResult`.** Substitute the repaired text in place (`_apply_repaired_response`). `LLMResult` (`helpers/llm_result.py`) carries twelve fields; constructing a fresh one resets `response_id`, `previous_response_id`, `input_items`, `output_items`, `provider_model_key`, `mode`, `state`, `usage`, `raw` and `capability` to defaults — breaking Responses-API chaining and losing the provider's native function-call items. `Agent.hist_add_ai_response` owns Responses state advancement; replacing the object defeats it. Note `helpers.llm` does not exist — `LLMResult` lives in `helpers.llm_result` and is re-exported from `agent`.
- **`is_misformat` fails CLOSED.** If `helpers.extract_tools` cannot be imported, the plugin cannot distinguish a misformat from a valid response, so it reports "not a misformat" and the cascade no-ops. Failing open would classify *every* response as malformed and replace it with a utility-model rewrite of itself, up to `max_total_per_chat` times per monologue.
- **Layer 4 reaches into a framework private constant.** `UPSTREAM_STATE_KEY` mirrors `STATE_KEY` in the framework's `_90_stop_unusable_response_loop.py`. `_upstream_state_key_confirmed()` re-checks it against the real module on every call and **fails safe**: if the key cannot be confirmed, the framework's own cost breaker is left untouched. Do not remove that guard — a silent mismatch is indistinguishable from working.
- **Hooks never raise.** Every layer wraps its body in `except Exception: pass` and degrades to a no-op, so a stale module or a missing attribute can never break the tool loop. Keep it that way.
- **API handlers need explicit `get_methods()`.** The framework default is `["POST"]` and returns 405 for anything else (`helpers/api.py:255`). `config`, `stats` and `health` declare `["GET", "POST"]`.
- **Browser calls go through the framework's `fetchApi`.** `requires_csrf()` follows `requires_auth()` (True), so a raw `fetch()` without `X-CSRF-Token` gets a 403. `webui/dashboard-store.js` is loaded as a *module* from the page-head extension precisely so it can import `fetchApi`.
- **Plugin assets are served from `/usr/plugins/<name>/<path>`** (`helpers/ui_server.py` `serve_plugin_asset`), and only from within the plugin's `webui/` or `extensions/webui/` directory.
- **Error detection is message-text based.** `helpers.tool.Response` has no `.error`/`.status` field (`{message, break_loop, additional}`), so `is_error_result` matches regexes against the message — the same convention `text_editor` uses (`"error patching <path>: …"`). This means a tool whose *legitimate* output starts with "error" is classified as a failure; keep `tool_repeat_error_patterns` tight.
- **Plugin config is read with inline defaults.** `helpers.plugins.get_plugin_config` does not merge `default_config.yaml` when a `config.json` exists, so a key the user never set is simply absent — the `cfg.get(key, <default>)` at each call site is the real fallback. Thresholds must respect `0` (which disables an action); never use `or`, which coerces `0` back to the default.
- **Two signatures, two thresholds.** The exact-args signature `(tool_name, sha1(args))` is the primary detector. A per-tool signature that ignores arguments is the *fallback* for a loop that drifts its arguments between iterations (retrying a patch against successive line numbers) — the exact signature reads that as continuous progress. The fallback uses doubled thresholds and is gated on the exact streak being *below* its own threshold, so a byte-identical loop never emits both warnings.

## Configuration

`default_config.yaml` + `api/config.py::_SCALAR_KEYS` must stay in sync. `consecutive_unusable_floor` and `install_overrides_consecutive_floor` are read at **install time**; the rest are read per call. `_coerce` guards every numeric conversion — a text input can deliver anything, and an unguarded `int()` turned a typo in the form into a 500.

## Work Guidance

- `install()` raises the framework's `max_consecutive_unusable_responses` to `consecutive_unusable_floor`. The writer is `helpers.settings.set_settings(settings, apply=True)`; there is **no** `update_settings`. The previous code called the non-existent name inside `except AttributeError`, so every install silently skipped the write while recording the change as applied in `.plugin_state.json` — and `uninstall()` then "restored" a value that had never changed. Persist the original **before** mutating, so a crash mid-install is recoverable.
- Mutating a framework-global setting is a scope decision: it affects every agent and every plugin. It is on by default; if you touch it, say so in `README.md`.
- `reset_unusable_loop_on_warning` removes the framework's only unbounded-cost guard for misformat loops. It is the price of "the agent never stalls"; the backstop is the floor above. Keep both in mind together.

## Verification

- `pytest usr/plugins/misformat_guard/tests` — 117 tests, all green.
- The end-to-end consume-hook tests drive the **real** framework `_90_stop_unusable_response_loop`. Their `read_prompt` fixture resolves like `Agent.read_prompt` does (root `prompts/` then every `plugins/*/prompts/`, since the upstream reads prompts unconditionally on entry). Do not narrow it back to a hand-picked list.
- Drive the agent into a synthetic repeat (ask for a `code_editor` patch with a wrong `old_text`): 1st fail → count 1, no warning; 2nd → `system_warning` + inline directive; 4th → stop message and `break_loop`. Then flip `tool_repeat_action: "warn"` and `tool_repeat_guard_enabled: false` to confirm the knobs gate.
- Confirm the WebUI panel actually loads: the page-head `<script type="module">` must resolve to `/usr/plugins/misformat_guard/webui/dashboard-store.js`, and `/stats` must answer 200 (not 403) with a CSRF token applied.

## Store Submission

The runtime manifest and the hub index are different files in different repositories; there is no local `plugin-hub/` directory. Contents go at the **repository root** so the installer finds `plugin.yaml`. The index CI validator (`agent0ai/a0-plugins/scripts/validate_plugin_submission.py`) requires folder name `^[a-z0-9_]+$` with no leading `_`, the remote `plugin.yaml` `name` to match the index folder name exactly, and only `title`/`description`/`github`/`tags`/`screenshots` within their length limits. A root `LICENSE` is required for listing (its absence is a guaranteed validator warning). `always_enabled` stays `false` — it is framework-reserved.

`plugin.yaml` is parsed by `PluginMetadata`, which has no `model_config`, so unknown keys are silently dropped. `min_framework_version` was removed in v0.7.0 for exactly this reason: it looked like an active version gate and was not. The v2.5 requirement is documented in this file instead. `settings_sections` must list only `agent`/`external` — those are the only two that mount the settings subsection.

## See also

- `plugin.yaml` — manifest (parsed keys documented inline)
- `default_config.yaml` — defaults
- `README.md` — user-facing docs
- Framework: `helpers/plugins.py` (lifecycle + config), `helpers/api.py` (dispatch + CSRF), `helpers/settings.py` (`set_settings`), `helpers/llm_result.py` (`LLMResult`), `helpers/ui_server.py` (asset serving)
