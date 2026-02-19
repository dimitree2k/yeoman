<div align="center">
  <img src="nanobot_logo.png" alt="nanobot" width="500">
  <h1>nanobot-stack: Professional Personal AI Assistant Stack</h1>
  <p>
    <img src="https://img.shields.io/badge/PyPI-nanobot--stack%20(pending)-orange" alt="PyPI status">
    <img src="https://img.shields.io/badge/python-≥3.14-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <a href="#chat-apps"><img src="https://img.shields.io/badge/channels-Telegram%20%7C%20WhatsApp%20%7C%20Discord%20%7C%20Feishu-5865F2" alt="Channels"></a>
  </p>
</div>

🐈 **nanobot-stack** is an **ultra-lightweight** personal AI assistant runtime inspired by [Clawdbot](https://github.com/openclaw/openclaw).

## Fork Provenance

This project is an independent fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot), maintained as **nanobot-stack**.

- Original upstream: `HKUDS/nanobot`
- License: MIT (preserved)
- Runtime compatibility: existing `nanobot` command remains supported

⚡️ Delivers core agent functionality in about **13,000** lines of code.

📏 Real-time line count: **~13,000 lines** (run `bash core_agent_lines.sh` to verify anytime)

## 📢 News

- **2026-02 (major runtime cycle)**: moved to a policy-first security model with deterministic admin commands, policy audit/rollback, and stricter operational hardening.
- **2026-02 (platform expansion)**: matured multi-channel runtime (WhatsApp bridge lifecycle, Telegram/Discord/Feishu support), plus better reconnect, dedupe, and message context handling.
- **2026-02 (memory and ops)**: landed memory v2 improvements (semantic capture/recall + background notes), plus stronger CLI/operator workflows for policy and diagnostics.

## Key Features of nanobot:

🪶 **Lean Runtime**: About 13k lines of core runtime code with clear module boundaries.

🔬 **Research-Ready**: Clean, readable code that's easy to understand, modify, and extend for research.

⚡️ **Lightning Fast**: Minimal footprint means faster startup, lower resource usage, and quicker iterations.

💎 **Easy-to-Use**: One-click to deploy and you're ready to go.

## 🏗️ Architecture

<p align="center">
  <img src="nanobot_arch.png" alt="nanobot architecture" width="800">
</p>

## 📦 Install

**Install from source** (latest features, recommended for development)

```bash
git clone https://github.com/dimitree2k/nanobot-stack.git
cd nanobot-stack
pip install -e .
```

