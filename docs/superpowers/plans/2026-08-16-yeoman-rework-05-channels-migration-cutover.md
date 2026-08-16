# Yeoman Rework Milestone 05: Channels, Migration, and Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship WhatsApp and Telegram parity, prove lossless non-widening migration and clean-release gates, then execute one quiesced cutover with an atomic forward-only commitment.

**Architecture:** Each channel has one exclusive native-session owner, durable Edge capture, canonical semantic mapping, and receipt-aware Delivery adapter behind common contract tests. Migration is offline through the signed sibling toolkit and target importer; cutover remains rollback-capable only until the first non-migration live Edge event and exact producer cutoff commit atomically with `target_committed`.

**Tech Stack:** TypeScript/Node 20/Baileys for WhatsApp, Python 3.14/python-telegram-bot for Telegram, SQLite/WAL, systemd, uv, npm, deterministic provider stubs, fault injection, clean-clone artifact builds, and authenticated migration/backup manifests.

## Global Constraints

- Read the orchestration file, normative §§1-4, 6, 8-16, 18-20, plus `m04-handoff.json`.
- The live old runtime remains authoritative until the exact `target_committed` transaction; after it the old release may never execute.
- Exactly one process owns each WhatsApp session or Telegram polling cursor at every point.
- Target rehearsal state is disconnected from live channels/effects; external calls use deterministic stubs unless exact authorized target Egress evidence is created.
- Two complete migrations and one blank-root restore must reconcile every source object with zero unexplained loss or widening.
- The final release is built non-editably from a clean clone and contains no legacy/compatibility path, superseded executable, dormant unsupported module, migration reader, or private activation value.
- WhatsApp and Telegram are the only channel implementations; unsupported modality is explicit.
- Every outbound mutation has canonical intent, stable ID, fresh authority, and receipt/failure/unknown evidence.
- `external_effect_unknown` is never automatically redispatched.
- Cutover, restore, generation activation, and sticky-fence clearance require action-bound local owner step-up.

---

## File map

- `packages/bridge/src/contracts.ts` — versioned WhatsApp native/capture/delivery/session wire types.
- `packages/bridge/src/spool.ts` — durable producer epoch/sequence and media staging.
- `packages/bridge/src/session.ts` — sole Baileys ownership and Secret Authority compare-and-swap.
- `packages/bridge/src/edge.ts` — capture-only native event production.
- `packages/bridge/src/delivery.ts` — stable client IDs and platform receipts.
- `packages/bridge/src/main.ts` — fixed bridge process entrypoint.
- `packages/gateway/yeoman_gateway/edge/whatsapp.py`, `edge/telegram.py` — native-to-canonical adapters.
- `packages/gateway/yeoman_gateway/delivery/whatsapp.py`, `delivery/telegram.py` — registered effect adapters.
- `packages/gateway/yeoman_gateway/edge/modality.py` — signed channel capability matrix.
- `packages/gateway/yeoman_gateway/egress/providers/openai_compatible.py` — required strict chat, embedding, vision/OCR, ASR, and TTS profile adapter only.
- `packages/workloads/yeoman_workloads/definitions/browser.toml`, `public_web.toml` — named required Capsule definitions.
- `packages/gateway/yeoman_gateway/app/operator.py` — extend the authenticated local operator entrypoint with migration/cutover/fence commands.
- `scripts/release/rehearse_migration.py`, `prove_release.py`, `prove_real_activation.py`, `cutover.py` — bounded operator orchestrators with canonical receipts.
- Toolkit worktree: `src/yeoman_migration_toolkit/readers/`, `normalize.py`, `edge_segment.py`, `reconcile.py`, and source-specific tests.
- `tests/channels/common/` — parameterized capture/audience/media/delivery/receipt/replay/reconnect/degradation suite.
- `tests/channels/whatsapp/`, `tests/channels/telegram/`, `tests/migration/`, `tests/cutover/`, `tests/release/`, and `tests/load/`.

## Task 1: Rewrite WhatsApp session ownership and durable Edge capture

**Files:**

- Create: `packages/bridge/src/contracts.ts`, `spool.ts`, `session.ts`, `edge.ts`, `main.ts`
- Create: `packages/gateway/yeoman_gateway/edge/whatsapp.py`
- Modify: `packages/bridge/package.json`, lockfile, TypeScript config, systemd bridge unit.
- Test: co-located protocol/spool/session/edge tests plus `tests/channels/whatsapp/test_capture.py`, `test_session.py`, `test_media.py`, `test_relationships.py`.

**Interfaces:**

