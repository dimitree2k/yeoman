# Yeoman Source Preservation and Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the currently working Yeoman source state, make it the safe `main` baseline, and remove only verified dead source and stale Git artifacts without changing the live runtime contract.

**Architecture:** The source checkout remains the code source of truth, `~/.yeoman` remains private runtime state, and the existing editable `uv` tool environment remains unchanged. Current uncommitted A2A, owner-turn, and observability work is captured before `main` is advanced. Cleanup happens in an isolated `c/source-cleanup` worktree and reaches the live runtime only after verification.

**Tech Stack:** Git branches/worktrees, Python 3.14, `uv`, pytest, Ruff, systemd user services.

**Spec:** User-approved cleanup strategy from the 2026-09-07 task conversation; source/runtime rules in `AGENTS.md` and `CLAUDE.md`.

## Global Constraints

- Preserve all current tracked and untracked A2A, owner-turn, observability, and test work before cleanup.
- Do not switch the live runtime to a tree that cannot parse the existing \`replyBudget\` policy.
- Do not delete runtime databases, WhatsApp credentials, memory backups, browser state, or bridge state in this pass.
- Do not edit files under \`~/.local/share/uv/tools/yeoman-gateway/\`; keep the existing editable deployment model.
- Delete source only when production references and the replacement test surface are verified.
- Keep unmerged experimental branches as archival refs unless their contents are explicitly reconciled into \`main\`.

---

### Task 1: Recheck and capture the current source state

**Files:**
- Inspect: \`/home/dm/Documents/yeoman/.git/\`
- Inspect: \`/home/dm/.yeoman/config.json\`, \`/home/dm/.yeoman/policy.json\`

- [ ] Confirm branch, dirty paths, worktrees, active services, and launcher.
- [ ] Confirm the current branch contains \`ReplyBudget\` while current runtime policy uses \`replyBudget\`; confirm \`main\` is not yet a safe live target.
- [ ] Record the exact inventory before any mutation.

### Task 2: Preserve the current working tree

**Files:**
- Create: Git branch \`c/turn-engine-v2-preserve-20260907\`
- Commit: all reviewed tracked and untracked source/docs WIP

- [ ] Create the preservation branch without discarding the dirty tree.
- [ ] Stage and inspect all current changes; ensure the A2A package/tool/tests, \`ipc/owner_turn.py\`, \`observability.py\`, today’s tracked edits, and session evidence are included and no secrets/runtime data are included.
- [ ] Run focused A2A/owner-turn tests, Ruff on the staged Python files, and \`git diff --cached --check\`; record unrelated full-repository Ruff findings without expanding scope.
- [ ] Commit with \`chore: preserve current working Yeoman state\`.

### Task 3: Promote the preserved state to \`main\` and isolate cleanup

**Files:**
- Modify: local \`main\` ref only
- Create: \`/home/dm/Documents/yeoman/.worktrees/source-cleanup\`
- Create: Git branch \`c/source-cleanup\`

- [ ] Verify the preservation commit is clean and contains the current WIP.
- [ ] Fast-forward local \`main\` to the preservation commit; do not push.
- [ ] Create the isolated cleanup worktree from the new \`main\`.

### Task 4: Remove only verified dead source and obsolete tests

**Files:**
- Delete: \`packages/gateway/yeoman_gateway/core/message.py\`
- Promote: \`tests/gateway/test_memory_cli.py\` and \`tests/gateway/test_responder_memory_recall.py\` from ignored local tests because they pass against the current gateway.
- Archive outside the repository: the 19 ignored legacy \`tests/test_*.py\` files that import the removed \`yeoman\` package and the stale \`tests/test_context_builder.py\` file.
- Modify: only documentation references proven to point exclusively to those deleted or archived files.

- [ ] Reconfirm \`core/message.py\` has no production callers and classify ignored root tests by current imports and fresh test results.
- [ ] Delete \`core/message.py\` in the isolated worktree, move the 20 stale ignored tests to \`/home/dm/Documents/yeoman-cleanup-archive-20260907/legacy-root-tests\`, and promote the two passing current tests under \`tests/gateway/\`.
- [ ] Run the focused tests and commit the tracked source/test cleanup as \`chore: remove obsolete gateway source\`; report the external archive separately.

Do not remove A2A, owner-turn, observability, reply-budget, WhatsApp, consciousness, memory, dormant API/webhooks, Discord/Feishu, or dependency paths in this pass without new evidence.

### Task 5: Clean stale Git metadata and clearly merged refs

**Files:**
- Modify: Git worktree metadata and local branch refs only
- Preserve: \`c/turn-engine-v2\`, \`c/turn-engine-v2-preserve-20260907\`, and all unmerged experimental branch commits

- [ ] Run \`git worktree prune --dry-run\`, then prune only missing worktree registrations.
- [ ] Delete only branches accepted by non-forced \`git branch -d\` as merged into \`main\`: \`c/bridge-watchdog-systemd\`, \`feat/langfuse-tracing\`, \`memory2-v1-rebuild\`, and \`phase0/code-health-pipeline-refactor\`.
- [ ] Inspect \`uv tool list\`; uninstall the legacy \`yeoman\` environment only if it exists and the active launcher resolves to \`yeoman-gateway\`.

### Task 6: Verify and report

**Files:**
- Inspect: source, refs/worktrees, systemd/runtime state
- Document: final task response and this committed plan

- [ ] Run focused A2A/owner-turn/API tests, full collection, Ruff on changed Python files, and diff checks from \`c/source-cleanup\`; run a second collection check from the live source after archiving the 19 ignored tests.
- [ ] Verify the live gateway/bridge services and editable package resolution were not changed by isolated cleanup.
- [ ] Reconcile remaining branches and deliberate non-deletions.
- [ ] Do not deploy the cleanup worktree until policy compatibility is revalidated and the cleaned source is intentionally selected as live.
