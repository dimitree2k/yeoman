# Changelog

## Unreleased

### Overseer
- Added deterministic stale agent-session cleanup for old `mosh-server -> bash -> codex/claude` trees, with a 1-hour current-session guard and a daily 04:00 starter runbook.
- Fixed cron trigger initialization so overseer restarts do not replay missed daily runbooks immediately on startup.

## v1.0.0 — Apr 2026

### Smart Context Windowing
- Ambient context window is now skipped for DM chats — session history already provides full context, eliminating redundant tokens.
- Added `session_history_limit` (default 15) and `session_history_limit_group` (default 20) to WhatsApp config for configurable session history sizes.
- Added per-chat `sessionHistoryLimit` policy override in `policy.json` for fine-grained control.
- Added `/new` command — inserts a session boundary marker. History stops at the boundary, providing a lightweight "fresh start" without deleting conversation history.
- Added preflight heuristic that detects backward references ("as we discussed", "you mentioned") and expands the history window 3x (capped at 50) to surface relevant context automatically.
- Added `recall_conversation` tool — lets the LLM search across all session history (including past boundaries) for messages matching a query, as a fallback when the heuristic isn't enough.
- Added system prompt hint directing the LLM to use `recall_conversation` when users reference something outside visible context.

## v0.9.0 — Mar 2026

### Autonomous Workflows
- Added multi-step workflow support to the CronService with job chaining, approval gates, and output passing between steps.
- Extended `CronPayload` with workflow fields: `next_job_id`, `requires_approval`, `approval_channel`, `input_from_previous`, `workflow_id`, `workflow_step`, `max_chain_depth`.
- Added `WorkflowState` manager for pending approval gates with atomic JSON persistence and asyncio-safe concurrency.
- Added workflow chain helpers: `build_chained_prompt` (truncated output injection), `detect_chain_cycle`, `is_chain_failure`.
- Wired chaining logic into `on_cron_job` bootstrap callback with recursive `_handle_chain` supporting direct chains and approval-gated chains.
- Added `ApprovalMiddleware` to the pipeline — intercepts owner messages matching `wf-approve-*` codes, consumes the approval, and triggers the next workflow step.
- Added `add_workflow` and `workflow_list` actions to the CronTool for batch workflow creation and status display.
- Added approval expiry check to the CronService timer loop with owner notification on expiry.
- Session keys now include a per-run UUID to isolate recurring workflow executions.

## v0.8.0 — Mar 2026

### Event Backbone
- Added typed event pub/sub to the message bus: `WebhookEvent`, `OverseerCommand`, `SystemEvent` frozen dataclasses with a `GatewayEvent` union type.
- Extended `MessageBus` with a bounded event queue and an unbounded IPC queue (overseer commands are never dropped), plus `subscribe_event` / `publish_event` / `dispatch_events` methods.
- Added `IpcConfig`, `WebhookSourceConfig`, and `WebhooksConfig` to the shared config schema.

### IPC (Gateway ↔ Overseer)
- Added `GatewaySocket` — Unix domain socket server receiving commands from the overseer (send_message, trigger_agent_turn, publish_event, get_session_state, ping). Rate-limited, `chmod 0o600`.
- Added `OverseerClient` — gateway-side client for querying the overseer socket with connect-per-command retry and exponential backoff.
- Added `GatewayClient` — overseer-side client for sending commands to the gateway socket (send_message, trigger_agent_turn, publish_event, notify_runbook_result).
- Added `get_runbook_status` command to the overseer socket server.

### Webhooks
- Added HMAC-SHA256 verified webhook router (`/webhooks/{source}`) mounted on the existing FastAPI server.
- GitHub-specific payload normalization for push and pull_request events; generic truncated JSON fallback for other sources.
- Per-source rate limiting and event type filtering via config.

### Wiring
- Wired event dispatch loop, IPC socket lifecycle, and webhook router into the gateway bootstrap and runtime.

## v0.7.1 — Mar 2026

