# Yeoman implementation program log

Status: Active implementation ledger for major-turn decisions and expert cross-review.

## Authority and operating rules

- Normative architecture: `docs/superpowers/specs/2026-08-16-yeoman-target-architecture-design.md`, SHA-256 `8201c5986e66292147e8682af506bf1978abe5338d56572b0395bf3d42affbfa`.
- Orchestration: `docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md`.
- Current milestone: `docs/superpowers/plans/2026-08-16-yeoman-rework-01-release-baseline.md`.
- Plan bundle reviewed GO: SHA-256 `e614e1b92e1fc7eb9aef33909edbca2e830554f2763347a8f137730614cc0067`.
- The live runtime at `/home/dm/.yeoman` remains authoritative and must not be mutated by build tasks.
- Implementation uses isolated worktrees, TDD, one implementer at a time, a task-scoped review after each task, and a broad milestone review before handoff.
- Important decisions, blockers, commit ranges, verification evidence commitments, and reviewer rulings are appended here. Conversation history is not authority.

## Standing major-turn reviewers

- Agent A, senior software engineer: task `01a009b3-e740-7972-992b-5d63d6066b8c`.
- Agent B, senior security architect: task `01a009b3-f495-7a90-8e50-8a22d4d306d2`.
- Every message to either standing reviewer must explicitly select `gpt-5.6-luna`.
- Superseded architecture-review tasks `01a00731-aa0d-7591-9382-2c52becddbae` and `01a00731-aa5c-7232-93c3-d165eabc5d58` are retired from implementation communication.

## What counts as a major turn

Request both standing reviews after each accepted milestone task or earlier when a task changes a cross-milestone contract, authority boundary, migration/recovery invariant, release signature, or cutover state. Each request names the exact commit range, task brief, implementation report, verification/review package, and this log. Both reviewers return commit-bound GO/NO-GO and any required adjustment before the next task.

## Implementation entries

### 2026-08-16 — Program start

- Owner established the implementation goal and required Luna for standing experts and implementation workers.
- Agent A and Agent B were created as same-directory tasks; all of their active turns will be sent with explicit Luna model override.
- Milestone 01 preflight begins. No target/runtime mutation has occurred in this implementation run yet.
- Tool constraint to track: the current in-process collaboration API advertises only `gpt-5.6-sol` and `gpt-5.6-terra` for spawned subagents. Before dispatching implementation work, the controller must either obtain a Luna-capable task mechanism or record the blocker rather than silently using a different model.

### 2026-08-16 — Milestone 01 preflight

- Controller read the complete orchestration plan, Milestone 01 plan, its required normative sections, and `AGENTS.md`; an SDD workspace, Task 1 brief, and recovery ledger now exist under the plan-scoped ignored workspace.
- Source fact: `/home/dm/Documents/yeoman` is a normal checkout on `c/turn-engine-v2` at `6f96c19a1fdf04ca777fe28d9ca831fbbd4482a8` with 50 staged source/test paths plus this untracked program log. Existing linked worktrees are unrelated and were left untouched.
- Preservation gate: every dirty path needs an explicit owner-approved disposition before the preservation commit/tag. No source path has been committed, excluded, reverted, or copied by this implementation run.
- Signature gate: repository Git config names no signing key, GnuPG reports zero local secret keys, and neither `age` nor `rage` is installed. The approved plan requires a pinned offline Ed25519 public-key fingerprint, an authorized signing operation, and two separately stored age recovery identities before the signed tag and encrypted bootstrap evidence.
- Worker-model gate: standing Agent A and Agent B acknowledged their read-only Luna review contracts. The collaboration subagent API still offers only Sol and Terra, so no implementation subagent has been dispatched under a substituted model.
- Runtime safety: `/home/dm/.yeoman`, its services, and all live state remain untouched.

### 2026-08-16 — Preservation-candidate audit while owner gates remain open

- Candidate index: 50 staged paths, 3,507 additions and 186 deletions before the privacy correction; current binary cached-diff SHA-256 `7d94039ee62bd91d6735cbfe4b293db41a6c7be4c6fdedc060273c21f5ddd56c` and cached raw-index SHA-256 `8e88c630cc63a3f1e053c9df9f53a167cf84d38b8ae35611723201989f8817c3`.
- Privacy correction: two newly added real channel/participant fixture identifiers in `tests/gateway/test_implicit_bot_address.py` were replaced with obvious synthetic identifiers. No behavior changed; the file's 25 tests pass. Existing historical identity fixtures remain a source-history privacy gap and must never enter the target release artifact or public handoff.
- Verification passed: `git diff --cached --check`; `uv run ruff check .`; 210 tests covering every modified Python test file; 868 tests under the supported `tests/gateway`, `tests/shared`, and `tests/overseer` collections; and all 23 Bridge tests including its TypeScript build.
- Repository-wide `uv run pytest -q` is not a valid green baseline: collection stops on 19 root-level stale tests importing the absent historical `yeoman` package. This is recorded as preservation debt; the target release plan already requires removal of old tests and a clean full collection rather than treating this failure as success.
- The AGENTS-prescribed mypy slice reports 13 errors in the existing large policy/responder adapters. One staged removal of a required boundary ignore was corrected; the remaining current errors are not represented as green and will not be used to justify baseline acceptance.
- Credential-like added literals are confined to explicit test fixtures such as redaction tests; no private-key marker was found. This heuristic scan is evidence, not a substitute for the signed exhaustive source inventory.
- No preservation commit/tag or implementation worktree has been created. Owner disposition, signing/recovery material, and Luna worker availability remain open gates.
