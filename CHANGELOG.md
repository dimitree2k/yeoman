# Changelog

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
