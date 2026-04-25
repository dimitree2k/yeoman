# Agent Guidelines

This file is read by AI coding agents (Claude Code, Codex, Cursor, etc.) working on this repo.
Human contributors should follow these rules too.

## Commit Messages — Conventional Commits (mandatory)

All commits **must** follow the [Conventional Commits](https://www.conventionalcommits.org/) spec:

```
<type>(<scope>): <short summary>

[optional body]

[optional footer(s)]
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `docs` | Documentation only |
| `test` | Adding or fixing tests |
| `chore` | Build, deps, tooling, CI |
| `style` | Formatting / lint (no logic change) |

### Scope (optional but recommended)

Use the module name or subsystem: `orchestrator`, `policy`, `memory`, `tts`, `context`, `config`, `channels`, `bridge`, etc.

### Examples

```
feat(orchestrator): add ambient context window for group chats
fix(tts): convert pcm16 to ogg/opus before sending voice note
docs(memory): add ambient-context-window design doc
chore(deps): bump litellm to 1.52
```

### Breaking changes

Append `!` after the type/scope and add `BREAKING CHANGE:` in the footer:

```
feat(config)!: rename reply_context_window to reply_context_window_limit

BREAKING CHANGE: config key renamed; update ~/.yeoman/config.json manually.
```

## General Rules

- Never commit secrets, API keys, or personal data (`~/.yeoman/` runtime data is gitignored).
- Keep PRs focused — one logical change per commit where practical.
- Run `python -m pytest tests/` and `ruff check .` before pushing.

## Project Navigation

Yeoman is a `uv` workspace monorepo. The source checkout is the only place to
edit code.

| Area | Path | Notes |
|------|------|-------|
| Workspace root | `~/Documents/yeoman/` | Source of truth for all code changes |
| Gateway package | `packages/gateway/yeoman_gateway/` | Main runtime: channels, bus, pipeline, responder, tools, memory |
| Shared package | `packages/shared/yeoman_shared/` | Config schema, config loader, telemetry, shared utilities |
| Overseer package | `packages/overseer/yeoman_overseer/` | Autonomous runbook service and overseer agent tools |
| WhatsApp bridge | `packages/bridge/` | TypeScript Baileys bridge; build/deploy needed after bridge changes |
| Runtime state | `~/.yeoman/` | Private config, policy, personas, memory, logs, workspace |

Primary source entrypoints:

- CLI: `packages/gateway/yeoman_gateway/__main__.py` -> `yeoman_gateway.cli.commands:app`
- Gateway runtime wiring: `packages/gateway/yeoman_gateway/app/bootstrap.py`
- Inbound pipeline composition: `packages/gateway/yeoman_gateway/core/orchestrator.py`
- Pipeline runner/context: `packages/gateway/yeoman_gateway/core/pipeline.py`
- Typed intents/events: `packages/gateway/yeoman_gateway/core/intents.py`, `packages/gateway/yeoman_gateway/bus/events.py`
- Config schema: `packages/shared/yeoman_shared/config/schema.py`
- Provider registry: `packages/gateway/yeoman_gateway/providers/registry.py`
- Tool registry and tools: `packages/gateway/yeoman_gateway/agent/tools/`

Current gateway flow:

```text
Channel -> MessageBus inbound -> Orchestrator middleware -> OrchestratorIntent[]
Intent dispatch -> MessageBus outbound/reaction -> Channel
```

The runtime is composed in `build_gateway_runtime()`. If behavior differs from
docs, trust `app/bootstrap.py`, `core/orchestrator.py`, and tests first.

## Runtime Context

This repo deliberately separates source and private runtime state.

- Code changes go in `~/Documents/yeoman/`.
- Config, policy, personas, local skills, memory, and logs live under `~/.yeoman/`.
- For live debugging, inspect redacted-safe runtime context: `yeoman env`, `yeoman status`, `~/.yeoman/policy.json`, `~/.yeoman/config.json`, and recent logs in `~/.yeoman/var/logs/`.
- Do not commit runtime files or secrets. If a runtime fact matters for future agents, document the sanitized version here or in `CLAUDE.md`.

## Durable Agent Findings

Use these locations so findings survive session compaction or context loss:

| Finding type | Put it here |
|--------------|-------------|
| Cross-agent rules, navigation, safety constraints | `AGENTS.md` |
| Detailed source architecture and module map | `CLAUDE.md` |
| Runtime-only layout and private operational notes | `~/.yeoman/CLAUDE.md` (sanitize before copying elsewhere) |
| Dated session findings and handoff notes | `session-context/YYYY-MM-DD-short-description.md` |
| Feature designs, tradeoffs, implementation plans | `docs/superpowers/specs/` and `docs/superpowers/plans/` |
| Shipped user-facing behavior changes | `CHANGELOG.md` |
| One-off temporary notes | Avoid if possible; convert to one of the above before ending work |

New files under `docs/` may be ignored by `.gitignore`; verify with
`git check-ignore -v <path>` and `git ls-files <path>` before assuming a note
will be tracked.

## Superpowers / Planning Artifacts

This project contains Claude Code Superpowers-style specs and plans in
`docs/superpowers/`. Codex may not have the Superpowers plugin installed, but
the documents are still useful project context.

Before large feature work or architectural refactors:

- Check `docs/superpowers/specs/` for the relevant design.
- Check `docs/superpowers/plans/` for implementation steps and completed intent.
- Treat those docs as historical/architectural context, not guaranteed current code.
- Reconcile against source and tests before editing.

## Test And Validation Matrix

Prefer targeted checks while iterating, then broader checks before handoff:

```bash
python -m pytest tests/gateway/
python -m pytest tests/shared/
python -m pytest tests/overseer/
python -m pytest tests/
ruff check .
mypy packages/gateway/yeoman_gateway/core packages/gateway/yeoman_gateway/adapters
```

Bridge changes:

```bash
cd packages/bridge
npm run build
npm test
```

After Python-only gateway changes, restart affected services. After bridge,
dependency, or packaging changes, run `yeoman deploy` from `~/Documents/yeoman/`.

## Deployment Rules

### Source of Truth
All code changes go in ~/Documents/yeoman/. Never edit installed copies.
Python changes are live immediately (editable install). Bridge or dependency
changes require `yeoman deploy`.

### Forbidden Paths (read-only for agents)
- ~/.local/share/uv/tools/yeoman-gateway/  (managed by uv)
- ~/.local/share/uv/tools/yeoman/          (stale legacy env — do not use)
- ~/.yeoman/var/cache/bridge/               (managed by ensure_runtime)

### After Code Changes
For Python-only changes: restart affected services.
For bridge or dependency changes: run `yeoman deploy` from ~/Documents/yeoman/.
Do not manually copy files between source and installed locations.
