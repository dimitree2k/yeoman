# Yeoman Rework Milestone 07: Proactivity Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver consciousness/speak-up as one domain-isolated registered Proactivity Workload, stage it to owner-approved narrow autonomy, prove its observation gate, and close the architecture program without persona evolution or stale artifacts.

**Architecture:** The new `assistant.proactivity` workload observes only explicitly granted canonical references through the Workload API, stores per-domain private checkpoints, and commits expiring proposals into core. Trust, Action, and Delivery independently authorize every send; preference/taste is ordinary derived memory and can never mutate persona, policy, grants, or definitions.

**Tech Stack:** Existing Python 3.14 Workload Fabric, SQLite canonical command/effect state, encrypted per-domain workload volumes, Controlled Egress, Trust, Action/Delivery, pytest/property/fault tests, signed Phase 2 build/activation manifests, and protected observation evidence.

## Global Constraints

- Read the orchestration file and normative §§3, 4.4, 4.7-4.8, 6-13, 16.2, 17, 19.4, and 20-21. Provisional build-only Tasks 1-4 Step 3 may use `m05-handoff.json` during stabilization; Task 4 Step 4 and every merge/build-for-install, activation, or observation task require accepted `m06-handoff.json`.
- Core Stabilization must be accepted before activation; implementation may run during Milestone 06 only in `~/Documents/yeoman-proactivity` on `c/yeoman-proactivity-phase2`, producing provisional protected receipts that cannot advance the registry, install, merge, activate, or change the observed Core Release. Every M06 Core change invalidates the lane; accepted `m06` requires rebase, full affected-gate rerun, and fresh reviews.
- The workload principal is exactly `assistant.proactivity` and never inherits owner read-all.
- Observations, preferences, budgets, cooldowns, deduplication, and checkpoints are compartmentalized by exact context domain and key epoch.
- A proposal is content, not authority; it cannot deliver directly.
- Restart/restore/duplicate instance starts inert and never replays sent or possibly sent proposals.
- Activation stages are `inert_shadow`, `proposal_only`, then owner-approved `narrow_autonomous`; no stage widens automatically.
- Persona evolution remains completely absent: no config, schema, model route, schedule, job, CLI, middleware, ledger, writer, auto-apply, test, or support doc.
- Taste/preference memory cannot mutate static persona, system instructions, policy, grants, workload definition, or destination scope.
- Completion requires 14 consecutive days and at least 100 trace-complete proactive decisions with zero cross-domain, wrong-audience, unauthorized, or duplicate delivery.

---

## File map

- `packages/gateway/yeoman_gateway/workloads/proposals.py` — canonical `SpeakUpProposal`, budget, cooldown, approval, outcome, and dedup state.
- `packages/gateway/yeoman_gateway/workloads/proactivity_activation.py` — owner-approved stage/destination/action-class activation generations.
- `packages/gateway/yeoman_gateway/workloads/proactivity_schema.py`, `proactivity_repository.py`, `migrations/proactivity_0001.py` — Workloads-owned DDL, repository, and registered core migration.
- `packages/gateway/yeoman_gateway/app/proactivity_operator.py` — local action-bound stage/approval commands; no chat-origin administration.
- `packages/workloads/yeoman_workloads/proactivity/models.py` — observation and decision types.
- `packages/workloads/yeoman_workloads/proactivity/triggers.py` — scheduled, burst, lull, and explicit/manual trigger normalization.
- `packages/workloads/yeoman_workloads/proactivity/decision.py` — bounded proposal/no-action decision using standard ports.
- `packages/workloads/yeoman_workloads/proactivity/checkpoint.py` — per-domain encrypted private state only.
- `packages/workloads/yeoman_workloads/proactivity/service.py` — fixed registered workload entrypoint.
- `packages/workloads/yeoman_workloads/definitions/proactivity.toml` — immutable Phase 2 definition added only to the distinct Phase 2 build.
- `packages/gateway/yeoman_gateway/status/proactivity.py` — redacted activation/observation gate facts.
- `docs/operations/proactivity.md` — current stage, approval, disable, and incident procedure.
- `tests/proactivity/` — semantic, isolation, stage, crash, restore, revocation, audience, effect, and observation suites.

