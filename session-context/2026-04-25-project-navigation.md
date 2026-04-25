# Project Navigation Context

Date: 2026-04-25
Context: Captures orientation findings from a Codex session reviewing how well
future agents can navigate the Yeoman repo.

## Findings

- Yeoman is a `uv` workspace monorepo with `packages/shared`, `packages/gateway`,
  `packages/overseer`, and a separate TypeScript WhatsApp bridge in
  `packages/bridge`.
- The best source-code entrypoints are `packages/gateway/yeoman_gateway/app/bootstrap.py`
  for runtime wiring, `packages/gateway/yeoman_gateway/core/orchestrator.py` for
  middleware composition, and `packages/gateway/yeoman_gateway/core/pipeline.py`
  for pipeline execution semantics.
- Runtime state is intentionally separate from source. Source lives in
  `~/Documents/yeoman`; private runtime config, policy, personas, memory, logs,
  and local workspace files live in `~/.yeoman`.
- `docs/superpowers/` contains Claude Code Superpowers-style specs and plans.
  Codex may not have the plugin installed, but those files are still useful
  design history and planning context.
- If docs disagree with code, trust `app/bootstrap.py`, `core/orchestrator.py`,
  and tests first.
- New files under `docs/` may be ignored by `.gitignore`; verify with
  `git check-ignore -v <path>` and `git ls-files <path>` before assuming they
  are tracked.

## Follow-up

- Keep cross-agent rules and navigation in `AGENTS.md`.
- Keep detailed source architecture in `CLAUDE.md`.
- Use this `session-context/` directory for dated, searchable handoff notes
  that should survive context compaction but are not broad rules.
