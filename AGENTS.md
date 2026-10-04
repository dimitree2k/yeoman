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
- Follow the validation scope in the approved plan and `~/.codex/AGENTS.md`; do not run the full suite or repository-wide Ruff automatically before every push.

## Two-Repository Model

This source checkout has a private runtime companion at `/home/dm/.yeoman`.

- `/home/dm/Documents/yeoman/` owns source code, tests, packaging, and deployment.
- `/home/dm/.yeoman/` owns runtime config and policy, personas and skills, memory, logs, live state, and private work notes.
- Deploy source changes with `yeoman deploy` from this checkout. Never manually copy code or edit installed/generated artifacts.
- Put new private specs, design notes, implementation plans, and dated handoffs in `/home/dm/.yeoman/docs/superpowers/{specs,plans}/` or `/home/dm/.yeoman/session-context/`.
- Keep this repository's `docs/` for documentation intentionally safe and useful to keep with the source checkout. Read `/home/dm/.yeoman/AGENTS.md` for runtime-specific guidance.

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
| Runtime-only layout and private operational notes | `~/.yeoman/CLAUDE.md` and `~/.yeoman/AGENTS.md` |
| Dated session findings and handoff notes | `~/.yeoman/session-context/YYYY-MM-DD-short-description.md` |
| Feature designs, tradeoffs, implementation plans | `~/.yeoman/docs/superpowers/specs/` and `~/.yeoman/docs/superpowers/plans/` |
| Shipped user-facing behavior changes | `CHANGELOG.md` |
| One-off temporary notes | Avoid if possible; convert to one of the above before ending work |

New files under `docs/` may be ignored by `.gitignore`; verify with
`git check-ignore -v <path>` and `git ls-files <path>` before assuming a note
will be tracked.

## Superpowers / Planning Artifacts

This project contains Claude Code Superpowers-style specs and plans in
`docs/superpowers/`. Codex may not have the Superpowers plugin installed, but
the documents are still useful project context. New private specs and plans go
in `/home/dm/.yeoman/docs/superpowers/` so they stay out of the source checkout.

Before large feature work or architectural refactors:

- Check `/home/dm/.yeoman/docs/superpowers/specs/` and `docs/superpowers/specs/` for relevant private or historical designs.
- Check `/home/dm/.yeoman/docs/superpowers/plans/` and `docs/superpowers/plans/` for implementation steps and completed intent.
- Treat those docs as historical/architectural context, not guaranteed current code.
- Reconcile against source and tests before editing.

## Bounded Delivery Protocol

Follow the approved plan; generic continuous-execution or five-round workflows
do not apply automatically.

- Plans identify prerequisites, independent lanes, file/API ownership, and
  integration. Parallelize only ready, disjoint lanes; serialize shared files,
  shared `~/.yeoman` tracker/plan edits, rebases, and integration.

- Before recovery/refactoring, record branch, HEAD and worktree status. Preserve
  uncommitted work; never reset, clean or discard it without approval.
- Use at most one implementer and one reviewer per task. Allow one fix round;
  a second needs the owner's decision. Only in-scope P0/P1 findings block
  completion; log P2/P3 and do not broaden the task.
- Work budgets are 60 minutes per task and 4 hours per phase, not process
  timeouts. Follow the approved plan and `~/.codex/AGENTS.md` for check scope,
  preflight, monitoring and retries.
- Run Ruff on changed files and Mypy on affected strict targets during work;
  broader checks belong only to a plan's final gate.
- Keep handoffs to ten lines plus a report path. Do not copy full logs or agent
  reports into the parent context.
- Verify the dispatched child uses the requested model and effort; stop if
  it does not.
- Do not merge, push, deploy or restart services unless the user authorized it.

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
Only after an authorized rollout, restart affected services for Python-only
changes or run `yeoman deploy` from ~/Documents/yeoman/ for bridge, dependency
or packaging changes. Never manually copy files between source and installed
locations.
