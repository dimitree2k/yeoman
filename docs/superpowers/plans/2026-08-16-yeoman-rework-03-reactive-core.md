# Yeoman Rework Milestone 03: Reactive Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a channel-neutral, trace-complete reactive turn from durable native event through isolated memory and controlled model use to canonical effect intent and authoritative receipt.

**Architecture:** Edge durably spools versioned native events and submits them through a bounded ingress port. A small Turn Kernel coordinates Trust, Interaction, Memory, Controlled Egress, Actions, and Delivery through typed ports; module-owned repositories share atomic `core.db` transactions only where causal facts must commit together.

**Tech Stack:** Python 3.14, asyncio, SQLite/WAL, encrypted blobs, Unix-socket IPC schemas, Pydantic 2, strict provider adapters over HTTP clients, SQLite FTS projection, pytest/property/fault tests, Ruff, and mypy strict mode.

## Global Constraints

- Read the orchestration file, normative §§3-11, 12.3, 13, 14, 16, and 19, plus `m02-handoff.json`.
- No live WhatsApp, Telegram, model, tool, or delivery endpoint is enabled; this milestone uses deterministic fake adapters.
- No production provider credential, protected external disclosure, channel effect, persistent workload, or real Secret/Key state activates before Milestone 04 recovery/control gates pass.
- Inbound interaction metadata and raw blob references commit before turn processing.
- Outbound intent commits before transport dispatch.
- Reply, quote, mention, thread, edit, delete, reaction, sender, recipients, native IDs, account epoch, and audience evidence remain typed canonical relationships.
- Model/tool output is tainted data and cannot mint authority, persist memory, or dispatch an effect.
- Every retry/fallback is a distinct attempt; `external_effect_unknown` is terminal for automatic redispatch.
- Memory visibility is independent of semantic type and is domain-isolated by default.
- Projection results are candidates only and are reauthorized before decryption/materialization.
- Controlled Egress is the only Gateway model/media-provider credential path.
- Edge ingress, Turn processing, Controlled Egress, Actions, and Delivery enforce the Milestone 02 capability/fence evaluator directly; Milestone 04 adds Overseer fence issuance/reconciliation, not first enforcement.

---

## File map

- `edge/contracts.py` — canonical native envelope, producer epoch/sequence, capture acknowledgement.
- `edge/spool.py` — durable per-account at-least-once segments and staged media references.
- `edge/ingress.py` — authenticated bounded conversion into Interaction commands.
- `interaction/models.py`, `schema.py`, `repository.py`, `service.py` — immutable communication graph and processing work.
- `memory/models.py`, `schema.py`, `repository.py` — layered versioned memory and provenance.
- `memory/capture.py` — attempt-first extraction and atomic version commit.
- `memory/recall.py`, `projection.py` — candidate/revalidation/materialization flow.
- `memory/suppression.py` — owner-only truthful suppression and lifecycle-head commit.
- `app/operator.py` — extend the local authenticated operator surface with exact-object suppression and protected evidence inspection commands.
- `egress/models.py`, `profiles.py`, `classification.py`, `transforms.py`, `service.py` — strict route profiles, one typed pre-route transform seam, and governed attempts.
- `egress/providers/base.py`, `egress/providers/fake.py` — registered bounded provider port and deterministic test implementation.
- `actions/models.py`, `schema.py`, `repository.py`, `coordinator.py` — semantic effects and single retry ownership.
- `delivery/models.py`, `schema.py`, `repository.py`, `service.py` — channel effects, stable IDs, attempts, receipts, and unknowns.
- `turn/ports.py`, `turn/materialize.py`, `turn/kernel.py` — bounded reactive orchestration.
- `evidence/inspection.py` — freshly authorized protected causal graph inspection.
- `status/snapshot.py` — redacted core status facts only.
- `tests/edge/`, `tests/interaction/`, `tests/memory/`, `tests/egress/`, `tests/actions/`, `tests/delivery/`, `tests/turn/`, and `tests/integration/test_reactive_trace.py`.

## Task 1: Durably capture native envelopes in the Edge spool

**Files:**

- Create: `edge/contracts.py`, `edge/spool.py`, `edge/ingress.py`
- Modify: `contracts/ipc.py`
- Test: `tests/edge/test_spool.py`, `test_ingress.py`, `test_media_cutoff.py`, `test_dedup_conflict.py`

**Interfaces:**

