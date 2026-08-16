# Yeoman Rework Milestone 06: Stabilization and Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the post-cutover Core Stabilization Gate, retire the sealed legacy/toolkit recovery obligation, and declare the legacy-free Core Release complete.

**Architecture:** This milestone changes no product scope. It accumulates canonical live evidence, fixes only correctness/integrity/security defects through forward migrations, proves local recovery repeatedly, then performs an owner-authorized one-way destruction of the inert legacy bundle and temporary toolkit.

**Tech Stack:** Target typed status/evidence, SQLite integrity tools, local backup/restore, signed release manifests, Git, systemd, pytest/fault suites, and protected operator receipts.

## Global Constraints

- Read the orchestration file, normative §§12-16, 18.4, 18.7, and 19, plus `m05-handoff.json`.
- The target is forward-only; the old release may never execute for diagnosis, comparison, or recovery.
- Only corrective, integrity, or security work may intervene before mandatory Proactivity Phase 2.
- The stabilization gate requires 14 consecutive observation days and at least 100 trace-complete live interactions.
- Every shipped channel/modality and DM/group authority path, reconnect, restart, Edge replay, provider failure, Capsule tool, reminder, and periodic workload must be proven.
- Three verified post-cutover backup generations, disposable restores, and one post-cutover blank-root recovery are mandatory.
- No unexplained divergence, access widening, stuck intent, unresolved integrity/security fence, or undisposed unknown effect may remain.
- Bundle/key/toolkit destruction requires local action-bound owner step-up after both reviewer GOs.
- Proactivity remains absent and program status must explicitly remain incomplete.

---

## File map

- `packages/gateway/yeoman_gateway/status/stabilization.py` — redacted computation of gate facts from protected evidence references.
- `packages/gateway/yeoman_gateway/app/operator.py` — fixed `stabilization-status` and `retire-legacy-bundle` commands.
- `release/build-manifest.toml` — exhaustive forward-maintenance release update containing the inert commands before observation.
- `tests/stabilization/test_gate.py`, `test_trace_coverage.py`, `test_no_false_progress.py`, `test_bundle_retirement.py`.
- `docs/operations/core-stabilization.md` — current operator procedure with no private identifiers.
- Outside Git: protected daily gate snapshots, backup/restore receipts, bundle manifest, destruction receipt.

## Task 1: Implement a truthful Core Stabilization Gate evaluator

**Files:**

- Create: `status/stabilization.py`
- Modify: `app/operator.py`
- Modify: `release/build-manifest.toml`
- Test: stabilization tests named in the file map.

**Interfaces:**

- Consumes: protected evidence references and redacted module facts; it does not read message content.
- Produces: `CoreStabilizationStatus(ready, observation_start, consecutive_days, trace_count, coverage, blockers, freshness)` plus an inert exact-target retirement command that cannot execute before the gate and reviewer predicates pass.

- [ ] **Step 1: Write failing gate tests**

Assert elapsed time without fresh evidence does not advance; duplicate/replayed traces count once; incomplete causal graphs do not count; synthetic/migration/test events do not count; any unexplained divergence, widening, stuck intent, integrity/security fence, undisposed unknown, backup/restore gap, unauthorized generation change, or stale status blocks ready.

Also reject retirement for early day/trace count, missing coverage, fewer than three restores, no blank-root proof, object whose only verified copy remains in bundle, open unknown/blocker, generation/fence instability, mismatched bundle/toolkit digest, reused/expired step-up, absent pre-destruction reviewer GO, path/glob/symlink ambiguity, or a target equal to `/`, home, workspace root, or `.yeoman`.

- [ ] **Step 2: Write coverage tests**

Require at least one trace for WhatsApp/Telegram, every signed modality, DM/group and owner-private/shared authority paths, reconnect/restart/Edge replay/provider failure/Capsule tool/reminder/periodic workload. Require no unauthorized generation/fence change for twice the longest reconciliation interval.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/stabilization -q
```

Expected: FAIL on missing evaluator.

- [ ] **Step 4: Implement protected query plus redacted output**

Every count derives from stable opaque trace IDs with exact eligibility predicates and freshness. Status exposes counts/categories/blockers, never chat/principal/content labels or hidden-domain existence to an unauthorized caller. Owner audit drill-down requires fresh Trust and step-up where designated. Implement `retire-legacy-bundle` now, before observation begins: it resolves only explicit manifest paths/digests, displays exact targets/recovery consequence in the step-up action digest, and remains non-executable until the ready status plus both pre-destruction GOs are current.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/stabilization tests/status -q
git add packages/gateway/yeoman_gateway/status/stabilization.py packages/gateway/yeoman_gateway/app/operator.py release/build-manifest.toml tests/stabilization
git commit -m "feat(status): evaluate core stabilization evidence"
```