## Task 1: Add canonical proposals, budgets, cooldowns, and activation state

**Files:**

- Create: Gateway proposal/activation/schema/repository/migration files from the file map.
- Modify: `packages/gateway/yeoman_gateway/storage/migrations.py` to register the Workloads-owned migration in global order.
- Test: `tests/proactivity/test_proposals.py`, `test_budgets.py`, `test_activation.py`, `test_dedup.py`, `test_restart.py`, `test_storage_ownership.py`, `test_migration.py`

**Interfaces:**

- Consumes: workload-authenticated proposal command with exact source manifest and lease.
- Produces: canonical expiring `SpeakUpProposal` plus budget/cooldown/dedup facts; it grants no send authority.

- [ ] **Step 1: Write failing proposal-state tests**

Require proposal ID/content blob/source object manifest/domain/destination intent/reply or mention target/classification/audience/grants/generations/expiry. Assert exact budget consumption and cooldown/dedup commit atomically with proposal; duplicate instances cannot double spend or duplicate proposal; expired/cancelled/probably-sent items remain inert after restart.

Assert only the Gateway Workloads module owns proposal/budget/cooldown/dedup/approval/outcome/activation DDL and repository methods. Register `proactivity_0001` through the global storage migration order; reject direct SQL from the Phase 2 worker and prove migration rollback/upgrade atomicity.

- [ ] **Step 2: Write failing activation tests**

Activation is compare-and-swap, action-bound owner step-up, and exact to stage, destination set, trigger/action classes, routes, budgets, quiet windows, minimum gaps, and current generations. Stage order cannot skip or widen automatically; disabling/fencing narrows immediately.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/proactivity/test_proposals.py tests/proactivity/test_budgets.py tests/proactivity/test_activation.py tests/proactivity/test_dedup.py tests/proactivity/test_restart.py tests/proactivity/test_storage_ownership.py tests/proactivity/test_migration.py -q
```

Expected: FAIL because Phase 2 core state does not exist.

- [ ] **Step 4: Implement module-owned proposal schema and service**

Implement Workloads-owned tables/repository/migration for proposals, source manifests, budget ledger, cooldown/dedup keys, approvals, outcomes, and activation generations. Use canonical Action/Delivery owners for commands/effects and receipts. Immediately before any approved delivery, re-resolve destination, membership/audience, reply/mention relationship, identity/grant revocation, classification, current generations, quiet window, budget/cooldown, fences, and lease. Persist `external_effect_unknown` terminally.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/proactivity/test_proposals.py tests/proactivity/test_budgets.py tests/proactivity/test_activation.py tests/proactivity/test_dedup.py tests/proactivity/test_restart.py tests/proactivity/test_storage_ownership.py tests/proactivity/test_migration.py -q
git add packages/gateway/yeoman_gateway/workloads/proposals.py packages/gateway/yeoman_gateway/workloads/proactivity_activation.py packages/gateway/yeoman_gateway/workloads/proactivity_schema.py packages/gateway/yeoman_gateway/workloads/proactivity_repository.py packages/gateway/yeoman_gateway/workloads/migrations/proactivity_0001.py packages/gateway/yeoman_gateway/storage/migrations.py tests/proactivity
git commit -m "feat(proactivity): add canonical proposal authority state"
```

## Task 2: Implement the per-domain Proactivity Workload

**Files:**

- Create: workload proactivity package and definition from the file map.
- Test: `tests/proactivity/test_observations.py`, `test_triggers.py`, `test_domain_compartments.py`, `test_checkpoint.py`, `test_port_boundaries.py`

**Interfaces:**

- Consumes: explicitly granted canonical object/event references, purpose, model routes, budget, observation set, and renewable lease.
- Produces: typed `NoAction` or expiring proposal command through Workload API; never canonical memory or Delivery directly.

- [ ] **Step 1: Write failing authority and compartment tests**

Assert `assistant.proactivity` has no owner read-all, database/projection handles, channel/model credentials, arbitrary network destination, grant mutation, direct memory persistence, or direct delivery. Identical people across groups produce separate observations/preferences/checkpoints/keys/budgets. Owner-private aggregation can emit only owner-private proposals and cannot feed shared destination.

- [ ] **Step 2: Write trigger behavior tests**