- Consumes: `NativeEventV1` with platform/account epoch/native IDs, original bytes, native relationships, and media handles.
- Produces: `CapturedEvent(producer_epoch, sequence, source_uuid, envelope_blob, staged_media, durability)` and an `InteractionIngressPort` protocol; tests use a bounded fake until Task 2 implements the port and core acceptance acknowledgement.

- [ ] **Step 1: Write failing spool durability tests**

Inject failure before/after envelope fsync, media stage/hash/fsync, segment fsync, and producer-sequence persistence. Assert the upstream acknowledgement or polling offset can advance only after the platform-specific durable boundary. Assert same key+digest is idempotent and same key+different digest retains both, quarantines processing, and alarms.

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/edge -q
```

Expected: FAIL on missing Edge package.

- [ ] **Step 3: Implement bounded capture and authenticated ingress**

Persist source UUID once when no native stable ID exists. Include platform, channel account/tenant, account epoch, conversation, native event ID, event kind, producer epoch, and sequence in the canonical key. The ingress port enforces role, version, size/depth/concurrency, allowed record type, idempotency key, sequence continuity, conflict behavior, and current `EDGE_CAPTURE`/`CORE_INGEST` permission; it accepts no SQL, paths, Trust decisions, or commands.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/edge -q
git add packages/gateway/yeoman_gateway/edge packages/shared/yeoman_shared/contracts/ipc.py tests/edge
git commit -m "feat(edge): durably spool native events"
```

## Task 2: Persist the complete Interaction graph before processing

**Files:**

- Create: `interaction/models.py`, `schema.py`, `repository.py`, `service.py`
- Test: `tests/interaction/test_capture.py`, `test_relationships.py`, `test_processing_queue.py`, `test_audience_versions.py`

**Interfaces:**

- Consumes: accepted Edge record plus exact Trust identity/domain/membership resolutions.
- Produces: immutable `InteractionId`, typed relationship edges, raw blob references, evidence reference, and a transactional processing work item.

- [ ] **Step 1: Write failing relationship tests**

Use a fixture containing sender, intended recipients, conservatively reachable audience, direct mention, quoted message, reply target, thread root, edit predecessor, reaction target, document, image, and audio. Assert every native relationship is stored and queryable without reconstructing it from text. Assert two groups with identical participants remain different domains.

- [ ] **Step 2: Write capture atomicity tests**

Crash at each insert and assert either the interaction/raw references/evidence/work item all exist or none exist. Processing cannot dequeue before commit. Replayed accepted Edge records do not duplicate the interaction.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/interaction -q
```

Expected: FAIL on missing Interaction package.

- [ ] **Step 4: Implement module-owned schema and service**

Store opaque object references rather than protected content in graph tables. Implement the Task 1 `InteractionIngressPort` with exact bounded commands. Reference the exact identity, domain, audience, membership, release, config, policy, and schema generations used. Commit causal evidence with the interaction and acknowledge Edge only after the transaction succeeds.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/interaction tests/edge -q
git add packages/gateway/yeoman_gateway/interaction tests/interaction
git commit -m "feat(interaction): preserve canonical message relationships"
```

## Task 3: Implement layered memory capture, versioning, and suppression

**Files:**

- Create: `memory/models.py`, `schema.py`, `repository.py`, `capture.py`, `suppression.py`
- Modify: `app/operator.py`
- Test: `tests/memory/test_layers.py`, `test_capture_atomicity.py`, `test_versions.py`, `test_suppression.py`, `test_lifecycle_restore.py`

**Interfaces:**

- Consumes: authorized source object manifest and a typed `MemoryExtractionPort`; tests use a deterministic fake until Controlled Egress implements the port in Task 5.
- Produces: versioned `MemoryObject` metadata plus encrypted content, exact provenance, visibility, classification, grants, and lifecycle axes.

- [ ] **Step 1: Write failing independent-axis tests**

Cover lifetime values `working`, `session`, `situation_workload`, `durable`, `distilled`; meaning values `episodic`, `semantic`, `procedural`, `emotional`, `reflective`; origin-domain/owner-private/explicit-grant visibility; eligible/suppressed recall; and available/quarantined/erasure-pending/erased content. Assert meaning never changes visibility.

- [ ] **Step 2: Write extraction transaction tests**