- [ ] **Step 6: Prove, review, sign, and deploy the forward-only maintenance release**

From a clean clone of the exact Task 1 commit, rerun the full affected release, artifact, status/redaction, operator step-up, retirement-target, backup/recovery, fence/startup, and forbidden-residual gates; build a non-editable artifact and prove all executable/runtime/package-data digests. Obtain targeted architecture and security GOs bound to the commit/build/manifest/activation diff and proof outputs. Then obtain a fresh owner step-up bound to that exact signed forward-only upgrade, activate/deploy through the canonical generation compare-and-swap while effects are fenced, reopen capabilities only after staged generation/postcondition proof, and record the protected upgrade receipt. Any correction creates a new commit and repeats this step. Observation has not started yet, so this is the baseline release rather than a cohort reset.

## Task 2: Accumulate 14 consecutive days and 100 qualifying traces

**Files:**

- Create outside Git: one protected daily gate snapshot per observation day.
- Modify source only for reviewed forward fixes; each fix receives its own test/commit/release proof.

- [ ] **Step 1: Record the observation start**

Begin only after Task 1 Step 6 is complete and the post-upgrade status snapshot shows that exact reviewed/signed/deployed release/build/manifest/activation digest containing the tested retirement command, current config/policy/schema/key/control generations, all required capabilities scoped correctly, current backup, no unexplained migration divergence, and no open integrity/security blocker.

- [ ] **Step 2: Observe normal authorized use**

Do not manufacture conversation or proactive output to reach a quota. Count ordinary trace-complete live interactions; owner-created explicit smoke interactions are allowed only to cover a required shipped path and must use normal channel authority.

- [ ] **Step 3: Apply exact observation-window invalidation rules**

Restart the full 14-day window and recount only post-change traces after any deployed executable/build-manifest digest change; storage schema migration; config, policy, provider-policy, activation, or control generation change; change to Trust, domain/audience, memory, Evidence, egress, Action/Delivery, Edge, workload, Capsule, backup/recovery, fence, or trace-eligibility semantics; or integrity/security correction. A credential/key rotation also restarts unless it is a pure rewrap with identical alias/scope/authority and both reviewers accept a targeted recovery/route revalidation. Daily backup generation, ordinary receipt append, current status measurement, and documentation-only text changes do not reset. Resource tuning inside an already signed/tested envelope pauses counting until fresh capacity/postcondition proof, but resets if a limit or behavior contract changes.

- [ ] **Step 4: Exercise failure and modality coverage safely**

Use controlled fault injection or disposable environments for provider failure, restart, reconnect, Edge replay, Capsule, reminder, periodic workload, and each modality. Never risk live memory or duplicate external effects merely to satisfy coverage. Link each proof receipt to the gate category.

- [ ] **Step 5: Reset consecutive-day start after a qualifying blocker**

Any integrity/security uncertainty, unexplained divergence, access widening, unauthorized generation/fence change, or trace-evidence defect resets the consecutive window after the forward fix and its proof. Ordinary availability degradation does not reset it when evidence proves no safety/correctness impact.

## Task 3: Prove post-cutover backups and recoveries

**Files:**

- Create outside Git: three verified generation/restore receipts and one blank-root recovery receipt.
- Modify: operator docs only if the executed procedure exposes a factual gap.

- [ ] **Step 1: Close and verify three independent post-cutover generations**

Each generation must include every object whose only old copy remains in the sealed bundle. Verify manifest, SQLite integrity/foreign keys/schema, blob reachability/decryption, keys, newest independent lifecycle head, cutoffs, and pending/unknown effects.

- [ ] **Step 2: Restore each generation into disposable storage**

Boot effects disabled and leases/jobs inert, reconcile all stores/cutoffs/effects, rebuild projections, and prove same authority/lifecycle state. Delete disposable plaintext/state through the approved recoverable cleanup procedure after recording protected results.

- [ ] **Step 3: Perform one post-cutover blank-root recovery**

Use only the signed current release, documented inputs, newest approved local backup, Secret Authority recovery-only mode, and one separately stored recovery-key copy. Keep Edge/Egress/Delivery fenced until reconciliation and deliberate owner release. Rotate credentials/key epochs if the rehearsal models compromise.

- [ ] **Step 4: Exercise the second recovery-key copy**

Use a separate disposable restore and prove both local copies work independently; neither key or plaintext appears in Git, logs, status, or backup ciphertext location.

## Task 4: Reconcile every unknown and blocker

**Files:**

- Create outside Git: canonical reconciliation or owner-disposition receipts.
- Test/modify source only when a defect requires a forward fix.