- Consumes: fake/test platform-session capability, bridge generation, and native Baileys events; no real session activates in this task.
- Produces: durable `WhatsAppNativeEventV1`, producer cutoffs, and session compare-and-swap receipts; capture-only has no Delivery port.

- [ ] **Step 1: Write failing protocol and spool tests**

Cover account/session epoch, conversation/message/sender/recipient IDs, participant and group membership evidence, reply/quoted stanza, mentions, edits, deletes, reactions, documents/images/audio/voice, raw payload digest, media stage status, producer epoch/sequence, acknowledgement mode, and same-ID/different-digest conflict. Crash after every fsync/CAS boundary.

- [ ] **Step 2: Write exclusive-session tests**

Assert a second bridge cannot acquire the account lease/session generation; stale ratchet/session CAS cannot revive; reconnect continues the recorded account epoch or creates an explicit new epoch; authentication material never enters event payload/log/error/core; capture-only mode has no Delivery command capability.

- [ ] **Step 3: Run failing tests**

```bash
cd packages/bridge
npm test
cd ../..
uv run pytest tests/channels/whatsapp -q
```

Expected: FAIL because the target capture Bridge/adapter does not exist.

- [ ] **Step 4: Implement the smallest capture-only Bridge**

Bridge owns only Baileys session/capture translation in this commit. It uses authenticated IPC, Secret Authority named fake-session operations, durable spool, and typed size limits; it cannot access core/memory/Trust/provider config or reconstruct assistant semantics. Remove WebSocket/general server surfaces not required by the fixed local transport.

- [ ] **Step 5: Verify and commit**

```bash
cd packages/bridge && npm test && npm run build
cd ../.. && uv run pytest tests/channels/whatsapp tests/architecture -q
git add packages/bridge packages/gateway/yeoman_gateway/edge/whatsapp.py deploy/systemd tests/channels/whatsapp/test_capture.py tests/channels/whatsapp/test_session.py tests/channels/whatsapp/test_media.py tests/channels/whatsapp/test_relationships.py
git commit -m "feat(whatsapp): add durable exclusive capture"
```

## Task 2: Add receipt-safe WhatsApp Delivery transport

**Files:**

- Create: `packages/bridge/src/delivery.ts` and co-located tests.
- Create: `packages/gateway/yeoman_gateway/delivery/whatsapp.py`
- Test: `tests/channels/whatsapp/test_delivery.py`, `test_receipts.py`, `test_delivery_unknown.py`

**Interfaces:**

- Consumes: typed canonical Delivery commands with stable effect ID and fake/test session capability.
- Produces: exact Baileys request, platform message ID, acknowledgement/read evidence where available, failure, or explicit unknown.

- [ ] **Step 1: Write failing receipt and relationship tests**

Cover send/react/edit/delete, stable client ID, exact reply/quote/mention/thread target, documented same-key reconciliation while still dispatched, terminal unknown without automatic redispatch, reconnect, and no in-memory-only success claim.

- [ ] **Step 2: Implement the narrow Delivery port**

Accept only registered typed effects from Gateway Delivery; do not accept arbitrary Baileys payloads, recipients, session aliases, or raw commands. Keep capture and delivery capabilities independently fenceable.

- [ ] **Step 3: Verify and commit**

```bash
cd packages/bridge && npm test && npm run build
cd ../.. && uv run pytest tests/channels/whatsapp/test_delivery.py tests/channels/whatsapp/test_receipts.py tests/channels/whatsapp/test_delivery_unknown.py -q
git add packages/bridge/src/delivery.ts packages/bridge/src/delivery.test.ts packages/gateway/yeoman_gateway/delivery/whatsapp.py tests/channels/whatsapp/test_delivery.py tests/channels/whatsapp/test_receipts.py tests/channels/whatsapp/test_delivery_unknown.py
git commit -m "feat(whatsapp): add receipt-safe delivery"
```

## Task 3: Implement Telegram with the same canonical contract

**Files:**

- Create: `edge/telegram.py`, `delivery/telegram.py`
- Modify: service/activation/build manifests for the fixed Telegram account type.
- Test: `tests/channels/telegram/test_capture.py`, `test_polling_offset.py`, `test_relationships.py`, `test_media.py`, `test_delivery.py`, `test_reconnect.py`

**Interfaces:**

- Consumes: exact bot credential capability, last accepted update offset, native update, or typed Delivery command.
- Produces: canonical native record before offset advancement and authoritative Telegram effect evidence.

- [ ] **Step 1: Write failing offset and relationship tests**

Assert update/media durability precedes offset advance; restart/redelivery deduplicates; partial media blocks false capture acknowledgement; sender/chat/thread/reply/quote/mention/reaction/edit/delete relationships and reachable audience evidence survive; a second poller cannot own the token/cursor.