### Provider & Performance
- Moved temporal grounding out of the system prompt into a per-turn system message, enabling LLM prefix caching across turns (~90% input token discount on Anthropic).
- Added LiteLLM transient retry (`num_retries=2`) for automatic recovery from 429/500/network errors.

### Reliability
- Added per-session `asyncio.Lock` in `LLMResponder._generate()` to serialize concurrent messages for the same chat, preventing session state corruption.

## v0.7.0 — Mar 2026

### Overseer
- Fixed 4 broken overseer runbooks (ops-source-cleanup, ops-memory-prune, governance-policy-audit, quality-response-sample) that referenced nonexistent databases, wrong table names, and invalid health check names.
- Added `yeoman overseer trigger <name>` CLI command for manually running runbooks outside their cron schedule, with optional `--dry` validation mode.

### CLI & Daemon
- Switched gateway and overseer daemon processes to use Loguru rotating file sinks instead of raw file handle redirection, fixing log rotation and compression.
- Added crash logging with `logger.exception` for unhandled gateway errors.
- Overseer start now uses `RotatingFileHandler` (10 MB, 3 backups) instead of unbounded `FileHandler`.
- Import sorting cleanup across CLI modules.

### Deploy
- Simplified overseer restart in deploy flow: daemon output routed to DEVNULL (logs go to their own file sinks).

## v0.6.0 — Mar 2026

### Memory & Contacts
- Added contact-scope memory recall with `reply_to_jid` support and person-profile routing.
- Added soft-delete support to the memory store and `forget()` / `forget_confirm()` flows in `MemoryService`.
- Added `upsert_field` support in the contacts service and store, plus merge/dedupe fixes.

### Admin & Ops
- Added `/forget` for preview-confirm memory deletion from admin flows.
- Added `OpsTool` with `system_stats`, `log_scan`, and `service_status` actions.
- Added `OpsManageTool` with a 4-digit confirmation flow for guarded management actions.
- Bundled the new ops skill and CLI command reference updates.
- Removed the legacy `pi_stats` tool in favor of the new ops tooling.

### Security & Web
- Hardened the web tool with DNS pinning, rate limiting, content-type filtering, streaming size guards, and audit logging.
- Wired `WebToolsConfig` through all web tool registration sites.
- Skipped LLM security classification for owner private DMs.

### Tests
- Added coverage for ops tooling, web hardening, memory forget flows, person-profile extraction, contacts updates, and `/forget` integration.

### Docs
- Added ops tool and `/forget` design/spec documents.
- Removed obsolete completed planning docs from `plans/`.

## v0.5.0 — Mar 2026

### Contacts CRM
- Full contacts subsystem with SQLite store, CRUD tool, identity resolution in pipeline
- Roster injection into LLM context, @Name mention resolution in outbound messages
- One-time memory backfill linking nodes to contact_id

### Voice
- Recording indicator (microphone icon) for WhatsApp voice messages
- Voice send result reporting to source chat
- Fish Audio TTS provider with `/voice` command
- `senderName` field support

### Telemetry
- Langfuse tracing module with REST API client
- Agent loop instrumentation, lifecycle init/shutdown

### Fixes
- WhatsApp mention tokens: strip trailing punctuation for reliable matching
- Doctor: detect CalDAV interpreter mismatch
- Tracing: add try/finally for spans

### Docs
- WhatsApp bridge health monitoring design spec
- Standardize CLI runtime entrypoints

### Build
- Update Python dependencies to latest versions
- Bundle browser skill

## v0.4.0 — Mar 2026

### Diagnostics
- Added bundled `agent-doctor` skill and `yeoman doctor` command
- Added user-facing health-check documentation and issue-ID based fix guidance

### Release Hygiene
- Aligned package metadata and runtime version string to `0.4.0`

## v0.3.0 — Mar 2026

### Calendar
- Full CalDAV integration: create, read, update, delete events via natural language
- `CalendarTool` registered in gateway with session reconnection on expiry
- Supports all-day events, UID-based lookup, and multi-calendar accounts