**Install with [uv](https://github.com/astral-sh/uv)** (stable, fast)

```bash
uv tool install nanobot-stack
```

**Install from PyPI** (once published)

```bash
pip install nanobot-stack
```

## 🚀 Quick Start

> [!TIP]
> Set your API key in `~/.nanobot/config.json`.
> Get API keys: [OpenRouter](https://openrouter.ai/keys) (Global) · [DashScope](https://dashscope.console.aliyun.com) (Qwen) · [Brave Search](https://brave.com/search/api/) (optional, for web search)

**1. Initialize**

```bash
nanobot onboard
```

**2. Configure** (`~/.nanobot/config.json`)

For OpenRouter - recommended for global users:
```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

**3. Chat**

```bash
nanobot agent -m "What is 2+2?"
```

That's it! You have a working AI assistant in 2 minutes.

> [!NOTE]
> CLI compatibility is preserved: both `nanobot` and `nanobot-stack` entrypoints are available.

## 🖥️ Local Models (vLLM)

Run nanobot with your own local models using vLLM or any OpenAI-compatible server.

**1. Start your vLLM server**

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

**2. Configure** (`~/.nanobot/config.json`)

```json
{
  "providers": {
    "vllm": {
      "apiKey": "dummy",
      "apiBase": "http://localhost:8000/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

**3. Chat**

```bash
nanobot agent -m "Hello from my local LLM!"
```

> [!TIP]
> The `apiKey` can be any non-empty string for local servers that don't require authentication.

<a id="chat-apps"></a>
## 💬 Chat Apps

Talk to your nanobot through Telegram, Discord, WhatsApp, or Feishu — anytime, anywhere.

| Channel | Setup |
|---------|-------|
| **Telegram** | Easy (just a token) |
| **Discord** | Easy (bot token + intents) |
| **WhatsApp** | Medium (scan QR) |
| **Feishu** | Medium (app credentials) |

<details>
<summary><b>Telegram</b> (Recommended)</summary>

**1. Create a bot**
- Open Telegram, search `@BotFather`
- Send `/newbot`, follow prompts
- Copy the token

**2. Configure**

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN"
    }
  }
}
```

> Access/reply/tool/persona rules now live in `~/.nanobot/policy.json` (not `allowFrom`).

**3. Run**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>Discord</b></summary>

**1. Create a bot**
- Go to https://discord.com/developers/applications
- Create an application → Bot → Add Bot
- Copy the bot token

**2. Enable intents**
- In the Bot settings, enable **MESSAGE CONTENT INTENT**
- (Optional) Enable **SERVER MEMBERS INTENT** if you plan to use allow lists based on member data

**3. Get your User ID**
- Discord Settings → Advanced → enable **Developer Mode**
- Right-click your avatar → **Copy User ID**

**4. Configure**

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN"
    }
  }
}
```

**5. Invite the bot**
- OAuth2 → URL Generator
- Scopes: `bot`
- Bot Permissions: `Send Messages`, `Read Message History`
- Open the generated invite URL and add the bot to your server

**6. Run**

```bash
nanobot gateway
```

</details>

<details>
<summary><b>WhatsApp</b></summary>

Requires **Node.js ≥18**.

**1. Link device**

```bash
nanobot channels login
# Scan QR with WhatsApp → Settings → Linked Devices
```

**2. Configure**

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "acceptFromMe": false
    }
  }
}
```

### WhatsApp Voice (STT + TTS)

Voice support is opt-in. Enable inbound audio persistence (required for transcription):

```json
{
  "channels": {
    "whatsapp": {
      "media": {
        "persistIncomingAudio": true
      }
    }
  },
  "providers": {
    "openrouter": { "apiKey": "sk-or-v1-..." }
  }
}
```

To use ElevenLabs for voice replies, set a TTS profile + route and add `providers.elevenlabs`:

```json
{
  "models": {
    "profiles": {
      "tts_elevenlabs": {
        "kind": "tts",
        "provider": "elevenlabs_tts",
        "model": "eleven_multilingual_v2",
        "timeoutMs": 30000
      }
    },
    "routes": {
      "whatsapp.tts.speak": "tts_elevenlabs"
    }
  },
  "providers": {
    "elevenlabs": {
      "apiKey": "YOUR_ELEVENLABS_KEY",
      "apiBase": "https://api.elevenlabs.io/v1",
      "voiceId": "ztZBipzb4WQJRDayep3G",
      "modelId": "eleven_multilingual_v2"
    }
  }
}
```

`providers.elevenlabs.apiBase` is optional (default is `https://api.elevenlabs.io/v1`, API v1).

Then configure per-chat voice policy in `~/.nanobot/policy.json` (example for a group):

```json
{
  "channels": {
    "whatsapp": {
      "chats": {
        "1203634...@g.us": {
          "whenToReply": { "mode": "mention_only", "senders": [] },
          "voice": {
            "input": { "wakePhrases": ["nanobot", "nano"] },
            "output": { "mode": "in_kind", "voice": "JBFqnCBsd6RMkjVDRZzb", "format": "opus", "maxSentences": 2, "maxChars": 150 }
          }
        }
      }
    }
  }
}
```

Notes:
- Voice replies are sent as **quoted voice notes** (no extra text), so groups can see what message the bot responded to.
- For ElevenLabs, set `voice.output.voice` to a **voice ID** (from ElevenLabs voices API).
- If TTS fails or the synthesized audio is too large for the bridge payload limit, the bot falls back to text.
- Set `channels.whatsapp.acceptFromMe=true` only when you want Nanobot to process messages sent by the same WhatsApp account that runs the bridge.