- [ ] **Step 2: Write common-contract parameterization**

Register WhatsApp and Telegram fixtures against the same capture, incomplete audience, media, delivery, receipt, replay, reconnect, degradation, and unknown-effect tests. Channel-specific capability differences must come from the signed modality matrix, never skipped assertions.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/channels/telegram tests/channels/common -q
```

Expected: FAIL on missing Telegram target adapters.

- [ ] **Step 4: Implement sole-poller Edge and Delivery**

Use the shared Edge spool/ingress and Delivery service. Resolve the bot credential per operation from Secret Authority; never store it globally. Preserve exact native update JSON as encrypted raw evidence, and explicitly mark unsupported read receipts or modalities.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/channels/telegram tests/channels/common -q
git add packages/gateway/yeoman_gateway/edge/telegram.py packages/gateway/yeoman_gateway/delivery/telegram.py tests/channels release
git commit -m "feat(telegram): add durable target channel transport"
```

## Task 4: Lock the first-release modality, provider, and tool allowlist

**Files:**

- Create: `edge/modality.py`, `egress/providers/openai_compatible.py`
- Create: fixed browser/public-web definition files.
- Modify: public build manifest and encrypted activation schema.
- Test: `tests/release/test_product_allowlist.py`, `tests/egress/test_openai_compatible_adapter.py`, `tests/egress/test_multimodal_profiles.py`, `tests/channels/common/test_modality_matrix.py`, `tests/capsule/test_shipped_definitions.py`

**Interfaces:**

- Consumes: owner-signed public type allowlist and private activation generation.
- Produces: only installed/activated WhatsApp, Telegram, approved provider profiles, reminders, browser, and public-web capabilities.

- [ ] **Step 1: Write failing absence and activation tests**

Assert Discord, Feishu, webhooks, public HTTP health/control/metrics, external telemetry, generic tools/providers/runbooks/workflows, arbitrary workloads, host execution, remote backup, anonymization, declassification, erase APIs, persona evolution, and proactivity are absent from source, dependency graph, schemas, tests, docs, build, entrypoints, and units. Assert an installed type remains inert unless present in encrypted activation generation.

- [ ] **Step 2: Write strict provider adapter tests**

Test exact registered endpoint/tenant/model/capabilities for chat, embedding, vision/OCR, ASR, and TTS; late credential resolution; per-attempt client state; cancellation; timeout; safe errors; governed request/result evidence; no caller headers/endpoints/model strings; no provider widening during fallback; and no SDK-global credentials. Unsupported profile/modality combinations fail before disclosure.

- [ ] **Step 3: Implement, verify, and commit**

```bash
uv run pytest tests/release/test_product_allowlist.py tests/egress/test_openai_compatible_adapter.py tests/egress/test_multimodal_profiles.py tests/channels/common/test_modality_matrix.py tests/capsule/test_shipped_definitions.py -q
git add packages/gateway/yeoman_gateway/edge/modality.py packages/gateway/yeoman_gateway/egress/providers/openai_compatible.py packages/workloads/yeoman_workloads/definitions release tests/release tests/egress tests/channels/common tests/capsule
git commit -m "feat(release): lock first release capabilities"
```

## Task 5: Complete and re-sign source-specific migration readers

**Files:**

- Create only in toolkit: readers for every store/file class discovered by the signed source manifest.
- Create only in toolkit: `normalize.py`, `edge_segment.py`, `reconcile.py` and tests.

**Interfaces:**

- Consumes: one authenticated preservation generation and newest lifecycle head.
- Produces: canonical target commands, one disposition per source object, source-native Edge segments, and a newly signed standalone toolkit artifact.

- [ ] **Step 1: Generate an exact reader coverage test from the protected manifest**

For each discovered database table/file/blob/JSON log/spool/persona/config/policy/workload source, require one explicit reader and classification. Unknown schema/object remains `legacy_unknown`; no wildcard “copy everything as trusted” reader is permitted.

- [ ] **Step 2: Write semantic migration tests**

Prove raw messages/media/relationships, identities/domains/audience evidence, memory versions/provenance/ACL/lifecycle, effects/receipts/unknowns, static persona history, proactivity history, generations, producer cutoffs, and key references map without widening. Old contact merges, shared scopes, sessions, grants, timers, approvals, desired-running, scheduler state, and leases remain inert evidence, never target authority.

- [ ] **Step 3: Write persona/proactivity disposition tests**