Normalize scheduled, burst, lull, and explicit/manual triggers; per-destination enable/profile; quiet windows; daily/burst budget; minimum gaps; approval/preview; reply targeting; and outcome classification. A trigger is input, never authority. Stale lease/audience/generation produces `NoAction` with protected evidence.

- [ ] **Step 3: Write preference/taste tests**

Store learned preference only by submitting a memory candidate with exact domain/source/audience/classification/grants. Assert it cannot modify static persona digest, policy/config generation, system instructions, grants, workload definition, or another domain's decision.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/proactivity/test_observations.py tests/proactivity/test_triggers.py tests/proactivity/test_domain_compartments.py tests/proactivity/test_checkpoint.py tests/proactivity/test_port_boundaries.py -q
```

Expected: FAIL on missing workload package.

- [ ] **Step 5: Implement smallest workload and commit**

The service uses the fixed runner, one immutable definition, standard Trust/Memory/Egress/Workload ports, and a per-domain encrypted checkpoint keyed by workload instance+domain+purpose+key epoch. Checkpoint contains only private observation cursor/decision hints; canonical proposals/budgets/effects remain in core.

```bash
uv run pytest tests/proactivity -q
git add packages/workloads/yeoman_workloads/proactivity packages/workloads/yeoman_workloads/definitions/proactivity.toml tests/proactivity
git commit -m "feat(proactivity): add domain-isolated workload"
```

## Task 3: Migrate proactive semantics from canonical migrated evidence

**Files:**

- Create: `packages/gateway/yeoman_gateway/workloads/proactivity_migration.py`
- Test: `tests/proactivity/test_semantic_migration.py`, `test_legacy_sent.py`, `test_inert_authority.py`

**Interfaces:**

- Consumes: protected canonical migration records from target state only.
- Produces: Phase 2 observation/preference candidates and inert historical dispositions; it never reads sealed bundle/toolkit.

- [ ] **Step 1: Write failing behavior-inventory tests**

Cover scheduled/burst/lull/manual semantics, enablement/profile, quiet windows, budgets/gaps, approval/preview, reply targeting, outcome classification, and domain-scoped preference learning. Preserve behavior meaning, not legacy classes/prompts/event buses/JSON/SQLite/bootstrap.

- [ ] **Step 2: Write legacy effect/authority tests**

Legacy `sent` without platform receipt remains `external_effect_unknown`; pending proposals, approval codes, timers, desired-running state, scheduler state, and leases become cancelled/inert evidence. No record creates a Phase 2 lease, active schedule, budget, proposal, or delivery.

- [ ] **Step 3: Implement target-only semantic migration and verify**

```bash
uv run pytest tests/proactivity/test_semantic_migration.py tests/proactivity/test_legacy_sent.py tests/proactivity/test_inert_authority.py -q
git add packages/gateway/yeoman_gateway/workloads/proactivity_migration.py tests/proactivity
git commit -m "feat(proactivity): migrate inert historical semantics"
```

## Task 4: Build and activate `inert_shadow`

**Files:**

- Create: `packages/gateway/yeoman_gateway/app/proactivity_operator.py`
- Modify: `release/build-manifest.toml`, `release/activation-manifest.schema.json`
- Create: `tests/proactivity/fixtures/inert_shadow.json`; create protected activation receipt outside Git only.
- Test: `tests/proactivity/test_inert_shadow.py`, `test_no_effect_surface.py`

**Interfaces:**

- Consumes: owner-signed Phase 2 build, static persona digest, authorized observation set, no effect capability.
- Produces: trace-complete shadow decisions/comparisons with no proposal intended for external effect and no delivery.

- [ ] **Step 1: Write failing Phase 2 artifact and inert-operator tests**

Diff Core Release and Phase 2 manifests. The only new executable surface is the proactivity package/definition and required core schema/commands; no persona evolution, old consciousness code, generic scheduler, new provider/tool route, or hidden destination appears.

Assert the local operator accepts only a reviewed `inert_shadow` activation diff bound to exact build/schema/config/policy/key/control/definition generations, authorized observation set, owner action digest, nonce, and expiry. It cannot activate another stage, create an effect capability, or accept chat/remote origin.

- [ ] **Step 2: Run the failing inert-shadow tests**

Assert workload can observe only granted references, uses sanitized deterministic replay comparison, creates `NoAction`/shadow evidence, cannot create effectful proposal/delivery, and starts inert on restart/restore/control loss.

```bash
uv run pytest tests/proactivity/test_inert_shadow.py tests/proactivity/test_no_effect_surface.py -q
```

Expected: FAIL because the Phase 2 manifest and local inert activation command do not exist.

- [ ] **Step 3: Implement, verify, and commit the inert artifact boundary**

```bash
uv run pytest tests/proactivity/test_inert_shadow.py tests/proactivity/test_no_effect_surface.py -q
git add packages/gateway/yeoman_gateway/app/proactivity_operator.py release/build-manifest.toml release/activation-manifest.schema.json tests/proactivity/test_inert_shadow.py tests/proactivity/test_no_effect_surface.py tests/proactivity/fixtures/inert_shadow.json
git commit -m "feat(proactivity): add inert shadow release boundary"
```

- [ ] **Step 4: Rebase and prove the exact post-Core commit**

After `m06` is accepted, rebase the provisional commits on the exact stabilized Core commit, require a clean worktree, rebuild the artifact, and rerun full tests plus every affected release/schema/migration/isolation gate. Record the exact rebased source commit and new build digest; provisional pre-rebase receipts are never activation evidence.

- [ ] **Step 5: Obtain reviewer GO and activate exact scope**

Architecture/security reviewers inspect the exact clean rebased commit/artifact diff, authority graph, compartment keys, migrated state, and no-effect proof. Sign that build/activation pair, then owner activates `inert_shadow` with action-bound step-up. Observe until every enabled trigger/destination type has stable trace coverage and zero isolation/authority defect. Any source/test/manifest change requires a new commit, proof, signatures, and reviews before activation.

## Task 5: Advance to `proposal_only`

**Files:**

- Modify: `packages/gateway/yeoman_gateway/app/proactivity_operator.py`
- Modify: `release/build-manifest.toml`
- Test: `tests/proactivity/test_proposal_only.py`, `test_owner_approval.py`, `test_revalidation.py`, `test_expiry.py`
- Create outside Git: protected activation and approval/delivery receipts.

- [ ] **Step 1: Write failing proposal-only semantics tests**

Every proposal is canonical, expiring, destination-bound, source-complete, and inert without exact action-bound owner approval. Approval does not bypass fresh Trust/Delivery checks. Changed audience/membership/grant/reply target/generation/fence/quiet window/budget/lease denies without send.

- [ ] **Step 2: Write failing crash/restore/duplicate tests**

Crash before/after proposal, approval, intent, dispatch, and receipt. Assert no duplicate send, budget double spend, stale approval reuse, or replay of sent/possibly-sent proposal. Unknown remains unknown.

- [ ] **Step 3: Run the failing stage-transition tests**

```bash
uv run pytest tests/proactivity/test_proposal_only.py tests/proactivity/test_owner_approval.py tests/proactivity/test_revalidation.py tests/proactivity/test_expiry.py -q
```

Expected: FAIL because the local operator does not expose proposal-only activation or exact proposal approval.

- [ ] **Step 4: Implement, verify, and commit proposal-only administration**

Add only local action-bound `activate-proposal-only`, `approve-proposal`, and `disable-proactivity` operations. Bind each to exact object/scope/diff and generations; no bulk, wildcard, chat-origin, replay, or implicit approval exists.

```bash
uv run pytest tests/proactivity/test_proposal_only.py tests/proactivity/test_owner_approval.py tests/proactivity/test_revalidation.py tests/proactivity/test_expiry.py -q
git add packages/gateway/yeoman_gateway/app/proactivity_operator.py release/build-manifest.toml tests/proactivity/test_proposal_only.py tests/proactivity/test_owner_approval.py tests/proactivity/test_revalidation.py tests/proactivity/test_expiry.py
git commit -m "feat(proactivity): add proposal-only administration"
```

- [ ] **Step 5: Rebuild, review, sign, and activate the exact commit**

Rerun the full affected release/activation/isolation/effect gate from a clean worktree, rebuild/sign the exact commit, and obtain fresh architecture/security GOs over its destination/trigger/action-class scope and prior-stage evidence. Then activate `proposal_only`; every live send requires owner approval and must end in receipt-backed or explicit unknown evidence.

## Task 6: Advance to owner-approved `narrow_autonomous`

**Files:**

- Modify: `packages/gateway/yeoman_gateway/app/proactivity_operator.py`
- Modify: `release/build-manifest.toml`
- Test: `tests/proactivity/test_narrow_autonomous.py`, `test_revocation.py`, `test_cross_domain.py`, `test_audience.py`, `test_effects.py`
- Create outside Git: protected scope and activation receipt.

- [ ] **Step 1: Write failing narrow exact-activation tests**

Name each destination, destination type, trigger/action class, provider profile, modality, reply behavior, quiet window, budget, gap, lease, and generation. Anything omitted remains disabled. Do not infer scope from successful proposal-only history.

- [ ] **Step 2: Write failing isolation/revocation/effect tests**

Cover same-person/different-group, owner-private aggregation, membership expansion/removal, unknown audience, grant/config/policy/key generation change, lease expiry, duplicate instances, reconnect, restart, restore, provider failure, Delivery unknown, and output taint.

- [ ] **Step 3: Run the failing autonomous-transition tests**

```bash
uv run pytest tests/proactivity/test_narrow_autonomous.py tests/proactivity/test_revocation.py tests/proactivity/test_cross_domain.py tests/proactivity/test_audience.py tests/proactivity/test_effects.py -q
```

Expected: FAIL because the local operator cannot activate autonomous scope.

- [ ] **Step 4: Implement, verify, and commit the narrow transition**

Add one compare-and-swap local activation command that accepts only the complete exact diff from Step 1, proves proposal-only predecessor evidence, and refuses omitted/wildcard destinations, routes, action classes, budgets, generations, or expiry. Disable remains immediately narrowing.

```bash
uv run pytest tests/proactivity/test_narrow_autonomous.py tests/proactivity/test_revocation.py tests/proactivity/test_cross_domain.py tests/proactivity/test_audience.py tests/proactivity/test_effects.py -q
git add packages/gateway/yeoman_gateway/app/proactivity_operator.py release/build-manifest.toml tests/proactivity/test_narrow_autonomous.py tests/proactivity/test_revocation.py tests/proactivity/test_cross_domain.py tests/proactivity/test_audience.py tests/proactivity/test_effects.py
git commit -m "feat(proactivity): add narrow autonomous activation"
```

- [ ] **Step 5: Rebuild, review, sign, and activate the exact commit**

Rerun the full affected release/activation/isolation/effect gate from a clean worktree, rebuild/sign the exact commit, obtain fresh architecture/security GOs, then activate only the reviewed exact scope. Every autonomous send still passes fresh Trust and Delivery and has canonical intent/receipt/unknown. Any cross-domain, wrong-audience, unauthorized, or duplicate event immediately fences proactivity and restarts its observation window after correction.

## Task 7: Prove the Proactivity observation gate

**Files:**

- Create: `packages/gateway/yeoman_gateway/status/proactivity.py`
- Modify: `release/build-manifest.toml`
- Test: `tests/proactivity/test_observation_gate.py`, `test_no_speaking_incentive.py`, `test_delivery_coverage.py`
- Create outside Git: protected daily observation snapshots.

**Interfaces:**

- Consumes: canonical proactive decision/effect traces and activation generations.
- Produces: redacted `ProactivityGateStatus` with exact cohort digest, days, unique decisions, delivery coverage, blockers, and freshness.

- [ ] **Step 1: Write failing gate tests**

Require 14 consecutive days and at least 100 unique trace-complete decisions from one exact cohort digest over: runtime-source closure digest (all executable/package/dependency/unit/schema/migration/runtime-manifest paths, explicitly excluding documentation/handoff-only paths), executable artifact digest, runtime section of the exhaustive build manifest, Workloads/proactivity schema and migration, config, policy, provider policy, key, control, activation, definition, static persona, and eligibility-rule generations/digests. Record the full signed manifest and Git commit as provenance, but do not make documentation-only paths a behavioral cohort input. Count `no_action`, expired, denied, deduplicated, and sends so the gate creates no incentive to speak. Also require at least one receipt-backed authorized delivery and outcome trace for every enabled destination type and autonomous action class.

- [ ] **Step 2: Run the failing gate tests**

```bash
uv run pytest tests/proactivity/test_observation_gate.py tests/proactivity/test_no_speaking_incentive.py tests/proactivity/test_delivery_coverage.py -q
```

Expected: FAIL because the cohort evaluator does not exist.

- [ ] **Step 3: Implement the pure cohort evaluator**

Read only canonical decision/effect facts and immutable generation/digest inputs. Return redacted counts, coverage, blockers, reset reason, and freshness; do not expose destination, principal, content, or hidden-trace identifiers and do not generate decisions.

- [ ] **Step 4: Verify and commit before observation begins**

```bash
uv run pytest tests/proactivity/test_observation_gate.py tests/proactivity/test_no_speaking_incentive.py tests/proactivity/test_delivery_coverage.py -q
git add packages/gateway/yeoman_gateway/status/proactivity.py release/build-manifest.toml tests/proactivity/test_observation_gate.py tests/proactivity/test_no_speaking_incentive.py tests/proactivity/test_delivery_coverage.py
git commit -m "feat(proactivity): add exact observation cohort gate"
```

- [ ] **Step 5: Build, review, sign, and deploy the exact cohort commit**

From a clean worktree rerun every affected release/status/redaction/activation gate, build and sign the exact commit, compute the runtime-source closure and runtime-manifest-section digests, obtain architecture/security GOs for the evaluator and cohort definition, and owner-sign a new activation generation that preserves the reviewed narrow-autonomous scope while binding it to this exact executable artifact. Activate/deploy only that build/activation pair. Observation begins only after the protected opening snapshot proves the deployed runtime closure/artifact/manifest section, activation, and every cohort component. Any later runtime source, test that changes runtime semantics, dependency, unit, schema, migration, runtime-manifest section, executable artifact, or other cohort-component change creates a new commit, signatures, reviews, and cohort.

- [ ] **Step 6: Accumulate normal staged evidence**

Do not generate unsolicited messages to reach counts. Observe ordinary decisions and record protected daily status. Any cohort component change, deployed runtime code/schema/config/policy/provider/key/control/activation/definition/eligibility change, integrity/security correction, or isolation/authority/duplicate defect starts a new cohort and resets both day and decision counts. Availability defects pause counting only when the exact cohort is unchanged and evidence proves no safety/correctness impact. The only post-observation exception is Task 8's pre-reviewed documentation-only cleanup: it must leave runtime-source closure, runtime manifest section, executable artifact, activation, and every other cohort component byte-identical; otherwise the exception fails and the full cohort restarts.

- [ ] **Step 7: Run the complete Phase 2 gate**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy packages/shared packages/gateway packages/workloads packages/overseer
uv build --all-packages
cd packages/bridge && npm test && npm run build
```