**3. Run** (two terminals)

```bash
# Terminal 1
nanobot channels login

# Terminal 2
nanobot gateway
```

After linking once, you can run the bridge in background and restart it without `Ctrl-C`:

```bash
nanobot channels bridge start
nanobot channels bridge status
nanobot channels bridge restart
nanobot channels bridge stop
```

</details>

<details>
<summary><b>Feishu (飞书)</b></summary>

Uses **WebSocket** long connection — no public IP required.

```bash
pip install nanobot-stack[feishu]
```

**1. Create a Feishu bot**
- Visit [Feishu Open Platform](https://open.feishu.cn/app)
- Create a new app → Enable **Bot** capability
- **Permissions**: Add `im:message` (send messages)
- **Events**: Add `im.message.receive_v1` (receive messages)
  - Select **Long Connection** mode (requires running nanobot first to establish connection)
- Get **App ID** and **App Secret** from "Credentials & Basic Info"
- Publish the app

**2. Configure**

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": ""
    }
  }
}
```

> `encryptKey` and `verificationToken` are optional for Long Connection mode.
> Access/reply/tool/persona rules now live in `~/.nanobot/policy.json`.

**3. Run**

```bash
nanobot gateway
```

> [!TIP]
> Feishu uses WebSocket to receive messages — no webhook or public IP needed!

</details>

## ⚙️ Configuration

Config files:
- Runtime/API config: `~/.nanobot/config.json`
- Access/reply/tool/persona policy: `~/.nanobot/policy.json`

### Agent Runtime Options (`config.json`)

`agents.defaults.timingLogsEnabled` controls optional per-message timing summaries.

```json
{
  "agents": {
    "defaults": {
      "timingLogsEnabled": false
    }
  }
}
```

Default is `false` (no timing summary logs).

### Chat Policy (`policy.json`)

`policy.json` controls four things per Telegram/WhatsApp DM or group:
1. Who can talk
2. When the bot replies
3. Which tools are allowed
4. Which persona file is used

Merge precedence is:
`defaults -> channels.<channel>.default -> channels.<channel>.chats.<chat_id>`

All supported options are documented in `.docs/POLICY.md`.

```json
{
  "version": 2,
  "runtime": {
    "reloadOnChange": true,
    "reloadCheckIntervalSeconds": 1.0
  },
  "owners": {
    "telegram": ["453897507"],
    "whatsapp": ["491757070305"]
  },
  "defaults": {
    "whoCanTalk": { "mode": "everyone", "senders": [] },
    "whenToReply": { "mode": "all", "senders": [] },
    "allowedTools": { "mode": "all", "tools": [], "deny": ["exec", "spawn"] },
    "personaFile": null
  },
  "channels": {
    "telegram": {
      "default": {
        "whenToReply": { "mode": "mention_only", "senders": [] }
      },
      "chats": {
        "-1001234567890": {
          "whoCanTalk": { "mode": "owner_only", "senders": [] },
          "allowedTools": { "mode": "allowlist", "tools": ["read_file", "web_search", "web_fetch"], "deny": ["exec", "spawn"] },
          "personaFile": "personas/serious.md"
        }
      }
    },
    "whatsapp": {
      "default": {
        "whenToReply": { "mode": "mention_only", "senders": [] }
      },
      "chats": {}
    }
  }
}
```

> `channels.*.allowFrom` has been removed from `config.json`.
> Use `policy.json` and run `nanobot policy migrate-allowfrom` if you still have legacy entries.
> `policy.json` is hot-reloaded by default (no gateway restart needed after edits).

Quick reference for modes:
- `whoCanTalk.mode`: `everyone` | `allowlist` | `owner_only`
- `whenToReply.mode`: `all` | `off` | `mention_only` | `allowed_senders` | `owner_only`
- `allowedTools.mode`: `all` | `allowlist`

Tip: use `nanobot policy explain` to debug "why didn’t the bot reply?" for a specific chat/sender.

#### WhatsApp Owner DM Policy Commands

Deterministic policy commands in chat are **slash-only** and currently use the `/policy` namespace.

- Owner DM example: `/policy help`
- Owner DM example: `/policy allow-group 120363400000000000@g.us`
- Non-slash text like `policy allow-group ...` is treated as normal LLM input (not deterministic command routing).
- Non-owner `/policy ...` is ignored silently (no command-surface disclosure).

### Providers

> [!NOTE]
> Groq provides free voice transcription via Whisper. If configured, Telegram voice messages will be automatically transcribed.

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `openrouter` | LLM (recommended, access to all models) | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM (Claude direct) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT direct) | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM (DeepSeek direct) | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + **Voice transcription** (Whisper) | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM (Gemini direct) | [aistudio.google.com](https://aistudio.google.com) |
| `aihubmix` | LLM (API gateway, access to all models) | [aihubmix.com](https://aihubmix.com) |
| `dashscope` | LLM (Qwen) | [dashscope.console.aliyun.com](https://dashscope.console.aliyun.com) |
| `moonshot` | LLM (Moonshot/Kimi) | [platform.moonshot.cn](https://platform.moonshot.cn) |
| `zhipu` | LLM (Zhipu GLM) | [open.bigmodel.cn](https://open.bigmodel.cn) |
| `vllm` | LLM (local, any OpenAI-compatible server) | — |

<details>
<summary><b>Adding a New Provider (Developer Guide)</b></summary>

nanobot uses a **Provider Registry** (`nanobot/providers/registry.py`) as the single source of truth.
Adding a new provider only takes **2 steps** — no if-elif chains to touch.

**Step 1.** Add a `ProviderSpec` entry to `PROVIDERS` in `nanobot/providers/registry.py`:

```python
ProviderSpec(
    name="myprovider",                   # config field name
    keywords=("myprovider", "mymodel"),  # model-name keywords for auto-matching
    env_key="MYPROVIDER_API_KEY",        # env var for LiteLLM
    display_name="My Provider",          # shown in `nanobot status`
    litellm_prefix="myprovider",         # auto-prefix: model → myprovider/model
    skip_prefixes=("myprovider/",),      # don't double-prefix
)
```

**Step 2.** Add a field to `ProvidersConfig` in `nanobot/config/schema.py`:

```python
class ProvidersConfig(BaseModel):
    ...
    myprovider: ProviderConfig = ProviderConfig()