### WhatsApp
- Mention support for `send_text` and `send_media`
- Separate debounce timing for media messages (reduces duplicate processing)

### Agent & Skills
- **Sync subagent**: synchronous subagent execution for tool-within-tool patterns
- **Fact check tool**: producer-reviewer pattern via sync subagent for verifiable claims
- Fact verification guardrails in system prompt to reduce hallucination
- YouTube / summarize skill: improved trigger detection for bare URLs and varied phrases
- Style Persistence: anti-repetition and brief-acknowledgment rules in context builder

### Security
- Reduced false positives in prompt injection classifier
- Reduced false positives in persona manipulation classifier

### Session & Memory
- Tool call traces persisted to session JSONL for auditability
- `get_history()` defensively skips legacy/malformed rows missing `content` key (crash fix)

### Ops
- Temperature reduced 0.8 → 0.6 for `assistantDefault`, `moneyboy`, `grokFast` profiles

## v0.2.0 — Feb 2026

- Renamed project from nanobotstack to yeoman
- Runtime directory migrated from `~/.nanobot` to `~/.yeoman` (auto-migrated on first run)

## v0.1.3 — Feb 2026

### Policy Engine
- Deterministic per-channel, per-chat access control with hot-reload
- Admin commands via `/policy` namespace (WhatsApp owner DM)
- `nanobot policy explain` for debugging reply decisions
- Blocked senders, talkative cooldown for groups
- Emergency `/panic` shutdown command
- Scoped file access grants with blocked paths/patterns override

### Multi-Channel Runtime
- **Telegram**: typing indicator, `/reset` and `/help` commands, proxy support, conflict handling
- **Discord**: full adapter with typing indicator
- **Feishu**: WebSocket long-connection (no public IP required)
- **WhatsApp**: Baileys bridge (TypeScript) with protocol v2, token auth, reply context window, bridge lifecycle CLI, read receipts, markdown formatting, reaction markers, owner alerts for new group additions

### Memory System
- SQLite-backed hybrid store (FTS + embeddings)
- Semantic capture and recall with scope filters
- Background memory notes with per-chat configuration
- CLI: `memory status`, `search`, `add`, `prune`, `reindex`, `backfill`
- Session reset with memory consolidation (`/new` command)

### Voice & Media
- STT: Groq Whisper transcription for voice messages
- TTS: ElevenLabs with per-chat voice policy and wake phrases
- Vision: image recognition in Telegram
- Media persistence pipeline for WhatsApp

### Security
- Three-stage security middleware (input → tool → output) with sensitive data redaction
- Workspace restriction (`tools.restrictToWorkspace`) for all file/exec tools
- Bubblewrap (bwrap) exec isolation with per-session containers, capacity management, idle timeout
- Scoped file access grants for owner sessions
- Hardened bridge security with mandatory localhost binding and token auth

### Providers
- Declarative provider registry (`providers/registry.py`) — single source of truth
- 11 providers: OpenRouter, AiHubMix, Anthropic, OpenAI, DeepSeek, Gemini, Groq, DashScope, Moonshot, Zhipu AI, vLLM
- Auto-prefix, env var fallback, gateway detection, model overrides

### Core & Architecture
- Hexagonal architecture with typed port interfaces
- Orchestrator intent system (typing, outbound, reactions, memory, metrics)
- Interleaved chain-of-thought in agent loop
- Temporal grounding and fact guardrails
- Reaction-based UX for security blocks and idea capture
- Ideas capture and backlog detection

### Tools & Skills
- Cron scheduler with one-shot `at` parameter and voice broadcast
- Bundled skills: github, weather, summarize, tmux, cron, ideas-inbox, skill-creator
- `deep_research` tool (Tavily)
- `edit_file` tool and sub-agent improvements

### CLI & Ops
- `yeoman gateway` with daemon control (start/stop/restart)
- `yeoman logs` — unified log viewer
- `yeoman config migrate-to-env` — move secrets to `.env`
- `yeoman policy annotate-whatsapp-comments` — auto-fill group names
- Docker support with volume-mounted config