Expected: all pass; static and artifact scans prove persona evolution/old consciousness are absent and only the new registered proactivity implementation exists.

## Task 8: Final review, documentation cleanup, and program completion

**Files:**

- Create outside Git: final protected `m07` Architecture Program receipt and immutable accepted-contract registry digest.
- Update: current architecture/operator/product docs and `CHANGELOG.md`.
- Modify: only the documentation inventory/signature section of `release/build-manifest.toml`; runtime sections may not change.
- Delete from final release branch: all superseded specs/plans/session notes/diagrams/procedures, completed handoff JSON files, and this orchestration/milestone plan set after importing their accepted digests into protected program evidence.
- Preserve: owner-approved normative architecture or its current canonical successor, current operator docs, release manifests/schemas/migrations, and sanitized templates.

- [ ] **Step 1: Obtain final architecture and security GOs**

Reviewers inspect the Phase 2 runtime closure/artifact/activation, 14-day/100-decision evidence, delivery coverage, isolation/revocation/crash/restore/effect proof, persona-evolution absence, current residual scan, and exact cleanup deletion/update set. Before cleanup they must classify every changed path as documentation/handoff-only and confirm the proposed runtime-manifest section remains byte-identical. Any runtime, package-data, schema, migration, dependency, test-semantics, unit, activation, or cohort-relevant change blocks the exception and restarts Task 7 observation after correction.

