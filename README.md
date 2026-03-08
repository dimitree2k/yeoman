<div align="center">
  <img src="yeoman_logo.png" alt="yeoman" width="400">

  <h3>Policy-first personal AI assistant runtime</h3>

  <p>
    <img src="https://img.shields.io/badge/python-≥3.14-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-22c55e" alt="License">
    <img src="https://img.shields.io/badge/core-~18k_lines-blueviolet" alt="Lines">
    <a href="#channels"><img src="https://img.shields.io/badge/channels-Telegram%20·%20WhatsApp%20·%20Discord%20·%20Feishu-0088cc" alt="Channels"></a>
  </p>
</div>

---

**yeoman** is a lightweight, multi-channel AI assistant runtime with deterministic policy control, long-term memory, voice I/O, and tool sandboxing.

> Originally inspired by [HKUDS/nanobot](https://github.com/HKUDS/nanobot). MIT license preserved.

## Highlights

| | |
|---|---|
| **Policy engine** | Deterministic per-channel, per-chat access control with hot-reload — no ad-hoc ACLs |
| **Multi-channel** | Telegram, WhatsApp (Baileys bridge), Discord, Feishu — unified pipeline |
| **Memory** | SQLite-backed semantic + FTS recall with session context and background notes |
| **Voice** | STT via Groq Whisper, TTS via ElevenLabs / OpenRouter — bidirectional voice in WhatsApp |
| **Tools & skills** | Sandboxed execution (bubblewrap), extensible skill system (OpenClaw-compatible) |
| **11 LLM providers** | OpenRouter, Anthropic, OpenAI, DeepSeek, Gemini, Groq, DashScope, Moonshot, Zhipu, AiHubMix, local vLLM — via LiteLLM |

## Architecture

```
Channel → Bus (inbound) → 13-stage Middleware Pipeline → OrchestratorIntent[]
  01 Normalize → 02 Dedup → 03 Archive → 04 Context → 05 Admin
  → 06 Policy → 07 Idea Capture → 08 Access Control → 09 New Chat
  → 10 No-Reply → 11 Security → 12 LLM Response → 13 Outbound
Intent dispatch → Bus (outbound/reaction) → Channel → User
```

Hexagonal / ports-and-adapters. `core/ports.py` defines interfaces (`PolicyPort`, `ResponderPort`, `ReplyArchivePort`, `SecurityPort`, `TelemetryPort`); adapters implement them. The pipeline emits typed `OrchestratorIntent` objects; channels react asynchronously. Media (ASR/TTS/vision) is cross-cutting — channels enrich inbound, the responder synthesizes outbound.

<p align="center">
  <img src="yeoman_arch.svg" alt="architecture" width="900">
</p>

## Install

```bash
# From source (recommended for development)
git clone https://github.com/dimitree2k/yeoman.git
cd yeoman
pip install -e .

# With uv
uv tool install yeoman

# From PyPI (once published)
pip install yeoman
```

## Quick Start

**1. Initialize**

```bash
yeoman onboard
```

**2. Add API keys** — pick any method:

| Method | Location | Notes |
|--------|----------|-------|
| `.env` file | `~/.yeoman/.env` | Recommended. `yeoman config migrate-to-env` can generate it |
| Environment variables | Shell / systemd | `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, etc. |
| Config file | `~/.yeoman/config.json` | Works but `.env` is preferred for secrets |

```bash
# Example: set model in config, key in .env
echo 'OPENROUTER_API_KEY=sk-or-v1-xxx' >> ~/.yeoman/.env
```

```json
{
  "agents": {
    "defaults": { "model": "anthropic/claude-opus-4-5" }
  }
}
```

**3. Chat**

```bash
yeoman agent -m "Hello!"
```

> [!TIP]
> For local models, point `providers.vllm.apiBase` at any OpenAI-compatible server (vLLM, Ollama, etc).

<a id="channels"></a>
## Channels

All channels are configured in `~/.yeoman/config.json` and access-controlled via `~/.yeoman/policy.json`.

| Channel | Complexity | Notes |
|---------|-----------|-------|
| **Telegram** | Easy | Bot token from @BotFather |
| **Discord** | Easy | Bot token + MESSAGE CONTENT intent |
| **WhatsApp** | Medium | QR link via `yeoman channels login` (Node.js ≥18) |
| **Feishu** | Medium | WebSocket — no public IP needed |

Start all enabled channels:

```bash
yeoman gateway
```

<details>
<summary><strong>Channel setup details</strong></summary>

### Telegram

```json
{ "channels": { "telegram": { "enabled": true, "token": "YOUR_BOT_TOKEN" } } }
```

### Discord

```json
{ "channels": { "discord": { "enabled": true, "token": "YOUR_BOT_TOKEN" } } }
```

Invite with scopes: `bot` · Permissions: `Send Messages`, `Read Message History`.

### WhatsApp

```bash
yeoman channels login   # scan QR
yeoman gateway           # start
```

```json
{ "channels": { "whatsapp": { "enabled": true } } }
```

Supports voice (STT + TTS), bridge lifecycle management (`yeoman channels bridge start|stop|restart|status`), and media persistence.

### Feishu

```bash
pip install yeoman[feishu]
```

```json
{
  "channels": {
    "feishu": { "enabled": true, "appId": "cli_xxx", "appSecret": "xxx" }
  }
}
```

</details>

## Policy Engine

`~/.yeoman/policy.json` controls four dimensions per channel and chat:

| Dimension | Modes |
|-----------|-------|
| **Who can talk** | `everyone` · `allowlist` · `owner_only` |
| **When to reply** | `all` · `off` · `mention_only` · `allowed_senders` · `owner_only` |
| **Allowed tools** | `all` · `allowlist` (with deny overrides) |
| **Persona** | Per-chat persona file selection |

Merge precedence: `defaults` → `channels.<ch>.default` → `channels.<ch>.chats.<id>`

Policy is hot-reloaded — no restart needed. Debug with:

```bash
yeoman policy explain --channel telegram --chat -1001234567890 --sender "12345|User"
```

Owner response controls (WhatsApp owner only):

```text
/stop              # pause current chat until /start
/stop all          # pause every chat until /start all
/start             # resume current chat
/start all         # resume all chats
/pause 30min       # pause current chat for a duration
/pause all 1h      # pause all chats for a duration
```

Supported pause units: `s`, `min`, `h`, `d` (for example `45s`, `15min`, `2h`, `1d`).

## Providers

| Provider | Type | |
|----------|------|--|
| OpenRouter | LLM gateway | [openrouter.ai](https://openrouter.ai) |
| AiHubMix | LLM gateway | [aihubmix.com](https://aihubmix.com) |
| Anthropic | LLM (Claude) | [console.anthropic.com](https://console.anthropic.com) |
| OpenAI | LLM (GPT) | [platform.openai.com](https://platform.openai.com) |
| DeepSeek | LLM | [platform.deepseek.com](https://platform.deepseek.com) |
| Gemini | LLM | [aistudio.google.com](https://aistudio.google.com) |
| Groq | LLM + STT (Whisper) | [console.groq.com](https://console.groq.com) |
| DashScope | LLM (Qwen) | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |
| Moonshot | LLM (Kimi) | [platform.moonshot.cn](https://platform.moonshot.cn) |
| Zhipu AI | LLM (GLM) | [open.bigmodel.cn](https://open.bigmodel.cn) |
| vLLM | Local LLM | Any OpenAI-compatible server |

Adding a new provider requires only 2 changes: a `ProviderSpec` in `providers/registry.py` and a config field in `config/schema.py`.

## Security

| Feature | Description |
|---------|-------------|
| **Policy engine** | Deterministic access control — no ad-hoc ACLs |
| **Workspace restriction** | `tools.restrictToWorkspace: true` sandboxes all file/exec tools |
| **Exec isolation** | Linux bubblewrap sandboxing with per-session containers |
| **Scoped file grants** | Explicit path grants with blocked paths/patterns override |
| **I/O validation** | Three-stage security middleware: input → tool → output checks with sensitive data redaction |

## Health Check

If yeoman includes the bundled `agent-doctor` skill, you can run a local health check for memory,
cron, config, workspace files, gateway/bridge, security posture, and system prerequisites.

Ask the agent:

```text
diagnose yeoman
run a health check
check what is broken
```

Or run it directly:

```bash
yeoman doctor
```

Use it:

- right after onboarding
- after editing `config.json`, `.env`, or `policy.json`
- after runtime/dependency upgrades
- when memory, gateway, WhatsApp, or cron behavior seems off

Exit codes:

- `0` no problems found
- `1` warnings or critical issues found

The doctor does not auto-fix anything; it reports findings and proposed fixes first.

## CLI Reference

| Command | Description |
|---------|-------------|
| `yeoman onboard` | Initialize config & workspace |
| `yeoman agent -m "..."` | Single-shot chat |
| `yeoman agent` | Interactive chat |
| `yeoman gateway` | Start all enabled channels |
| `yeoman status` | Runtime status |
| `yeoman doctor` | Run health checks and report issues |
| `yeoman logs` | View gateway/bridge logs |
| **Channels** | |
| `yeoman channels login` | Link WhatsApp (scan QR) |
| `yeoman channels status` | Show channel status |
| `yeoman channels bridge start\|stop\|restart\|status` | Manage WhatsApp bridge |
| **Policy** | |
| `yeoman policy path` | Show policy file location |
| `yeoman policy explain` | Debug policy decisions for a chat/sender |
| `yeoman policy cmd "/policy ..."` | Run policy commands from CLI |
| `yeoman policy annotate-whatsapp-comments` | Auto-fill WhatsApp group names in policy |
| **Memory** | |
| `yeoman memory status` | Memory backend info and counters |
| `yeoman memory search --query "..."` | Search long-term memory |
| `yeoman memory add --text "..."` | Insert manual memory entry |
| `yeoman memory prune` | Retention cleanup |
| `yeoman memory reindex` | Rebuild FTS index |
| `yeoman memory notes status\|set` | Per-chat background notes config |
| **Config** | |
| `yeoman config migrate-to-env` | Move secrets from config.json to .env |
| **Cron** | |
| `yeoman cron list\|add\|remove\|enable\|run` | Manage scheduled tasks |
| `yeoman cron add-voice` | Schedule voice broadcast jobs |

## Docker

```bash
docker build -t yeoman .
docker run -v ~/.yeoman:/root/.yeoman -p 18790:18790 yeoman gateway
```

## Project Structure

```
yeoman/
├── agent/        Core agent loop, prompt builder, skills, tools
├── core/         Orchestrator pipeline, ports, intents, models
├── adapters/     Port implementations (policy, LLM, archive, telemetry)
├── app/          Runtime bootstrap
├── channels/     Telegram, WhatsApp, Discord, Feishu
├── bus/          Async message queue with dedup
├── config/       Pydantic schema, loader, defaults
├── providers/    LLM registry, LiteLLM wrapper, transcription
├── policy/       Engine, schema, identity normalization, personas
├── memory/       SQLite store, embeddings, extractor, sessions
├── media/        ASR, TTS, vision, routing
├── storage/      Inbound message archive
├── security/     Rule engine, bubblewrap isolation
├── skills/       Bundled skills (github, weather, cron, tmux...)
├── session/      Conversation session state
├── cron/         Scheduled task service
├── heartbeat/    Proactive wake-up timer
├── utils/        Shared utilities
└── cli/          typer commands
bridge/           WhatsApp bridge (TypeScript / Baileys)
```

## Changelog

### v0.4.0 — Mar 2026

#### Diagnostics
- Added bundled `agent-doctor` skill and `yeoman doctor` command
- Added user-facing health-check documentation and issue-ID based fix guidance

#### Release Hygiene
- Aligned package metadata and runtime version string to `0.4.0`

### v0.3.0 — Mar 2026

#### Calendar
- Full CalDAV integration: create, read, update, delete events via natural language
- `CalendarTool` registered in gateway with session reconnection on expiry
- Supports all-day events, UID-based lookup, and multi-calendar accounts

#### WhatsApp
- Mention support for `send_text` and `send_media`
- Separate debounce timing for media messages (reduces duplicate processing)

#### Agent & Skills
- **Sync subagent**: synchronous subagent execution for tool-within-tool patterns
- **Fact check tool**: producer-reviewer pattern via sync subagent for verifiable claims
- Fact verification guardrails in system prompt to reduce hallucination
- YouTube / summarize skill: improved trigger detection for bare URLs and varied phrases
- Style Persistence: anti-repetition and brief-acknowledgment rules in context builder

#### Security
- Reduced false positives in prompt injection classifier
- Reduced false positives in persona manipulation classifier

#### Session & Memory
- Tool call traces persisted to session JSONL for auditability
- `get_history()` defensively skips legacy/malformed rows missing `content` key (crash fix)

#### Ops
- Temperature reduced 0.8 → 0.6 for `assistantDefault`, `moneyboy`, `grokFast` profiles

### v0.2.0 — Feb 2026

- Renamed project from nanobotstack to yeoman
- Runtime directory migrated from `~/.nanobot` to `~/.yeoman` (auto-migrated on first run)

### v0.1.3 — Feb 2026

#### Policy Engine
- Deterministic per-channel, per-chat access control with hot-reload
- Admin commands via `/policy` namespace (WhatsApp owner DM)
- `nanobot policy explain` for debugging reply decisions
- Blocked senders, talkative cooldown for groups
- Emergency `/panic` shutdown command
- Scoped file access grants with blocked paths/patterns override

#### Multi-Channel Runtime
- **Telegram**: typing indicator, `/reset` and `/help` commands, proxy support, conflict handling
- **Discord**: full adapter with typing indicator
- **Feishu**: WebSocket long-connection (no public IP required)
- **WhatsApp**: Baileys bridge (TypeScript) with protocol v2, token auth, reply context window, bridge lifecycle CLI, read receipts, markdown formatting, reaction markers, owner alerts for new group additions

#### Memory System
- SQLite-backed hybrid store (FTS + embeddings)
- Semantic capture and recall with scope filters
- Background memory notes with per-chat configuration
- CLI: `memory status`, `search`, `add`, `prune`, `reindex`, `backfill`
- Session reset with memory consolidation (`/new` command)

#### Voice & Media
- STT: Groq Whisper transcription for voice messages
- TTS: ElevenLabs with per-chat voice policy and wake phrases
- Vision: image recognition in Telegram
- Media persistence pipeline for WhatsApp

#### Security
- Three-stage security middleware (input → tool → output) with sensitive data redaction
- Workspace restriction (`tools.restrictToWorkspace`) for all file/exec tools
- Bubblewrap (bwrap) exec isolation with per-session containers, capacity management, idle timeout
- Scoped file access grants for owner sessions
- Hardened bridge security with mandatory localhost binding and token auth

#### Providers
- Declarative provider registry (`providers/registry.py`) — single source of truth
- 11 providers: OpenRouter, AiHubMix, Anthropic, OpenAI, DeepSeek, Gemini, Groq, DashScope, Moonshot, Zhipu AI, vLLM
- Auto-prefix, env var fallback, gateway detection, model overrides

#### Core & Architecture
- Hexagonal architecture with typed port interfaces
- Orchestrator intent system (typing, outbound, reactions, memory, metrics)
- Interleaved chain-of-thought in agent loop
- Temporal grounding and fact guardrails
- Reaction-based UX for security blocks and idea capture
- Ideas capture and backlog detection

#### Tools & Skills
- Cron scheduler with one-shot `at` parameter and voice broadcast
- Bundled skills: github, weather, summarize, tmux, cron, ideas-inbox, skill-creator
- `deep_research` tool (Tavily)
- `edit_file` tool and sub-agent improvements

#### CLI & Ops
- `yeoman gateway` with daemon control (start/stop/restart)
- `yeoman logs` — unified log viewer
- `yeoman config migrate-to-env` — move secrets to `.env`
- `yeoman policy annotate-whatsapp-comments` — auto-fill group names
- Docker support with volume-mounted config

---

<sub>MIT License · Originally inspired by [HKUDS/nanobot](https://github.com/HKUDS/nanobot)</sub>