Assert the extractor commits an attempt intent, calls the injected `MemoryExtractionPort` outside the database transaction, durably stores the result blob, then atomically commits version/source/transform/ACL/classification/grant/current/projection/attempt facts. Missing blob, invalid key, pending, quarantined, erasure-pending, and erased objects cannot become current or indexable.

- [ ] **Step 3: Write owner-only suppression tests**

Assert suppression requires verified owner-private/operator context, exact object/domain manifest, action-bound step-up, canonical receipt, and newest lifecycle journal durability on every approved local medium. It removes recall/projection/background/model/tool/workload materialization but retains raw and protected canonical content. Non-owner requests create review requests only.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/memory/test_layers.py tests/memory/test_capture_atomicity.py tests/memory/test_versions.py tests/memory/test_suppression.py tests/memory/test_lifecycle_restore.py -q
```

Expected: FAIL on missing memory implementation.

- [ ] **Step 5: Implement the smallest versioned memory model**

Corrections always create `corrects` or `supersedes` versions. Working memory is a bounded materialization, never an independent source. Every durable/distilled object retains all contributing sources and the highest classification, audience intersection, source domains, and grant conjunction.

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/memory -q
git add packages/gateway/yeoman_gateway/memory packages/gateway/yeoman_gateway/app/operator.py tests/memory
git commit -m "feat(memory): add isolated layered memory versions"
```

## Task 4: Implement candidate-only projection and authorized recall

**Files:**

- Create: `memory/projection.py`, `memory/recall.py`
- Test: `tests/memory/test_projection.py`, `test_recall_authorization.py`, `test_owner_recall.py`, `test_projection_failure.py`

**Interfaces:**

- Consumes: bounded `RecallRequest(envelope, query_object, limit, purpose)`.
- Produces: authorized `MaterializedMemory` objects or explicit no-recall degradation; projection hints never authorize.

- [ ] **Step 1: Write failing cross-domain and failure tests**

Assert same-person/different-group candidates are rejected, owner cross-domain recall works only in owner-private destination, shared response rematerializes under shared rules, stale grants/membership reject, hidden trace existence is not leaked, projection failure selects bounded canonical fallback only when explicitly authorized, and unbounded database scan is impossible.

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/memory/test_projection.py tests/memory/test_recall_authorization.py tests/memory/test_owner_recall.py tests/memory/test_projection_failure.py -q
```

Expected: FAIL on missing recall/projection modules.

- [ ] **Step 3: Implement transactional outbox and revalidation**

Commit projection work with canonical memory changes. Projection stores encrypted candidate indexes and opaque IDs/non-authoritative hints. Recall caps candidates before canonical lookup, revalidates every object with current Trust, applies lifecycle before key access, decrypts only admitted objects, and records the exact materialization manifest.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/memory -q
git add packages/gateway/yeoman_gateway/memory/projection.py packages/gateway/yeoman_gateway/memory/recall.py tests/memory
git commit -m "feat(memory): authorize projection-backed recall"
```

## Task 5: Implement Action intents and strict Controlled Egress attempts

**Files:**

- Create: `actions/models.py`, `schema.py`, `repository.py`, `coordinator.py`
- Create: `egress/models.py`, `profiles.py`, `classification.py`, `transforms.py`, `service.py`
- Create: `egress/providers/base.py`, `egress/providers/fake.py`
- Test: `tests/actions/test_intents.py`, `test_retry_ownership.py`
- Test: `tests/egress/test_classification.py`, `test_profiles.py`, `test_transform_seam.py`, `test_attempt_flow.py`, `test_credentials.py`, `test_fallback.py`, `test_output_taint.py`

**Interfaces:**

- Consumes: authorized semantic action request, exact object manifest, and immutable route-profile preference.
- Produces: committed `ActionIntent` followed by `ProviderAttemptResult` with governed disclosure evidence, route/generation/usage/receipt, and tainted output object.

- [ ] **Step 1: Write failing Action intent and retry-owner tests**

Assert the semantic purpose, initiating envelope, source selectors, capability, constraints, budget, deadline, disclosure/use limits, and state are canonical before any provider is chosen. Exactly one coordinator owns retry/reconciliation; provider attempts never overwrite their parent intent or one another.

- [ ] **Step 2: Write failing handling/profile tests**

