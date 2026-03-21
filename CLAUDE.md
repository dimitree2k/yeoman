# yeoman — Source Code

Lightweight, policy-first personal AI assistant runtime (~18k core lines).
Version: **0.6.0**


## Quick Reference

| What | Where |
|------|-------|
| Entry point / CLI | `packages/gateway/pyproject.toml` → `yeoman_gateway.__main__:main` |
| Orchestrator pipeline | `yeoman_gateway/core/orchestrator.py` |
| Policy engine | `yeoman_gateway/policy/engine.py` |
| Memory service | `yeoman_gateway/memory/service.py` |
| Channel adapters | `yeoman_gateway/channels/{telegram,discord,whatsapp,feishu}.py` |
| Tool registry | `yeoman_gateway/agent/tools/registry.py` |
| Provider registry | `yeoman_gateway/providers/registry.py` (single source of truth) |
| Config schema (Pydantic) | `yeoman_shared/config/schema.py` |
| Port interfaces (DI) | `yeoman_gateway/core/ports.py` |
| WhatsApp bridge (TS) | `packages/bridge/src/` |
| Tests | `tests/shared/`, `tests/gateway/`, `tests/overseer/` |
| Architecture docs | `docs/` |
| Runtime data dir | `~/.yeoman/` (config, policy, memory, logs — see `~/.yeoman/CLAUDE.md`) |


## Two Repositories

This project spans two directories that must be kept in sync:

| Location | Purpose | Git repo |
|----------|---------|---------|
| `~/Documents/yeoman/` | Source code (this repo) | public/private source repo |
| `~/.yeoman/` | Runtime state: config, policy, memory, logs, workspace | separate private runtime repo |

**When working on a task**, consider which directory is relevant:
- Code changes → `~/Documents/yeoman/`, then restart the gateway
- Config/policy/persona/skill changes → `~/.yeoman/`
- Debugging a live issue → check `~/.yeoman/var/logs/` and `~/.yeoman/data/`

The runtime CLAUDE.md (`~/.yeoman/CLAUDE.md`) documents the full layout, git tracking rules, secrets management, and config file schemas for the runtime directory.


## Monorepo Structure

This is a **uv workspace monorepo**. The root `pyproject.toml` has no `[project]` section — it is a virtual workspace root that defines workspace members and shared tool config (ruff, mypy, pytest).

```
~/Documents/yeoman/
├── pyproject.toml              # Virtual workspace root (no [project])
├── uv.lock                     # Unified lockfile for all packages
├── packages/
│   ├── shared/                 # yeoman-shared (import as yeoman_shared)
│   ├── gateway/                # yeoman-gateway (import as yeoman_gateway)
│   ├── overseer/               # yeoman-overseer (import as yeoman_overseer)
│   └── bridge/                 # TypeScript WhatsApp bridge (not a uv member)
├── tests/
│   ├── shared/                 # Tests for yeoman-shared
│   ├── gateway/                # Tests for yeoman-gateway
│   └── overseer/               # Tests for yeoman-overseer
└── docs/
```

### Workspace Members

| Package dir | Package name | Import root | Purpose |
|-------------|-------------|-------------|---------|
| `packages/shared/` | `yeoman-shared` | `yeoman_shared` | Config, telemetry, utils — no runtime deps |
| `packages/gateway/` | `yeoman-gateway` | `yeoman_gateway` | All gateway modules (agent, pipeline, channels, etc.) |
| `packages/overseer/` | `yeoman-overseer` | `yeoman_overseer` | Autonomous orchestration layer (skeleton) |
| `packages/bridge/` | *(TypeScript)* | N/A | WhatsApp Baileys bridge — not a uv workspace member |

### Import Conventions

```python
# Shared code (config, telemetry, utils)
from yeoman_shared.config import ...
from yeoman_shared.telemetry import ...
from yeoman_shared.utils import ...

# Gateway code (agent, pipeline, channels, etc.)
from yeoman_gateway.agent import ...
from yeoman_gateway.pipeline import ...
from yeoman_gateway.core import ...

# Overseer code
from yeoman_overseer import ...
```

**Import boundary**: gateway and overseer may import from shared. They **never** import from each other.

### Development Commands

```bash
cd ~/Documents/yeoman && uv sync    # Install all workspace packages (after pyproject.toml / uv.lock changes)
python -m pytest tests/             # Run all tests
python -m pytest tests/shared/     # Run shared tests only
python -m pytest tests/gateway/    # Run gateway tests only
python -m pytest tests/overseer/   # Run overseer tests only
ruff check .                        # Lint all packages
ruff format .                       # Format all packages
mypy packages/gateway/yeoman_gateway/core packages/gateway/yeoman_gateway/adapters  # Type check strict modules
```

After any gateway source code change, restart the gateway to pick it up:
```bash
yeoman gateway restart
```


## Architecture

```
Channel → Bus (inbound) → 13-stage Middleware Pipeline → OrchestratorIntent[]
  01 Normalize → 02 Dedup → 03 Archive → 04 Context → 05 Admin
  → 06 Policy → 07 Idea Capture → 08 Access Control → 09 New Chat
  → 10 No-Reply → 11 Security → 12 LLM Response → 13 Outbound
Intent dispatch → Bus (outbound/reaction) → Channel → User
```

**Hexagonal / Ports & Adapters**: `yeoman_gateway/core/ports.py` defines `PolicyPort`, `ResponderPort`,
`ReplyArchivePort`, `SecurityPort`. Adapters in `yeoman_gateway/adapters/` implement them.
The pipeline emits `OrchestratorIntent` objects; channels react asynchronously.
Media (ASR/TTS/vision) is cross-cutting: channels enrich inbound, responder synthesizes outbound.