```

That's it! Environment variables, model prefixing, config matching, and `nanobot status` display will all work automatically.

**Common `ProviderSpec` options:**

| Field | Description | Example |
|-------|-------------|---------|
| `litellm_prefix` | Auto-prefix model names for LiteLLM | `"dashscope"` → `dashscope/qwen-max` |
| `skip_prefixes` | Don't prefix if model already starts with these | `("dashscope/", "openrouter/")` |
| `env_extras` | Additional env vars to set | `(("ZHIPUAI_API_KEY", "{api_key}"),)` |
| `model_overrides` | Per-model parameter overrides | `(("kimi-k2.5", {"temperature": 1.0}),)` |
| `is_gateway` | Can route any model (like OpenRouter) | `True` |
| `detect_by_key_prefix` | Detect gateway by API key prefix | `"sk-or-"` |
| `detect_by_base_keyword` | Detect gateway by API base URL | `"openrouter"` |
| `strip_model_prefix` | Strip existing prefix before re-prefixing | `True` (for AiHubMix) |

</details>


### Security

> [!TIP]
> For production deployments, set `"restrictToWorkspace": true` in your config to sandbox the agent.

| Option | Default | Description |
|--------|---------|-------------|
| `tools.restrictToWorkspace` | `false` | When `true`, restricts **all** agent tools (shell, file read/write/edit, list) to the workspace directory. Prevents path traversal and out-of-scope access. |
| `tools.exec.isolation.enabled` | `false` | Enable Linux-only bubblewrap isolation for `exec` with per-session batch sandboxes. |
| `tools.exec.isolation.batchSessionIdleSeconds` | `600` | Recycle a session sandbox after inactivity timeout. |
| `tools.exec.isolation.maxContainers` | `5` | Global cap for active session sandboxes. |
| `tools.exec.isolation.pressurePolicy` | `"preempt_oldest_active"` | Capacity policy when all sandboxes are busy. |
| `~/.nanobot/policy.json` | auto-created | Per-channel and per-chat access, reply rules, tool ACL, and persona selection. |

#### Scoped File Access Grants (Owner Sessions)

Use `policy.json` `fileAccess` to permit explicit non-workspace paths while keeping workspace-first defaults.

```json
{
  "fileAccess": {
    "ownerOnly": true,
    "audit": true,
    "grants": [
      {
        "id": "docs-read",
        "path": "/home/dm/Documents",
        "recursive": true,
        "mode": "read",
        "description": "Read-only documents access"
      },
      {
        "id": "nanobot-source",
        "path": "/home/dm/Documents/nanobot",
        "recursive": true,
        "mode": "read-write",
        "description": "Edit local source tree"
      }
    ],
    "blockedPaths": [
      "/home/dm/.ssh",
      "/home/dm/.aws"
    ],
    "blockedPatterns": [".env", "id_rsa", "*.pem"]
  }
}
```

Rules:
- Workspace remains always allowed.
- `blockedPaths` and `blockedPatterns` always override grants.
- Grants are only enabled for owner sessions when `ownerOnly=true`.
- Exec isolation mounts grant paths under `/grants/<grant-id>` when enabled.

#### Linux Exec Isolation (bubblewrap)

- Scope: `exec` tool only (file tools remain host-side but are forced to workspace scope when isolation is enabled).
- Platform: Linux only (recommended target: Raspberry Pi/Linux servers).
- Lifecycle: one sandbox per session; sandbox expires after idle timeout.
- Capacity: max 5 by default; when full and all busy, oldest active sandbox is preempted.
- Warm pool: not enabled in v1 (keeps implementation lean).

Add a host allowlist file at `~/.config/nanobot/mount-allowlist.json`:

```json
{
  "allowedRoots": ["~/.nanobot/workspace"],
  "blockedHostPatterns": [".ssh", ".aws", ".env", "id_rsa", "id_ed25519"]
}
```

If isolation is enabled and `bubblewrap`/allowlist checks fail, execution is fail-closed by default.


## CLI Reference

| Command | Description |
|---------|-------------|
| `nanobot onboard` | Initialize config & workspace |
| `nanobot agent -m "..."` | Chat with the agent |
| `nanobot agent` | Interactive chat mode |
| `nanobot gateway` | Start the gateway |
| `nanobot gateway restart` | Restart gateway in background |
| `nanobot logs` | Open gateway/bridge logs in lnav |
| `nanobot status` | Show status |
| `nanobot channels login` | Link WhatsApp (scan QR) |
| `nanobot channels bridge restart` | Restart WhatsApp bridge daemon |
| `nanobot channels status` | Show channel status |
| `nanobot policy path` | Print the active `policy.json` location |
| `nanobot policy explain` | Show merged policy + decision for a specific channel/chat/sender |
| `nanobot policy cmd "/policy ..."` | Execute shared deterministic policy command backend from CLI |
| `nanobot policy migrate-allowfrom` | Migrate legacy `channels.*.allowFrom` into `policy.json` |
| `nanobot policy annotate-whatsapp-comments` | Auto-fill WhatsApp `*@g.us` chat IDs with human-readable `comment` names |
| `nanobot memory status` | Show long-term memory backend and counters |
| `nanobot memory search --query ...` | Search long-term memory |
| `nanobot memory add --text ... --kind ...` | Insert one manual memory entry |
| `nanobot memory prune --dry-run` | Preview or run memory cleanup |
| `nanobot memory backfill` | Import legacy `memory/*.md` files into DB |
| `nanobot memory reindex` | Rebuild memory FTS index |

### Memory Operator Playbook

Full architecture and tuning guide:
- `.docs/MEMORY_SYSTEM.md`

```bash
# Inspect backend/counters
nanobot memory status

# Verify recall for a chat/user scope
nanobot memory search --query "preferred coding style" --channel cli --chat-id direct --scope all

# Add operator-curated memory
nanobot memory add --text "Use concise answers by default" --kind preference --scope user --channel cli --chat-id direct

# Preview retention cleanup
nanobot memory prune --older-than-days 365 --dry-run

# Run cleanup / rebuild index if needed
nanobot memory prune --older-than-days 365
nanobot memory reindex
```

### Policy Command Examples

```bash
# Show where policy file lives
nanobot policy path

# Explain why a Telegram group message would be ignored/replied
nanobot policy explain \
  --channel telegram \
  --chat -1001234567890 \
  --sender "453897507|DietmarDude" \
  --group \
  --mentioned

# Migrate old allowFrom entries (preview only)
nanobot policy migrate-allowfrom --dry-run

# Apply migration (creates policy backup automatically)
nanobot policy migrate-allowfrom

# Auto-fill WhatsApp group names into policy chat comments (creates policy backup automatically)
nanobot policy annotate-whatsapp-comments
```

<details>
<summary><b>Scheduled Tasks (Cron)</b></summary>

```bash
# Add a job
nanobot cron add --name "daily" --message "Good morning!" --cron "0 9 * * *"
nanobot cron add --name "hourly" --message "Check status" --every 3600

# List jobs
nanobot cron list

# Remove a job
nanobot cron remove <job_id>
```

</details>

## 🐳 Docker

> [!TIP]
> The `-v ~/.nanobot:/root/.nanobot` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.

Build and run nanobot in a container:

```bash
# Build the image
docker build -t nanobot .

# Initialize config (first time only)
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot onboard

# Edit config on host to add API keys
vim ~/.nanobot/config.json

# Run gateway (connects to Telegram/WhatsApp)
docker run -v ~/.nanobot:/root/.nanobot -p 18790:18790 nanobot gateway

# Or run a single command
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot agent -m "Hello!"
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot status
```

## 📁 Project Structure

```
nanobot/
├── agent/          # 🧠 Core agent logic
│   ├── loop.py     #    Agent loop (LLM ↔ tool execution)
│   ├── context.py  #    Prompt builder
│   ├── memory.py   #    Persistent memory
│   ├── skills.py   #    Skills loader
│   ├── subagent.py #    Background task execution
│   └── tools/      #    Built-in tools (incl. spawn)
├── skills/         # 🎯 Bundled skills (github, weather, tmux...)
├── channels/       # 📱 WhatsApp integration
├── bus/            # 🚌 Message routing
├── cron/           # ⏰ Scheduled tasks
├── heartbeat/      # 💓 Proactive wake-up
├── providers/      # 🤖 LLM providers (OpenRouter, etc.)
├── session/        # 💬 Conversation sessions
├── config/         # ⚙️ Configuration
└── cli/            # 🖥️ Commands
```

## 🤝 Contribute & Roadmap

PRs welcome! The codebase is intentionally small and readable. 🤗

**Roadmap** — Pick an item and open a PR in this repository.

- [x] **Voice Transcription** — Support for Groq Whisper (Issue #13)
- [ ] **Multi-modal** — See and hear (images, voice, video)
- [ ] **Long-term memory** — Never forget important context
- [ ] **Better reasoning** — Multi-step planning and reflection
- [ ] **More integrations** — Discord, Slack, email, calendar
- [ ] **Self-improvement** — Learn from feedback and mistakes

For upstream relationship and sync strategy, see `UPSTREAM.md`.

<p align="center">
  <em>Thanks for visiting nanobot-stack.</em>
</p>


<p align="center">
  <sub>nanobot-stack is for educational, research, and technical exchange purposes only.</sub>
</p>
