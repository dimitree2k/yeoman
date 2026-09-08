# Model catalog and profile settings

Approved design: retain JSON and existing profile/model choices. Store capability facts
separately in `~/.yeoman/model-catalog.json`, keyed by provider and exact model ID.
Fetch current OpenRouter metadata on explicit refresh; direct provider cards contain
documentation URLs and their actual review date. Unknown capabilities stay unknown.
Refresh never modifies config or policy; offline refresh preserves previous facts.

Implementation sequence: capability parsing and validation tests; catalog/CLI;
provider payload adapters; profile token-limit propagation; isolated tests; integration
into the editable installation and restart Gateway/Overseer. No model calls or messages
are needed for verification.

## Commands

```sh
yeoman models refresh
yeoman models list
yeoman models show assistantDefault
yeoman models check
yeoman models configure assistantDefault --copy-to focusedChat --reasoning on --effort high
yeoman models assign focusedChat --channel whatsapp --chat EXACT_CHAT_ID
yeoman models configure focusedChat --reasoning default
```

`configure` without flags shows the card. `--effort` accepts only the exact advertised
values. `--reasoning default` clears explicit reasoning; `on`/`off` reset old effort
and budget choices. Effort and `--reasoning-budget` are mutually exclusive. Mandatory
reasoning cannot be disabled. `--model`/`--provider` changes revalidate the complete
reasoning setting against the new card. For a new uncached model, first select it with
`--reasoning default`, refresh, then choose verified settings. `--temperature` requires
verified support; `--max-tokens` checks known output limits. Model cards also report
context size, tool support, input modalities, alias target and provenance when known.

Profiles remain the reusable settings layer. `--copy-to` avoids changing shared profiles.
`assign --persona EXACT_PERSONA_FILE --channel whatsapp` applies a profile to existing
chats explicitly referencing that `personaFile`; it does not establish a default for
future chats or inherited persona choices. Config/profile changes require a Gateway and
Overseer restart. Policy changes use the existing policy reload setting.

Reasoning is translated into OpenRouter's `reasoning`, direct DeepSeek's `thinking`
plus `reasoning_effort`, Xiaomi/Z.ai's `thinking`, or Groq's `reasoning_effort`.
Unverified direct adapters reject explicit reasoning instead of sending OpenRouter
fields blindly. Known DeepSeek/MiMo thinking modes omit unsupported temperature.
The chat loop now honors profile `maxTokens` (previously it always used 4096).

## Live metadata findings, 2026-09-08

24 unique provider/model cards cover 33 existing profiles. `assistantDefault` still
selects `~z-ai/glm-flash-latest`; its current alias reports mandatory reasoning and
efforts `max`, `high`, `low`, while the existing profile has `medium`.
`inclusionai/ring-2.6-1t` is missing from the current model listing, so its existing
explicit reasoning cannot be verified. `inclusionai/ling-2.6-1t` is also unknown.
These are audit findings: no existing model/profile/policy choice was changed.

Direct capability sources are documented in each card; OpenRouter facts come from
https://openrouter.ai/api/v1/models. Direct sources require deliberate documentation
review when providers change; running refresh does not pretend to reverify them.

## Verification and activation

- 62 targeted tests passed in both the isolated worktree and integrated source:
  catalog/CLI, provider registry and payloads, chat token propagation, textual tool
  calls, memory recall and social holdback.
- Ruff on the new catalog/CLI/tests and changed provider modules passed;
  `git diff --check` passed.
- Installed `yeoman models show assistantDefault` reads the populated catalog.
- Gateway and Overseer restarted successfully at 02:11 CEST. Both IPC sockets
  answered `ping` with `pong`; Bridge remained reachable on port 3001.
- SHA-256 comparisons confirmed config.json and policy.json were byte-identical
  before and after service restart. No paid model calls or outbound messages were
  used for testing. Actual provider responses to new reasoning settings have not
  been tested live; request construction was tested with mocked completions.

## Follow-up configuration correction

At the user's explicit request, changed `assistantDefault.reasoning.effort` from
`medium` to the advertised `high`, and cleared `inclusionRing26.reasoning` to
provider default. Model IDs and other profile settings remain unchanged.
`yeoman models check` reports zero invalid profiles; Ling/Ring capabilities remain
unknown because the models are absent from the catalog response.