## Module Map

| Module | Package | Responsibility |
|--------|---------|---------------|
| `agent/` | gateway | Core loop, prompt context builder, skills loader, tools |
| `core/` | gateway | Orchestrator pipeline, admin commands, intents, models, ports |
| `adapters/` | gateway | Port implementations (policy, LLM, archive, telemetry) |
| `channels/` | gateway | Platform integrations + channel lifecycle manager |
| `bus/` | gateway | Async message queue with deduplication |
| `providers/` | gateway | LLM registry, LiteLLM wrapper, OpenAI-compat, transcription |
| `policy/` | gateway | Engine, schema, loader, identity normalization, personas, admin handlers |
| `memory/` | gateway | Service, SQLite store, embeddings, extractor, session state (WAL) |
| `media/` | gateway | ASR (Groq Whisper), TTS (ElevenLabs), vision, routing, storage |
| `security/` | gateway | Rule engine, built-in rules, noop (dev) |
| `skills/` | gateway | Bundled skills (github, weather, summarize, tmux, cron, etc.) |
| `cron/` | gateway | Scheduled task service |
| `heartbeat/` | gateway | Proactive wake-up timer |
| `cli/` | gateway | typer commands |
| `config/` | shared | Pydantic schema, loader, defaults |
| `telemetry/` | shared | Structured logging, tracing |
| `utils/` | shared | Shared utilities |


## Conventions

- **Package manager**: `uv` workspace — `uv sync` installs all packages in editable mode
- **Linter/Formatter**: Ruff (line-length 100, Python 3.14 target) — configured in root `pyproject.toml`
- **Type checker**: MyPy strict on `yeoman_gateway/core/`, `yeoman_gateway/adapters/` — configured in root `pyproject.toml`
- **Logging**: Loguru (structured, thread-safe)
- **Async**: asyncio throughout; mutexes for shared state
- **Tests**: pytest + `@pytest.mark.asyncio`; organized under `tests/shared/`, `tests/gateway/`, `tests/overseer/`
- **Naming**: `*_adapter.py` = port impl, `*_service.py` = long-running, `*_engine.py` = business logic
- **Type hints**: Always; enforced by MyPy on strict modules


## Config Hierarchy (runtime)

1. Hard-coded defaults in source
2. `~/.yeoman/config.json` — providers, models, channels
3. `~/.yeoman/policy.json` — per-channel/per-chat overrides (hot-reloaded)
4. Environment variables (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, etc.)


## Adding a New LLM Provider

Only 2 changes needed:
1. Add entry in `yeoman_gateway/providers/registry.py`
2. Add config field in `yeoman_shared/config/schema.py`


## Skills System

Skills are directories containing `SKILL.md` (YAML front-matter + markdown body).
Loaded dynamically by `yeoman_gateway/agent/skills.py`. Compatible with OpenClaw format.
Bundled skills in `yeoman_gateway/skills/`; user skills in `~/.yeoman/workspace/skills/`.


## WhatsApp Bridge

TypeScript (Baileys 7.0.0-rc.9) in `packages/bridge/src/`. Compiled to `packages/bridge/dist/`.
The gateway package bundles the bridge dist via hatchling's `force-include` at build time
(`packages/bridge/` → `yeoman_gateway/bridge/`).
Communicates with Python gateway via WebSocket (`ws://localhost:3001`).
Auth state persisted in `~/.yeoman/whatsapp-auth/`.


## Security Notes

- Tool isolation via Linux bubblewrap sandbox (`yeoman_gateway/agent/tools/exec_isolation.py`)
- Policy engine is deterministic — no ad-hoc ACLs in code
- All access control in `~/.yeoman/policy.json`
- Input/output validation in `yeoman_gateway/security/engine.py`


## Conversation Context (WhatsApp)

Two parallel context sources are injected into every prompt for WhatsApp messages:

| Source | Trigger | Limit (config key) |
|--------|---------|-------------------|
| **Thread window** | explicit `reply_to_message_id` only | `reply_context_window_limit` (default 6) |
| **Ambient window** | every message, always | `ambient_window_limit` (default 8) |

**Ambient window** = last N messages before the current one, fetched from the inbound archive
(`SqliteReplyArchiveAdapter` → `lookup_messages_before(current_message_id)`).
Injected into `event.raw_metadata["ambient_context_window"]` by
`orchestrator._build_ambient_window()` → `_resolve_reply_context()`.

Rendered in `context._with_reply_context()`:
- With reply → `[Reply Context]` block gains a `recent_messages:` sub-section
- No reply, but ambient present → `[Recent Messages]` block (ambient only)

Also fed into `memory.build_retrieved_context(query=…)` in `responder_llm.py` to enrich
the semantic/FTS recall signal for vague one-liner messages.

See `docs/ambient-context-window.md` for the full design (local only, gitignored).


## Deployment Rules

**Source of truth**: `~/Documents/yeoman/` — ALL code changes happen here.
Python changes are live immediately (editable install). Bridge or dependency
changes require `yeoman deploy`.

**NEVER modify files in these derived locations:**
- `~/.local/share/uv/tools/yeoman-gateway/` — managed by `uv tool install`
- `~/.local/share/uv/tools/yeoman/` — stale legacy env, do not use
- `~/.yeoman/var/cache/bridge/` — managed by `ensure_runtime()`

**NEVER manually copy files** between source, installed package, or bridge cache.
After bridge or dependency changes, run `yeoman deploy` from this directory
(or `bin/deploy` for first install / recovery).
