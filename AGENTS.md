# misformat_guard

> Repairs misformatted chat-model responses using a cheap utility model. Intercepts the agent's response stream, detects malformed JSON / broken tool calls, and re-prompts the model (or uses a local grammar repair) to fix the output before the framework tries to parse it.

**Version:** 0.4.0 · **Plugin ID:** `misformat_guard`

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

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults (referenced by `install()` and the WebUI settings UI)
- `README.md` — user-facing docs (what the plugin does from a user's perspective)
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch), `helpers/ui_server.py` (asset serving)