Preserve effective static persona generation, applied diffs/content, owner-shown proposals, source refs, actor/context/time, before/after hashes, and behavior-governing content as protected inert evidence. Classify routine scans, duplicate renderings, scratch/intermediates/caches, and unseen unused drafts without unique evidence as noncanonical. Map legacy proactive `sent` without authoritative platform receipt to `external_effect_unknown` and never replay it.

- [ ] **Step 4: Write source-segment and rollback-importer tests**

Capture WhatsApp and Telegram migration-time native events into the versioned source-native segment. Prove the old rollback importer consumes it idempotently in producer order with stable IDs/media, including reconnect, duplicate, partial media, cursor ambiguity, auth epoch change, and outbound unknown. Target compatibility is a separate target-branch commit in Task 6.

- [ ] **Step 5: Implement readers and commit only the toolkit branch**

```bash
cd /home/dm/Documents/yeoman-migration-toolkit
uv run pytest -q
uv run ruff check .
uv run mypy src/yeoman_migration_toolkit
git add src tests
git commit -m "feat(migration): normalize complete legacy state"
uv build
```

Generate a canonical artifact manifest for every new `dist/` file, sign it with the pinned offline Ed25519 program key, and verify the detached signature from the pinned public key in a separate process. Record a new toolkit build ID, commit, artifact/manifest/signature digests, exported command/Edge-segment schema versions, and invalidated rehearsal IDs in protected evidence plus a release-safe `artifacts/program/contracts/migration-toolkit-reference.json`. The M01 signature is historical and cannot authenticate this artifact.

Expected: the newly signed toolkit artifact has no runtime traffic or target-core write entrypoint; no rehearsal may use it until Task 6 passes.

## Task 6: Pin and verify the signed toolkit contract in target source

**Files:**

- Create: `artifacts/program/contracts/migration-toolkit-reference.json`
- Create: `tests/migration/test_toolkit_contract.py`, `test_edge_segment_compatibility.py`
- Modify only if the exported version changes: `packages/shared/yeoman_shared/contracts/migration.py`

**Interfaces:**

- Consumes: the newly signed Task 5 artifact/manifest and synthetic source-native Edge segments.
- Produces: target verification of exact signature/build/schema plus idempotent target-import compatibility; no toolkit code or private fixture enters the runtime artifact.

- [ ] **Step 1: Write failing signature/schema tests**

Pin public-key fingerprint, toolkit commit/build ID, artifact/manifest/signature digests, canonical target-command schema, and Edge-segment version. Reject M01 signature reuse, changed artifact, unknown record, version drift, source-specific type leakage, and any toolkit import/entrypoint/package data in target wheels.

- [ ] **Step 2: Prove target importer compatibility**

Feed synthetic WhatsApp/Telegram segments covering duplicate, conflict, reconnect, partial media, cursor ambiguity, auth epoch change, and outbound unknown. Assert target ingestion is idempotent, preserves producer order/stable IDs/media, and creates exact quarantine/effect dispositions.

- [ ] **Step 3: Verify and commit the target contract**

```bash
uv run pytest tests/migration/test_toolkit_contract.py tests/migration/test_edge_segment_compatibility.py tests/architecture -q
git add artifacts/program/contracts/migration-toolkit-reference.json tests/migration/test_toolkit_contract.py tests/migration/test_edge_segment_compatibility.py packages/shared/yeoman_shared/contracts/migration.py
git commit -m "test(migration): pin signed toolkit compatibility"
```

If the shared contract file is unchanged, omit it from `git add`; the commit must contain only files actually changed.

## Task 7: Codify full migration reconciliation

**Files:**

- Create: `scripts/release/rehearse_migration.py`
- Test: `tests/migration/test_rehearsal_orchestrator.py`, `test_full_reconciliation.py`, `test_non_widening.py`

**Interfaces:**

- Consumes in this task: deterministic signed snapshot/toolkit/target fixtures and fresh disposable test roots.
- Produces in this task: one committed bounded rehearsal/reconciliation driver; Task 11 later produces the two real manifests and blank-root proof against the exact final source commit.

- [ ] **Step 1: Write failing rehearsal-orchestrator tests**

Assert it refuses live roots, writable source snapshot, live channel credentials, unfenced effects, wrong toolkit/release digest, stale lifecycle head, reused target root, or missing source object disposition. It uses local provider/channel stubs and never changes live configuration/memory.

- [ ] **Step 2: Run the failing orchestrator tests**

```bash
uv run pytest tests/migration/test_rehearsal_orchestrator.py tests/migration/test_full_reconciliation.py tests/migration/test_non_widening.py -q
```

Expected: FAIL because the bounded rehearsal orchestrator does not exist.

- [ ] **Step 3: Implement, verify, and commit the rehearsal boundary**

