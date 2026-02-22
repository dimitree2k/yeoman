<div align="center">
  <img src="nanobot_logo.png" alt="nanobot-stack" width="400">

  <h3>Policy-first personal AI assistant runtime</h3>

  <p>
    <img src="https://img.shields.io/badge/python-≥3.14-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-22c55e" alt="License">
    <img src="https://img.shields.io/badge/core-~18k_lines-blueviolet" alt="Lines">
    <a href="#channels"><img src="https://img.shields.io/badge/channels-Telegram%20·%20WhatsApp%20·%20Discord%20·%20Feishu-0088cc" alt="Channels"></a>
  </p>
</div>

---

**nanobot-stack** is a lightweight, multi-channel AI assistant runtime with deterministic policy control, long-term memory, voice I/O, and tool sandboxing — ~18k lines of Python.

> Originally forked from [HKUDS/nanobot](https://github.com/HKUDS/nanobot). This project has since diverged significantly with a rewritten policy engine, hexagonal architecture, multi-channel support, memory system, security hardening, and more. MIT license preserved. See [UPSTREAM.md](UPSTREAM.md) for details.

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
Channel → Manager → Bus/Queue → Orchestrator
  → Policy (decision) → Security (validation)
  → LLM Responder → Tool Execution → Memory Capture
  → Channel → User
```

Hexagonal / ports-and-adapters. `core/ports.py` defines interfaces (`PolicyPort`, `ResponderPort`, `ReplyArchivePort`, `SecurityPort`, `TelemetryPort`); adapters implement them. Orchestrator emits typed `OrchestratorIntent` objects; channels react asynchronously.

<p align="center">
  <img src="nanobot_arch.png" alt="architecture" width="700">
</p>

## Install

```bash
# From source (recommended for development)
git clone https://github.com/dimitree2k/nanobot-stack.git
cd nanobot-stack
pip install -e .

# With uv
uv tool install nanobot-stack

# From PyPI (once published)
pip install nanobot-stack
```

## Quick Start

**1. Initialize**

```bash
nanobot onboard
```

**2. Add API keys** — pick any method:

| Method | Location | Notes |
|--------|----------|-------|
| `.env` file | `~/.nanobot/.env` | Recommended. `nanobot config migrate-to-env` can generate it |
| Environment variables | Shell / systemd | `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, etc. |
| Config file | `~/.nanobot/config.json` | Works but `.env` is preferred for secrets |

```bash
# Example: set model in config, key in .env
echo 'OPENROUTER_API_KEY=sk-or-v1-xxx' >> ~/.nanobot/.env
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
nanobot agent -m "Hello!"
```

> [!TIP]
> For local models, point `providers.vllm.apiBase` at any OpenAI-compatible server (vLLM, Ollama, etc).

<a id="channels"></a>
## Channels

All channels are configured in `~/.nanobot/config.json` and access-controlled via `~/.nanobot/policy.json`.

| Channel | Complexity | Notes |
|---------|-----------|-------|
| **Telegram** | Easy | Bot token from @BotFather |
| **Discord** | Easy | Bot token + MESSAGE CONTENT intent |
| **WhatsApp** | Medium | QR link via `nanobot channels login` (Node.js ≥18) |
| **Feishu** | Medium | WebSocket — no public IP needed |

Start all enabled channels:

```bash
nanobot gateway
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
nanobot channels login   # scan QR
nanobot gateway           # start
```

```json
{ "channels": { "whatsapp": { "enabled": true } } }
```

Supports voice (STT + TTS), bridge lifecycle management (`nanobot channels bridge start|stop|restart|status`), and media persistence.

### Feishu

```bash
pip install nanobot-stack[feishu]
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

`~/.nanobot/policy.json` controls four dimensions per channel and chat:

| Dimension | Modes |
|-----------|-------|
| **Who can talk** | `everyone` · `allowlist` · `owner_only` |
| **When to reply** | `all` · `off` · `mention_only` · `allowed_senders` · `owner_only` |
| **Allowed tools** | `all` · `allowlist` (with deny overrides) |
| **Persona** | Per-chat persona file selection |

Merge precedence: `defaults` → `channels.<ch>.default` → `channels.<ch>.chats.<id>`

Policy is hot-reloaded — no restart needed. Debug with:

```bash
nanobot policy explain --channel telegram --chat -1001234567890 --sender "12345|User"
```

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

## CLI Reference

| Command | Description |
|---------|-------------|
| `nanobot onboard` | Initialize config & workspace |
| `nanobot agent -m "..."` | Single-shot chat |
| `nanobot agent` | Interactive chat |
| `nanobot gateway` | Start all enabled channels |
| `nanobot status` | Runtime status |
| `nanobot logs` | View gateway/bridge logs |
| **Channels** | |
| `nanobot channels login` | Link WhatsApp (scan QR) |
| `nanobot channels status` | Show channel status |
| `nanobot channels bridge start\|stop\|restart\|status` | Manage WhatsApp bridge |
| **Policy** | |
| `nanobot policy path` | Show policy file location |
| `nanobot policy explain` | Debug policy decisions for a chat/sender |
| `nanobot policy cmd "/policy ..."` | Run policy commands from CLI |
| `nanobot policy annotate-whatsapp-comments` | Auto-fill WhatsApp group names in policy |
| **Memory** | |
| `nanobot memory status` | Memory backend info and counters |
| `nanobot memory search --query "..."` | Search long-term memory |
| `nanobot memory add --text "..."` | Insert manual memory entry |
| `nanobot memory prune` | Retention cleanup |
| `nanobot memory reindex` | Rebuild FTS index |
| `nanobot memory notes status\|set` | Per-chat background notes config |
| **Config** | |
| `nanobot config migrate-to-env` | Move secrets from config.json to .env |
| **Cron** | |
| `nanobot cron list\|add\|remove\|enable\|run` | Manage scheduled tasks |
| `nanobot cron add-voice` | Schedule voice broadcast jobs |

## Docker

```bash
docker build -t nanobot .
docker run -v ~/.nanobot:/root/.nanobot -p 18790:18790 nanobot gateway
```

## Project Structure

```
nanobot/
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

### Unreleased

**Ambient context & voice expansion**
- Ambient context window for group conversations
- OpenRouter audio TTS provider
- Voice broadcast scheduling via cron
- `.env` file support for provider API keys (`nanobot config migrate-to-env`)
- Tavily deep research tool (replacing Brave Search)
- Data layout restructuring and process utilities extraction

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
- `nanobot gateway` with daemon control (start/stop/restart)
- `nanobot logs` — unified log viewer
- `nanobot config migrate-to-env` — move secrets to `.env`
- `nanobot policy annotate-whatsapp-comments` — auto-fill group names
- Docker support with volume-mounted config

---

<sub>MIT License · Originally forked from [HKUDS/nanobot](https://github.com/HKUDS/nanobot) · See [UPSTREAM.md](UPSTREAM.md)</sub>
