# Config and policy review — 2026-09-08

Review and cleanup of current private runtime configuration against source HEAD
`31f79ab0fb6d0176fa8bf7c37851881a537cf8f6` on `main`, including pre-existing
working-tree changes. JSON remains the persisted format. The cleanup changed only
the named policy/config/docs plus the shared loader/schema and doctor diagnostics;
no credentials, chat targets, model selections, or Bridge files were changed.

## Verified scope

- Runtime `config.json`, `policy.json`, examples, environment variable names,
  systemd service definitions, mount allowlist, cron profile references, and
  Overseer runbook profile references.
- Current config schema/migration, policy inheritance, model routing, bridge
  consumers, memory policy consumers, TTS dispatch and reply-budget semantics.
- Gateway, Bridge and Overseer systemd units report active. This does not prove
  that every disk setting is loaded into each running process.
- Config and policy validate against current source. Config now matches the
  loader's canonical serialized representation and no longer contains the
  duplicate `runtime.whatsappBridge` block.
- All 19 normalized model routes resolve. The 33 remaining model profiles and all 14 configured
  WhatsApp chats resolve; explicitly referenced chat persona files exist.

## Findings, in recommended order

### 1. Remove redundant per-chat overrides — implemented

Seven chats repeat the inherited `assistantDefault` profile; eight repeat voice
output limits of three sentences / 500 characters. Those overrides were removed
from `/home/dm/.yeoman/policy.json`. A before/after comparison through
`PolicyConfig` and `PolicyEngine` confirms identical effective policy for all
14 configured WhatsApp chats.

Evidence: runtime policy channel default near line 100, chat overrides from line
158 onward; `packages/gateway/yeoman_gateway/policy/engine.py:229` performs deep
inheritance. Other repeated fields, especially reply-budget targets and tool
lists, remain because they are chat-specific or list replacement semantics would
change behavior.

Keep intentional differences, including tool lists and chat-specific reply budgets.
Lists replace inherited lists rather than extending them. Do not promote group
permissions into channel defaults merely to reduce repetition.

### 2. Consolidate Bridge setting ownership — implemented

The old config contained both `channels.whatsapp` and `runtime.whatsappBridge`,
while the active Bridge service also sets host, port, auth/media paths and receipt
flags through environment variables. The duplicate JSON startup timeouts were
30000 versus 15000 ms.

Current Gateway runtime consumers use channel settings:
`channels/whatsapp_runtime.py:506` and `channels/whatsapp.py:228`.
The runtime block was not used by Gateway operations. `Config` no longer exposes
that second block; `_migrate_config_with_change` folds old values into
`channels.whatsapp` with channel values taking precedence. The Doctor now checks
only the channel token. The standalone systemd Bridge remains unchanged.

### 3. Clarify model configuration ownership and inventory — bounded cleanup

33 profiles remain; 22 have no direct references in config routes or policy.
This is not evidence that all 23 can be deleted: five live Overseer runbooks
explicitly reference `overseerDefault`, and profiles can be selected manually.

`agents.defaults.subagentModel` (runtime config line 422) selects Gemini, while
`subagentFast` and `subagentDefault` describe GPT-4o-mini. The responder receives
the former directly (`app/bootstrap.py:458`, `adapters/responder_llm.py:478`).
Editing the apparent default profile therefore does not change spawned subagents.

Assistant model configuration is also duplicated. Gateway bootstrap prefers
`assistant.reply` over `agents.defaults.model` (`app/bootstrap.py:348`), while
the CLI status and agent paths read the latter. Both currently agree.

The invalid, unreferenced `ttsElevenlabs` preset was removed. The remaining
unreferenced profiles were retained because they are manual presets and profile
selection is an exposed runtime capability; Overseer runbook references were
also preserved. `assistantDefault` and `subagentModel` were not changed.

### 4. Fix serialization before attempting a minimal config.json — partially implemented

`packages/shared/yeoman_shared/config/loader.py:116` migrates, fills defaults,
validates, serializes the complete model and rewrites config when different.
Deleting default-valued fields is therefore not durable. Several schema models
also use `extra="ignore"`, so misspelled fields can be ignored and dropped during
this normalization. Policy schema instead rejects unknown fields.

The Bridge cleanup now has an explicit migration path: normal loading removes the
legacy block only after folding its values into the canonical channel settings.
General default elision and strict rejection of every unknown config key remain
out of scope because the current schema deliberately allows forward-compatible
fields in several submodels.

### 5. Remove misleading disabled provider preset — implemented

The runtime preset `ttsElevenlabs` used provider `elevenlabs_tts_DISABLED`, which
was not a supported disabled state:
`packages/gateway/yeoman_gateway/media/tts.py:594` accepts `elevenlabs_tts` or
`elevenlabs`, otherwise returning `tts_provider_unsupported` near line 640.
No current route referenced it, so it was removed rather than retained as an
invalid manually selectable preset.

### 6. Refresh environment documentation — implemented

The runtime `.env.example` now documents all nine keys present in `.env`, including
MiMo, market-data integrations and the Telegram owner chat identifier used by the
Overseer alert service. Only empty placeholders and descriptions were added.
Environment precedence is process environment > dotenv > config secret fields
for the loader's mapped provider/channel/tool settings.

## Boundaries that should remain distinct

- `config.memory` controls the memory subsystem; `policy.memoryNotes` controls
  capture eligibility and batching. Both have current consumers. Keep this split.
- `policy.fileAccess` and the mount allowlist are separate authorization layers,
  not interchangeable copies. The reviewed paths align; do not drop either layer.
- `replyBudget.hardMaxChars` is not an unconditional response cap. Domain-sensitive
  and researched answers disable that cap (`reply_budget.py:155`); authorized
  long-form requests use a separate cap. Document this before changing numbers.
- `consciousness` and per-chat `spontaneity` separate mechanism from permission.
- Persona evolution is enabled with automatic application. Base persona text may
  be supplemented by a sibling `.evolution.md` file (`policy/persona.py:40`).
  These files are behavior inputs worth including in a subsequent prompt review.

## Validation limits

Validation used direct schema/migration functions, the actual `load_config` path
on a temporary file, the effective-policy helper, targeted Shared/Gateway tests,
Ruff and shell syntax checks. Gateway and Overseer were restarted after the source
change; both are active, the Gateway IPC socket is present, and the Bridge remains
active on 127.0.0.1:3001. No provider availability, authentication, message
delivery, or complete security audit is implied.