Implement only manifest-pinned snapshot, toolkit, target-import, projection-rebuild, reconciliation, blank-root restore, and protected-receipt operations. Refuse live roots and caller-provided shell/actions. Test with deterministic disposable fixtures, then commit the source boundary before using real snapshots.

```bash
uv run pytest tests/migration/test_rehearsal_orchestrator.py tests/migration/test_full_reconciliation.py tests/migration/test_non_widening.py -q
git add scripts/release/rehearse_migration.py tests/migration/test_rehearsal_orchestrator.py tests/migration/test_full_reconciliation.py tests/migration/test_non_widening.py
git commit -m "test(migration): codify full-state reconciliation"
```

- [ ] **Step 4: Freeze this task as tooling only**

Require a clean worktree and record this exact task commit in protected program evidence. Do not use real snapshots, quiesce live writers, sign a release, or claim a migration rehearsal here. Any later change to toolkit, importer, schema, target source, manifest, or this driver means Task 11 must use the new exact commit for both rehearsals.

## Task 8: Codify the clean public release proof

**Files:**

- Create: `scripts/release/prove_release.py`
- Modify: `release/build-manifest.toml`
- Create: `tests/release/test_prove_release.py`, `test_clean_install.py`, `test_forbidden_residuals.py`
- Create: `tests/load/test_release_soak.py`, `tests/fault/test_release_fault_matrix.py`, `tests/security/test_release_information_flow.py`
- Create: `docs/operations/release-proof.md`

**Interfaces:**

- Consumes in this task: clean source fixtures, locked fake inputs, manifest schemas, deployed-unit harnesses, and test activation fixtures.
- Produces in this task: one committed deterministic public-release proof boundary; Task 11 later runs it against the complete Task 10 commit and produces the signed Core Release candidate.

- [ ] **Step 1: Write failing release-orchestrator contract tests**

Require one exact clean source commit, locked dependency inputs, exhaustive manifest/residual verification, disposable installation, deployed-unit information-flow and fault matrices, bounded load evidence, output commitments, and refusal to sign when the tree is dirty or any required gate is missing/stale. The orchestrator may invoke only fixed typed checks; it cannot weaken a manifest, skip full test collection, accept caller shell, or read private activation plaintext.

- [ ] **Step 2: Run the failing orchestrator tests**

```bash
uv run pytest tests/release/test_prove_release.py tests/release/test_clean_install.py tests/release/test_forbidden_residuals.py tests/load/test_release_soak.py tests/fault/test_release_fault_matrix.py tests/security/test_release_information_flow.py -q
```

Expected: FAIL because the release-proof orchestrator and fixtures do not exist.

- [ ] **Step 3: Implement, verify, and commit the complete proof boundary**

Implement deterministic fixed subcommands for source/build/install/residual/isolation/fault/load evidence and document the exact operator flow. Test fixtures use fake credentials, channels, providers, clocks, and bounded load; the later real execution records protected evidence outside Git.

```bash
uv run pytest tests/release tests/load tests/fault tests/security -q
git add scripts/release/prove_release.py release/build-manifest.toml tests/release tests/load tests/fault tests/security docs/operations/release-proof.md
git commit -m "test(release): codify clean target release proof"
```

- [ ] **Step 4: Freeze this task as tooling only**

Require a clean worktree and record this exact task commit in protected program evidence. Do not sign a release, install, activate, import real secrets, or begin a load/observation window here. Task 11 runs the committed proof only after the rollback/cutover source boundary is also committed, so the signed candidate contains every release and operator path.

## Task 9: Codify inert real protected-state recovery

**Files:**

- Create: `scripts/release/prove_real_activation.py`
- Modify: `release/build-manifest.toml`
- Modify outside Git: encrypted activation generation and Secret Authority store only through typed operator APIs.
- Create outside Git: protected import, encrypted-secret backup, disposable-recovery, and fence receipts.
- Test: `tests/cutover/test_inert_secret_activation.py`, `test_real_secret_backup.py`, `test_real_secret_recovery.py`

**Interfaces:**

- Consumes in this task: signed migration-inventory schema, accepted Milestone 04 recovery interfaces, fake protected aliases/key references, two disposable recovery-key fixtures, and test owner assertions.
- Produces in this task: a committed deterministic driver/test boundary only; Task 11 later produces the inert Secret Authority generation and encrypted backup/recovery evidence without issuing Edge/Egress/Delivery capability.

- [ ] **Step 1: Write failing inert-activation tests**

Require exact aliases/references, source/target key generations, release/activation/toolkit digests, owner action digest, and all downstream fences. Reject plaintext bulk import, enumeration, unknown alias, stale generation, missing migration receipt, live channel/provider access, or any capability issuance.