Cover `public`, `protected`, `restricted`, and `host_only`; unknown becomes `restricted+unclassified`; hard-deny credential/platform-auth/key/recovery/session-ratchet tags have no external route. Profiles declare exact processor/provider tenant/capability/modalities/destination/handling/transforms/posture/credential/budget/retry/trace. Caller endpoints, headers, aliases, and arbitrary model strings are rejected.

The transform-seam test fixes the order `authorized materialization -> classification -> registered transform -> distinct declassification decision -> route resolution`. First release registers only required deterministic normalization transforms; anonymization and automatic declassification are absent. A separately authorized declassification decision may change only handling class/tags and route eligibility; it cannot add a source domain, widen audience, create a grant, or make owner-private content speakable in a shared destination.

- [ ] **Step 3: Write the eight-stage attempt-flow test**

Assert intent precedes materialization; complete payload classification includes system/persona/history/memory/attachments/tool schemas; profile decision and exact manifest precede credential resolution; provider runs outside transactions; result/failure/cancel/unknown commits afterward; every fallback rematerializes and creates a distinct attempt; output is tainted.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/actions tests/egress -q
```

Expected: FAIL on missing Action/Egress packages.

- [ ] **Step 5: Implement Action ownership, registered profiles, and fake provider**

Commit the provider-neutral `ActionIntent` first. Implement the Task 3 `MemoryExtractionPort` through the same governed attempt service and add an integration test proving memory extraction cannot bypass classification/profile/evidence. Resolve one test credential alias at the last possible point through the Secret Authority client only after `EGRESS(route)` permission passes. Never store it in environment globals, SDK globals, envelopes, rows, blobs, prompts, logs, errors, or traces. Record ordered source/transform manifest, semantic request, deterministic serializer identity, credential-stripped wire body only when required, exact bounded normalized result, processor request ID, usage, cost, and protection labels.

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/actions tests/egress tests/secrets -q
git add packages/gateway/yeoman_gateway/actions packages/gateway/yeoman_gateway/egress tests/actions tests/egress
git commit -m "feat(actions): govern strict provider attempts"
```

## Task 6: Implement receipt-safe Delivery

**Files:**

- Create: `delivery/models.py`, `schema.py`, `repository.py`, `service.py`
- Test: `tests/delivery/test_intent_before_send.py`, `test_relationship_target.py`, `test_receipts.py`, `test_unknown.py`, `test_revalidation.py`

**Interfaces:**

- Consumes: authorized semantic `ActionIntent` and registered fake channel adapter.
- Produces: deterministic stable effect ID, durable dispatch attempt, platform ID/receipt/failure/unknown, and canonical evidence.

- [ ] **Step 1: Write failing effect-state tests**

Cover `not_dispatched`, `dispatched`, `succeeded`, `failed_permanent`, `cancelled_or_expired`, and `external_effect_unknown`; Trust denial remains separate. Assert exactly one retry owner, restart preserves state, same-key resubmission is allowed only while dispatched and platform-documented, terminal unknown never automatically redispatches, and owner disposition is explicit.

- [ ] **Step 2: Write exact reply-target tests**

Authorize a reply/quote/mention/thread target, insert newer ambient messages, and assert delivery uses the committed relationship. Immediately before send, change audience, membership, identity, grant, classification, generation, or fence and assert denial with no adapter call.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/delivery -q
```

Expected: FAIL on missing Delivery module.

- [ ] **Step 4: Implement central effect ownership**

Action owns consequential semantic effects from Task 5; Delivery owns channel effects. Require current `PROCESS` and scoped `DELIVER` permission, commit Delivery intent and stable ID before adapter invocation, call outside transactions, then commit exact typed request, adapter/build generation, platform ID, receipts/read evidence, safe failure, or unknown. Raw provider/platform errors remain protected blobs and never assistant content.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/actions tests/delivery -q
git add packages/gateway/yeoman_gateway/delivery tests/delivery
git commit -m "feat(delivery): persist receipt-safe channel effects"
```

## Task 7: Compose the bounded Turn Kernel

**Files:**

- Create: `turn/ports.py`, `turn/materialize.py`, `turn/kernel.py`
- Create: `evidence/inspection.py`, `status/snapshot.py`
- Modify: `app/main.py`
- Test: `tests/turn/test_kernel.py`, `test_port_boundaries.py`, `test_revalidation.py`
- Test: `tests/evidence/test_inspection.py`, `tests/status/test_core_snapshot.py`

**Interfaces:**