- [ ] **Step 2: Import plan/handoff digests into protected Evidence**

Record plan paths/digests, accepted milestone commits, every cumulative contract/schema/migration/interface/definition digest, prior reviewer decisions, final release/activation/cohort generations, proposed cleanup set, and proposed final program status without copying private content. Write but do not close the protected `m07` receipt body; the exact cleanup commit and clean-clone proof are not known yet. Freeze the immutable final accepted-contract registry digest before the source copies are deleted. Git history remains the historical documentation archive.

- [ ] **Step 3: Delete stale planning/session documentation by explicit path**

Create a dedicated `~/Documents/yeoman-final-cleanup` worktree on `c/yeoman-final-cleanup` from the exact accepted Phase 2 commit. Use `git rm` on the pre-reviewed exact list, never a broad recursive home/workspace target. Keep only current normative and operator/product documents, update `CHANGELOG.md`, update only the exhaustive manifest's documentation inventory/signature input, and commit the candidate as `docs(release): prepare final target documentation`. Do not merge or tag it yet.

- [ ] **Step 4: Run final clean-clone proof**

```bash
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run mypy packages/shared packages/gateway packages/workloads packages/overseer
uv build --all-packages
cd packages/bridge && npm ci && npm test && npm run build
```

Run these commands from a fresh clean clone of the candidate cleanup commit, then run `scripts/release/verify_artifact.py` and the full forbidden-residual scan against source, artifacts, disposable install, units/timers, runtime caches, schemas/config keys, tests, and docs. Prove the runtime-source closure digest, runtime manifest section, executable artifacts, units, schemas/migrations, dependencies, activation, definition, and every other observation-cohort input are byte-identical to Task 7; only the Git commit, approved document paths, documentation-manifest section, full manifest digest/signature, and protected evidence may differ. Owner-sign the updated exhaustive manifest. Any other difference invalidates the cohort and returns to Task 7 Step 5 for a full new observation window.