- [ ] **Step 2: Run the failing protected-activation driver tests**

```bash
uv run pytest tests/cutover/test_inert_secret_activation.py tests/cutover/test_real_secret_backup.py tests/cutover/test_real_secret_recovery.py -q
```

Expected: FAIL because the bounded real-activation proof driver does not exist.

- [ ] **Step 3: Implement, verify, and commit the inert driver boundary**

The driver accepts only manifest-pinned aliases/references, typed Secret Authority/backup/recovery operations, two separately identified recovery-key inputs, expected generations, fixed fence predicates, and an action-bound owner assertion. It exposes a deterministic fixture/dry-run mode for tests, never prints protected values, never opens network/channel/provider capability, and cannot continue after an unknown receipt.

```bash
uv run pytest tests/cutover/test_inert_secret_activation.py tests/cutover/test_real_secret_backup.py tests/cutover/test_real_secret_recovery.py -q
git add scripts/release/prove_real_activation.py release/build-manifest.toml tests/cutover/test_inert_secret_activation.py tests/cutover/test_real_secret_backup.py tests/cutover/test_real_secret_recovery.py
git commit -m "test(release): codify inert protected-state recovery"
```

- [ ] **Step 4: Freeze this task as tooling only**

Require a clean worktree and record this exact task commit in protected program evidence. Do not import real protected state or mutate activation here. Task 11 runs this committed driver after Task 10 finalizes rollback/cutover source and Task 11 signs the complete public release commit.

## Task 10: Rehearse and prove pre-marker rollback

**Files:**

- Create: `scripts/release/cutover.py`
- Modify: `release/build-manifest.toml`
- Test: `tests/cutover/test_handoff_receipt.py`, `test_capture_only.py`, `test_pre_marker_rollback.py`, `test_quiet_channel.py`

**Interfaces:**

- Consumes: exact digest-pinned non-live target build, signed toolkit, verified rehearsal backup, exact fake/test channel handoff receipts, and disposable owner-step-up fixtures.
- Produces before commitment: capture-only target state plus authenticated ability to roll back through source-native Edge replay.

- [ ] **Step 1: Write the handoff receipt tests**

Require platform/account epoch, old process/build stop proof, last old cursor/producer sequence/core acknowledgement, target Edge process/build, first target producer epoch/sequence, auth/key generation, media cutoff, and acknowledgement mode. Reject concurrent owner, unknown cursor, partial media, unclosed segment, or generation mismatch.

- [ ] **Step 2: Write pre-marker rollback tests**

Stop target Edge, close/authenticate its segment, use signed toolkit to import/re-emit events/media into untouched old release in original order, verify old canonical acceptance, reconcile all pending/unknown old and migration-time effects, and only then reopen old Delivery/session/poller from reconciled cursor. Synthetic/migration records can never satisfy commitment.

- [ ] **Step 3: Write quiet-channel behavior test**

After core ingest reconciliation, no non-migration live event means the system remains fenced at the commitment gate indefinitely. The owner may create an ordinary live channel interaction; no timer, local CLI injection, migration item, health probe, or synthetic event advances it.

- [ ] **Step 4: Run the failing cutover tests**

```bash
uv run pytest tests/cutover -q
```

Expected: FAIL because the bounded cutover orchestrator does not exist.

- [ ] **Step 5: Implement, verify, and commit cutover tooling**

Implement only fixed handoff/capture/reconcile/marker/fence operations over exact typed manifests. No caller shell, arbitrary unit/path, synthetic commitment event, or post-marker old-runtime operation exists.

```bash
uv run pytest tests/cutover -q
git add scripts/release/cutover.py release/build-manifest.toml tests/cutover/test_handoff_receipt.py tests/cutover/test_capture_only.py tests/cutover/test_pre_marker_rollback.py tests/cutover/test_quiet_channel.py
git commit -m "feat(release): enforce forward-only cutover boundary"
```

- [ ] **Step 6: Run the full disposable rollback rehearsal**

```bash
uv run python scripts/release/cutover.py rehearse --release-manifest dist/build-manifest.json --target-state disposable-cutover-root
```

Expected: pre-marker rollback restores the old rehearsal environment with every segment/effect reconciled.

- [ ] **Step 7: Close the rollback rehearsal without signing the release**

Record the exact committed cutover-tool digest and protected dry-rehearsal receipt. Do not sign/install/activate the release or obtain cutover authorization yet. Any correction creates a new source commit; Task 11 proves the final clean commit afterward.

## Task 11: Freeze, prove, recover, and authorize the complete release