- [ ] **Step 1: Enumerate pending and unknown state**

Query typed status and protected owner audit for Interaction processing, provider attempts, Capsule effects, Action/Delivery, Edge cutoffs, workload commands, backup generations, migrations, and fences. Empty status must be fresh and evidence-backed.

- [ ] **Step 2: Close only through authoritative evidence or owner disposition**

For `external_effect_unknown`, use platform/provider receipt query when authorized; otherwise bind owner disposition to exact effect/destination/evidence. Never redispatch automatically or rewrite unknown to succeeded. Resolve stuck intents through the owning coordinator and record causality.

- [ ] **Step 3: Re-run isolation and residual gates**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy packages/shared packages/gateway packages/workloads packages/overseer
uv build --all-packages
cd packages/bridge && npm test && npm run build
```

Expected: all pass on current exact release. Clean artifact/source/installed scans contain no old runtime path, migration toolkit, unsupported module, persona evolution, or proactivity.

## Task 5: Authorize and execute sealed legacy/toolkit retirement

**Files:**

- Use unchanged: the Task 1 tested `app/operator.py` retirement command and `test_bundle_retirement.py`.
- Delete after gate: sealed legacy source/state bundle, its runtime-capable key, migration-toolkit artifact/worktree/branch, and old live checkout/service artifacts.

**Interfaces:**

- Consumes: ready stabilization status, exact bundle/toolkit manifests, both reviewer GOs, and action-bound owner step-up.
- Produces: canonical destruction/disposition receipt; it cannot claim unverifiable physical secure erase.

- [ ] **Step 1: Re-run retirement denial and exact-target dry-run tests**

Run `uv run pytest tests/stabilization/test_bundle_retirement.py -q` against the unchanged observed build and exact proposed manifest. Any source/build/config/policy/schema change resets the observation gate under Task 2.

- [ ] **Step 2: Close the exact retirement manifest and dry run**

Resolve explicit manifest paths and digests without globs, symlink traversal, broad roots, or environment-variable targets. Confirm the dry run displays the exact targets/recovery consequence and refuses every broad or unresolved target. Do not modify the command or deployed release.

- [ ] **Step 3: Obtain milestone architecture/security GO before deletion**

Reviewers inspect the full stabilization proof, backup/restore evidence, current release residual scan, exact retirement manifest, and deletion command. Any NO-GO blocks deletion.

- [ ] **Step 4: Retire recoverably where possible, then destroy the bundle key**

Stop/prove absent every old unit/process, remove old installed/service/cache artifacts by explicit path, delete toolkit artifact/worktree and branch after confirming Git preservation history, and destroy the sealed bundle encryption key where practical. Record manifest/digest/time/actor/method and state that physical secure erase is not claimed.

- [ ] **Step 5: Prove old execution is impossible**

Scan systemd units/timers, PATH/entrypoints, Python/Node installations, source worktrees/branches, runtime directories, caches, sockets, processes, build manifests, and target imports. Attempting the old service/build identity must fail before state access.

- [ ] **Step 6: Obtain post-destruction architecture and security GOs**

Send both reviewers the canonical destruction receipt, exact deleted-target manifest, key-destruction evidence and limitation statement, post-action process/unit/path/import/cache scan, current target backup/recovery proof, and proof that old execution fails before state access. These GOs are distinct from the pre-destruction authorization. Any residual or unverifiable target blocks Core Release completion and requires forward remediation.

## Task 6: Close the Core Release milestone

**Files:**

- Create: `artifacts/program/handoffs/m06-handoff.json`
- Modify: orchestration state and current operator docs.
- Preserve: normative target architecture and active Milestone 07 plan.

- [ ] **Step 1: Generate final Core Stabilization status**

Expected: 14 consecutive days, at least 100 eligible traces, complete coverage, three verified backups/restores, blank-root proof, current backup covering bundle-only objects, no blockers/unknowns, stable generations/fences, retirement receipt, and both post-destruction reviewer GOs.

- [ ] **Step 2: Commit only release-safe documentation/receipt**

```bash
git add artifacts/program/handoffs/m06-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md docs/operations/core-stabilization.md
git commit -m "docs(release): complete legacy-free core stabilization"
```

- [ ] **Step 3: Advance directly to Proactivity Phase 2**

Set orchestration to `READY_FOR_MILESTONE_07_ACTIVATION`, Core Release `complete`, Architecture Program `incomplete`, and proactivity `required next feature milestone`. If Milestone 07 Tasks 1-4 Step 3 produced green provisional non-activated commits during this observation window, rebase/revalidate them against exact `m06` and begin Task 4 Step 4 immediately; otherwise start Milestone 07 in a fresh task. No unrelated feature may intervene.