- Consumes: committed Interaction processing item.
- Produces: trace-complete response candidate/effect outcome through typed Trust, Memory, Egress, Action, and Delivery ports.

- [ ] **Step 1: Write failing port-boundary tests**

Assert the kernel cannot import provider SDKs, channel transports, raw storage repositories, arbitrary query APIs, sandbox/systemd code, or lifecycle reconciler code. Ports must accept opaque IDs/envelopes and return typed decisions/manifests/results. Deny at every `PROCESS`, `EGRESS`, and `DELIVER` boundary when the canonical capability evaluator reports a matching fence, stale generation, or expired lease.

- [ ] **Step 2: Write the reactive kernel test**

Use fake adapters to process one reply with mention and attachment. Assert ordered causal links from native event, Interaction, Trust envelope, memory candidate/materialization, egress intent/decision/attempt/result, response candidate, fresh delivery authorization, intent, adapter request, and receipt. Every evidence record must inherit correct protection.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/turn tests/evidence/test_inspection.py tests/status/test_core_snapshot.py -q
```

Expected: FAIL on missing kernel/status/inspection.

- [ ] **Step 4: Implement the smallest orchestration loop**

Dequeue one committed processing item, obtain current Trust envelope, materialize the exact active static-persona generation/digest plus bounded working context, optionally recall, optionally call registered egress profile, record candidate, revalidate destination/audience, and request Delivery. Every wait checks deadline/generations; failures produce typed safe outcomes. Status reports timestamps, evidence source, freshness, expected/observed generations, queue lag, last commits, pending/unknown effects, and collection errors without principal/chat labels.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/turn tests/evidence tests/status -q
uv run mypy packages/gateway/yeoman_gateway/turn packages/gateway/yeoman_gateway/evidence packages/gateway/yeoman_gateway/status
git add packages/gateway/yeoman_gateway/turn packages/gateway/yeoman_gateway/evidence/inspection.py packages/gateway/yeoman_gateway/status packages/gateway/yeoman_gateway/app/main.py tests/turn tests/evidence tests/status
git commit -m "feat(turn): compose trace-complete reactive kernel"
```

## Task 8: Prove the channel-neutral reactive core and close Milestone 03

**Files:**

- Create: `tests/integration/test_reactive_trace.py`, `test_reactive_crash_matrix.py`, `test_domain_isolation.py`
- Create: `artifacts/program/handoffs/m03-handoff.json`
- Modify: orchestration state after review.

- [ ] **Step 1: Write and run the integrated trace, crash, and isolation tests**

Inject failures at blob/core/intent/model/send/receipt boundaries. Run same-person/different-group, owner-private read-all, shared destination, incomplete membership, revocation, output-taint, projection failure, provider fallback, and unknown-effect scenarios. Assert no loss, widening, duplicate effect, hidden-trace disclosure, or automatic unknown replay.

```bash
uv run pytest tests/integration/test_reactive_trace.py tests/integration/test_reactive_crash_matrix.py tests/integration/test_domain_isolation.py -q
```

Expected: PASS against the already committed Milestone 03 modules; any failure is fixed in the owning earlier task with a new commit and full affected rerun.

- [ ] **Step 2: Commit the green integration contract**

```bash
git add tests/integration/test_reactive_trace.py tests/integration/test_reactive_crash_matrix.py tests/integration/test_domain_isolation.py
git commit -m "test(integration): prove reactive core boundaries"
```

- [ ] **Step 3: Run the complete milestone gate**

```bash
uv run pytest tests/edge tests/interaction tests/memory tests/egress tests/actions tests/delivery tests/turn tests/evidence tests/status tests/integration tests/architecture -q
uv run ruff check .
uv run mypy packages/shared packages/gateway
uv build --all-packages
uv run python scripts/release/verify_artifact.py --manifest dist/build-manifest.json --dist dist
git diff --check
```

Expected: all pass with fake external adapters and all runtime effect capabilities still fenced.

- [ ] **Step 4: Obtain reviewer GOs and advance**

Architecture reviews module ownership/Turn Kernel size and causal transaction boundaries. Security reviews cross-domain isolation, materialization, egress credential/data handling, output taint, effect unknowns, and protected evidence inspection. After both GO:

```bash
git add artifacts/program/handoffs/m03-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md
git commit -m "docs(architecture): accept reactive core milestone"
```

Expected: next state is `READY_FOR_MILESTONE_04`.
