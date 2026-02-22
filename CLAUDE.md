# nanobot-stack — Source Code

Lightweight, policy-first personal AI assistant runtime (~13k core lines).
Independent fork of HKUDS/nanobot. MIT license.

## Quick Reference

| What | Where |
|------|-------|
| Entry point / CLI | `nanobot/cli/commands.py` (typer) |
| Orchestrator pipeline | `nanobot/core/orchestrator.py` |
| Policy engine | `nanobot/policy/engine.py` |
| Memory service | `nanobot/memory/service.py` |
| Channel adapters | `nanobot/channels/{telegram,discord,whatsapp,feishu}.py` |
| Tool registry | `nanobot/agent/tools/registry.py` |
| Provider registry | `nanobot/providers/registry.py` (single source of truth) |
| Config schema (Pydantic) | `nanobot/config/schema.py` |
| Port interfaces (DI) | `nanobot/core/ports.py` |
| WhatsApp bridge (TS) | `bridge/src/` |
| Tests | `tests/test_*.py` |
| Architecture docs | `docs/` |
| Runtime data dir | `~/.nanobot/` (separate — see its own CLAUDE.md) |

## Architecture

```
Channel → Manager → Bus/Queue → Orchestrator
  → Policy Engine (decision) → Security Engine (validation)
  → LLM Responder (LiteLLM) → Tool Execution → Memory Capture
  → Channel → User
```

**Hexagonal / Ports & Adapters**: `core/ports.py` defines `PolicyPort`, `ResponderPort`,
`ReplyArchivePort`, `SecurityPort`. Adapters in `adapters/` implement them.
Orchestrator emits `OrchestratorIntent` objects; channels react asynchronously.

## Module Map

| Module | Responsibility |
|--------|---------------|
| `agent/` | Core loop, prompt context builder, skills loader, tools |
| `core/` | Orchestrator pipeline, admin commands, intents, models, ports |
| `adapters/` | Port implementations (policy, LLM, archive, telemetry) |
| `channels/` | Platform integrations + channel lifecycle manager |
| `bus/` | Async message queue with deduplication |
| `config/` | Pydantic schema, loader, defaults |
| `providers/` | LLM registry, LiteLLM wrapper, OpenAI-compat, transcription |
| `policy/` | Engine, schema, loader, identity normalization, personas, admin handlers |
| `memory/` | Service, SQLite store, embeddings, extractor, session state (WAL) |
| `media/` | ASR (Groq Whisper), TTS (ElevenLabs), vision, routing, storage |
| `security/` | Rule engine, built-in rules, noop (dev) |
| `skills/` | Bundled skills (github, weather, summarize, tmux, cron, etc.) |
| `cron/` | Scheduled task service |
| `heartbeat/` | Proactive wake-up timer |
| `cli/` | typer commands |

## Conventions

- **Package manager**: `uv` (preferred), `pip`, or `poetry`
- **Linter/Formatter**: Ruff (line-length 100, Python 3.14 target)
- **Type checker**: MyPy strict on `core/`, `adapters/`
- **Logging**: Loguru (structured, thread-safe)
- **Async**: asyncio throughout; mutexes for shared state
- **Tests**: pytest + `@pytest.mark.asyncio`; files in `tests/test_*.py`
- **Naming**: `*_adapter.py` = port impl, `*_service.py` = long-running, `*_engine.py` = business logic
- **Type hints**: Always; enforced by MyPy on strict modules

## Config Hierarchy (runtime)

1. Hard-coded defaults in source
2. `~/.nanobot/config.json` — providers, models, channels
3. `~/.nanobot/policy.json` — per-channel/per-chat overrides (hot-reloaded)
4. Environment variables (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, etc.)

## Adding a New LLM Provider

Only 2 changes needed:
1. Add entry in `providers/registry.py`
2. Add config field in `config/schema.py`

## Key Commands

```bash
pytest tests/                        # Run all tests
pytest -xvs tests/test_policy_engine.py  # Single test, verbose
ruff check nanobot/                  # Lint
ruff format nanobot/                 # Format
mypy nanobot/core nanobot/adapters   # Type check strict modules
bash core_agent_lines.sh             # Count core lines
```

## Skills System

Skills are directories containing `SKILL.md` (YAML front-matter + markdown body).
Loaded dynamically by `agent/skills.py`. Compatible with OpenClaw format.
Bundled skills in `nanobot/skills/`; user skills in `~/.nanobot/workspace/skills/`.

## WhatsApp Bridge

TypeScript (Baileys 7.0.0-rc.9) in `bridge/src/`. Compiled to `bridge/dist/`.
Communicates with Python gateway via WebSocket (`ws://localhost:3001`).
Auth state persisted in `~/.nanobot/whatsapp-auth/`.

## Security Notes

- Tool isolation via Linux bubblewrap sandbox (`agent/tools/exec_isolation.py`)
- Policy engine is deterministic — no ad-hoc ACLs in code
- All access control in `~/.nanobot/policy.json`
- Input/output validation in `security/engine.py`

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