**Files:**

- Modify outside Git only: signed public build, encrypted activation generation, Secret Authority store, protected backups/recovery roots, proof receipts, and owner authorization.
- No source, test, manifest, or operator-document changes are allowed in this task.

**Interfaces:**

- Consumes: exact clean Task 10 commit, signed toolkit, two independently closed old snapshots, committed rehearsal/release/activation/rollback proof drivers, real protected aliases/references, and two independent recovery-key copies.
- Produces: two final reconciliation manifests plus blank-root proof, final signed public release, real inert activation/backup/recovery proof, and current pre-cutover reviewer GOs/owner authorization.

- [ ] **Step 1: Run rehearsal one against the exact final commit**

From a clean clone first build/verify the exact Task 10 commit and create a separately labeled program-signed `migration-rehearsal-only` artifact whose activation keeps every live/effect capability fenced; it is not the final Core Release signature and cannot install into the live target root. Obtain an action-bound local owner authorization naming the exact source generation, writers to quiesce, snapshot/target roots, current toolkit digest, exact Task 10 commit/rehearsal-build digest, operations, and expiry. Quiesce only those writers, take the authenticated backup, run the committed rehearsal driver/toolkit into a fresh non-live target root, rebuild projections, and reconcile counts/sizes/digests/relationships/ACLs/domains/classifications/lifecycle/effects/persona/proactivity/generations/cutoffs/keys. Commit no snapshot-derived data and never hand-edit migrated state.

- [ ] **Step 2: Run independent rehearsal two and blank-root restore**

Use a later independently closed snapshot, new action-bound authorization, new roots, and the same exact Task 10/toolkit digests; no authority or state carries over. Repeat complete reconciliation, then restore its coordinated backup to a blank disposable root using only the signed non-live target artifact, documented inputs, newest lifecycle head, and a test recovery-key copy. All effects stay disabled.

- [ ] **Step 3: Close zero-divergence reconciliation evidence**

Every source object must be preserved, normalized, deduplicated, quarantined, rebuildable/excluded, or an explicit unrecoverable gap. Any unexplained loss, widening, cross-domain collapse, missing blob, materializable unknown, cutoff mismatch, or unknown effect without disposition blocks release. Close two protected manifests plus blank-root receipt and bind each to the exact Task 10 commit/toolkit. Any subsequent source/test/manifest/toolkit change invalidates all three and restarts Task 11 at Step 1.

- [ ] **Step 4: Prove the complete clean non-editable release**

From a fresh clone of the exact Task 10 commit run `uv sync --frozen`, full pytest collection, Ruff, strict mypy, all-package build, Bridge `npm ci/test/build`, disposable install, and `scripts/release/prove_release.py`. Verify exhaustive source/dependency/import/unit/route/profile/tool/schema/config/migration/doc/package-data allowlists and forbidden residuals. Execute deployed-unit cross-domain/sentinel/Capsule/network/browser/taint/redaction/backup/lifecycle/admin-step-up gates, the full crash/fault matrix, expected-load soak for 24 hours, and twice measured peak for at least two hours. Any source correction returns to the owning earlier task, creates a new commit, and restarts this task.

- [ ] **Step 5: Sign the exact public release commit and build**

Assert the clone is clean and every gate output names the same commit/build. The owner signs that commit's exhaustive build manifest, channel/modality/provider/tool/workload allowlist, and static persona digest. No private activation value enters the public artifact. Any later source/build change invalidates this signature and all following steps.

- [ ] **Step 6: Import the exact real protected state while fully fenced**

Run the committed `prove_real_activation.py` using one short-lived action-bound owner assertion naming every alias/reference, expected prior/new generation, release/toolkit/migration digests, and operation. Import through typed Secret Authority APIs while Edge, Egress, Delivery, workloads, providers, and channel paths remain fenced. Reconcile the protected import receipt; no capability is issued.

- [ ] **Step 7: Back up and recover with two independent real key copies**

Create an authenticated coordinated target generation containing the actual inert encrypted secret store, wrapped objects, lifecycle head, and key-escrow references. Restore it to a first blank disposable root with the first recovery-key copy, prove exact alias/key/session generations and zero issued capabilities, close evidence, and destroy that root. Repeat independently in a new root with the separately stored second copy; prove neither copy is co-located with the other or the only ciphertext.

- [ ] **Step 8: Re-sign real activation and rerun every invalidated gate**

Create an owner-signed encrypted activation generation/digest containing only inert real aliases/references and current key generations. Rerun strict activation, Secret Authority, both backup/restores, sentinel/redaction, fence/startup, public/private manifest separation, and any load/capacity gate affected by encrypted state. Public build digest remains fixed. Add only safe commitments to pending handoff evidence; aliases, ciphertext equality hashes, and recovery material remain protected.

