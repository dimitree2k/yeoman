# Persona Evolution Follow-Up Todo

Status: active
Created: 2026-05-05

This checklist tracks the remaining persona-evolution work after the May 4-5
implementation pass. It deliberately excludes the separate spontaneity
ignore/approve policy thread.

## Current State To Preserve

- [x] Keep the latest runtime persona-evolution proposal in `proposed` state for
  the ignore/expiry test.
- [x] Do not delete persona source files under `~/.yeoman/workspace/personas/`.
- [x] Keep generated proposal artifacts under private runtime state, not source.
- [x] Keep base persona files immutable; only `.evolution.md` can be changed by
  an approved persona-evolution apply step.

## Reconcile Landed Work

- [x] Review the current dirty worktree and separate persona-evolution changes
  from unrelated edits.
- [x] Re-run targeted validation:
  `uv run ruff check packages/gateway/yeoman_gateway/persona_evolution.py tests/gateway/test_persona_evolution.py`
- [x] Re-run targeted tests:
  `uv run python -m pytest tests/gateway/test_persona_evolution.py tests/gateway/test_workflow_types.py -q`
- [x] Update
  `docs/superpowers/plans/2026-04-27-persona-evolution-consciousness-learning.md`
  so completed Phase 2 and Phase 4 items match the actual code.
- [x] Commit the reconciled implementation with a Conventional Commit message.

## Phase 1 - Observability

- [x] Add one compact operator query for recent speakups, outcomes, and taste
  distillations by channel/chat.
- [x] Add taste-distillation log lines for write, skip, and failure cases.
- [x] Add an operational status/check command that reports sent speakups,
  labeled outcomes, taste-distillation count, and last learned taste per
  eligible chat.
- [x] Add tests proving the operator can answer "what did Yeoman learn in this
  chat?" without opening SQLite directly.

## Phase 2 - Cron And Distiller Cleanup

- [x] Decide whether the current structured-proposal renderer is enough or
  whether a dedicated `persona.evolution` model route is still needed.
- [x] If the route is still needed, add it with low temperature and structured
  JSON expectations in config/schema and route loading. Current decision: not
  needed yet.
- [x] Reconcile the `evo1a2md` job name so the live typed cron and the old private
  cron concept cannot be confused.
- [x] Add a small runtime/status check that shows last persona-evolution run,
  last result, and next scheduled run.

## Phase 3 - Durable Evolution Format

- [x] Define the allowed `.evolution.md` section contract and reject unknown
  durable sections.
- [x] Require evidence counts for every proposed durable lesson.
- [x] Require date and confidence for every new durable lesson.
- [x] Enforce small bounded consolidations per run.
- [x] Read the base persona before apply and reject changes that conflict with
  base persona invariants.
- [x] Add tests for invalid section names, missing evidence, and invariant
  conflicts. Implemented as invalid section rejection, proposed-note evidence
  validation, and base-persona hash rejection before apply.

## Phase 4 - Owner Review And Apply Hardening

- [x] Make persona-evolution preview mode explicit and independent from
  spontaneity preview settings.
- [x] Confirm proposal files are written only under private runtime workspace
  paths.
- [x] Confirm owner approval via Telegram reply and CLI use the same apply
  service path.
- [x] Ensure rejected and expired proposals cannot mutate evolution files.
- [x] Ensure apply fails if the base persona or evolution file changed since the
  proposal hash was recorded.
- [x] Add or refresh tests for approve, deny, expired, and changed-file cases.

## Phase 5 - Scheduled Autonomy Guardrails

- [x] Add config for persona-evolution scheduling:
  enabled flag, cron expression, minimum samples, mode, and persona allowlist.
- [x] Start scheduled runs in preview-only mode.
- [x] Add metrics for proposed, applied, rejected, expired, and no-op runs.
- [x] Add status output for next evolution run and last result.
- [x] Defer any auto-apply mode until repeated reviewed runs are clean.

## Final Validation Before Calling This Done

- [x] `uv run python -m pytest tests/gateway/test_persona_evolution.py tests/gateway/test_workflow_types.py -q`
- [x] `uv run ruff check packages/gateway/yeoman_gateway/persona_evolution.py tests/gateway/test_persona_evolution.py`
- [x] If config/schema or cron wiring changes: run the relevant shared/gateway
  config and cron tests.
- [x] Restart the gateway after Python runtime changes.
- [x] Verify the live pending proposal state and next scheduled evolution run.