- [ ] **Step 5: Close the immutable protected `m07` receipt body**

Finalize the body with the exact candidate cleanup commit, observed runtime-source/artifact/runtime-manifest-section/activation/cohort digests, final full documentation-updated manifest digest/signature, clean-clone/bit-identity/residual-proof commitments, exact deleted/preserved document lists, final accepted-contract registry digest, and program-complete assertion. Close it immutably and compute its digest before requesting review. The body has no mutable reviewer slots and is never rewritten after a GO; reviewer receipts are separate signed objects.

- [ ] **Step 6: Obtain post-cleanup architecture and security GOs**

Send both reviewers the exact candidate commit/diff, immutable protected `m07` receipt-body digest, clean-clone command outputs, source/build/install/unit/runtime residual scans, observed runtime cohort digests, final documentation-updated exhaustive-manifest digest/signature, unchanged activation digest, and current operator-document set. Each signed GO must bind every exact digest, affirm the bit-identical runtime/non-invalidation proof, and name the standing reviewer task ID. These GOs are distinct from the pre-cleanup authorization in Step 1. Any stale or missing current artifact blocks completion; corrections create a new candidate commit, new receipt body, and rerun affected proof.

- [ ] **Step 7: Commit final cleanup and declare completion**

Fast-forward the target release branch to the exact reviewed cleanup commit and verify its digest. Do not mutate the protected `m07` receipt body reviewed in Step 6. Create a cryptographically signed final architecture-program tag whose signed message binds the exact released commit, normative-spec digest, observed runtime-source closure/executable artifact/runtime-manifest-section/encrypted-activation/observation-cohort digests, final documentation-updated exhaustive-manifest digest/signature, immutable protected `m07` receipt-body digest, final accepted-contract registry digest, and both separate architecture/security GO receipt digests/task IDs. This signed tag is the terminal release-safe handoff reference; `next_milestone` is null. Verify the tag from the pinned program public key, then delete the temporary cleanup worktree/branch only after the tagged clean checkout is proven.

Expected final state: Core Release complete, Proactivity Phase 2 complete in exact owner-approved scope, Architecture Program complete, persona evolution absent, old runtime/toolkit/bundle absent, and no superseded code/docs/specs in the released checkout or runtime.