- [ ] **Step 9: Obtain pre-cutover GOs and owner authorization**

Send both reviewers the exact source/build/activation digests, two reconciliation manifests, blank-root proof, both real recovery commitments, rollback rehearsal, static/load/fault/isolation evidence, handoff procedure, fence state, and proposed action digest. Require explicit architecture/security GOs bound to all exact digests. Then obtain a fresh local owner step-up bound to release, activation, backup, toolkit, accounts, cutoffs, target root, and `target_committed`. Any changed digest, expired assertion, or source edit restarts the applicable proof.

## Task 12: Execute the quiesced production cutover

**Files:**

- Create outside Git: final backup, migration, channel handoff, fence, marker, and opening receipts.
- Modify outside Git: systemd fixed-unit activation and encrypted activation generation only.
- Never modify: old source/state after the final authenticated snapshot except the explicitly defined pre-marker rollback path.

- [ ] **Step 1: Install sticky cutover fences and quiesce every old writer**

Verify Task 11 Step 9 reviewer GOs and owner assertion are current, then stop old Overseer, Gateway, cron, extractors, backfills, Bridge consumers/pollers, and every writer. Prove PIDs/builds are gone. Keep target external delivery/effects fenced.

- [ ] **Step 2: Transfer exclusive channel ownership in capture-only mode**

After the old owners are proven gone, use a fresh pre-reviewed action-bound assertion to compare-and-swap the inert Secret Authority to the exact final WhatsApp session generation and Telegram cursor/token capability from the quiesced source delta; reconcile the receipt before process start. For WhatsApp, start target with that approved session generation, durably spool/stage/hash/fsync before controllable acknowledgement, continue account epoch and create target producer epoch. For Telegram, start the sole poller at the last accepted offset, spool before offset advance, and record target producer epoch. Any activation mismatch leaves both channels fenced and triggers pre-marker recovery, never a second owner.

- [ ] **Step 3: Close final old backup and migrate/reconcile delta**

Record exact producer cutoffs, migrate into target, boot fully fenced, and verify release/config/policy/schema/key/control generations, database/blob integrity, lifecycle head, and effect state.

- [ ] **Step 4: Open ingest only and reconcile live capture**

Open `CORE_INGEST`, replay/deduplicate migration-tagged segments, and reconcile cutoffs while `PROCESS`, `EGRESS`, `DELIVER`, timers, reminders, workloads, and semantic/admin mutations remain fenced.

- [ ] **Step 5: Commit the first ordinary live event and marker atomically**

Wait for the first non-migration live Edge event and durably hold it without processing. Obtain or refresh a local owner step-up bound to the exact release/activation/account/producer epoch, that event digest, its proposed cutoff, and the one-way commitment action; a quiet channel or expired assertion remains at the gate. In one `core.db` transaction commit that Interaction, its exact producer cutoff, and unique `target_committed` generation marker. Confirm transaction durability and authenticated receipt before proceeding.

- [ ] **Step 6: Destroy rollback authority and open capabilities in order**

After marker success, permanently remove pre-commit rollback authority; the old release may never execute. Open `PROCESS`, then scoped `EGRESS` after proof, then scoped `DELIVER` after proof. Start Overseer last and prove it observes active generations without rewriting them. Any failure now uses forward recovery only.

## Task 13: Close Milestone 05

**Files:**

- Create: `artifacts/program/handoffs/m05-handoff.json`
- Modify: orchestration state to post-cutover stabilization only after review.

- [ ] **Step 1: Run immediate post-cutover proof**

Verify both channels capture/process/deliver within signed scope, exact reply/mention/quote traceability, provider/capsule routes, reminder, receipt/unknown handling, restart/reconnect/replay, current backup, status freshness, and no old PIDs/units/imports/writers. Do not generate synthetic sends solely for health.

- [ ] **Step 2: Obtain reviewer GOs**

Architecture review verifies exclusive ownership, reconciliation, artifact completeness, and forward-only marker. Security review verifies activation/step-up, secrets/sessions, non-widening migration, unknown effects, fences, and post-marker old-runtime impossibility. Any blocker keeps the program in fenced forward recovery.

- [ ] **Step 3: Commit sanitized receipt and advance**

```bash
git add artifacts/program/handoffs/m05-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md
git commit -m "docs(architecture): record target cutover commitment"
```

Expected: state is `CORE_STABILIZATION_IN_PROGRESS`, rollback authority is `FORWARD_ONLY`, and Milestone 06 begins in a fresh task.
