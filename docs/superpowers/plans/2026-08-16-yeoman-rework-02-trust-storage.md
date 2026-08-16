# Yeoman Rework Milestone 02: Trust and Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical durable storage, encrypted blobs, Secret/Key Authority, immutable generations, Trust authority, lifecycle journal, and target-side migration foundation.

**Architecture:** One Gateway writer coordinates short transactions in `core.db`, while each logical module owns its tables, repositories, migrations, and causal evidence. Secrets and active keys stay behind one fixed Unix-socket service; Trust produces immutable narrowing envelopes and fails closed on every unknown or stale input.

**Tech Stack:** Python 3.14, SQLite 3/WAL, Pydantic 2, cryptography AEAD and Ed25519/HMAC primitives, python-fido2 for local phishing-resistant step-up, systemd credentials, Unix sockets, pytest/property tests, Ruff, and mypy strict mode.

## Global Constraints

- Read the orchestration file, normative §§3-8, 12-15, 18.2-18.3, and 19, plus `m01-handoff.json`.
- `core.db` uses WAL, `synchronous=FULL`, `foreign_keys=ON`, bounded busy timeout, and strict `PRAGMA user_version` migrations.
- External I/O never occurs inside a core transaction.
- Interaction, Trust, Memory, Actions, Egress, Delivery, Evidence, and Workloads own their own repositories and migrations; `storage/` owns only primitives, blobs, registry, and global ordering.
- Edge, workers, projections, Overseer, Secret/Key Authority, and backup never open `core.db`.
- Unknown identity, domain, audience, membership, classification, route, generation, or grant fails closed.
- Identity linkage never merges domains or memory.
- Only a verified owner in a positively owner-private destination can receive read-all authority.
- First-release suppression is reversible and owner-only; no erase API or retention purge ships.
- The state root remains fresh and non-live, with all effect capabilities fenced.
- Secret/Key Authority uses test keys only in this milestone; no real channel session, provider credential, key-encryption key, or migrated protected state activates before Milestone 04 proves its backup and fenced blank-root recovery contract.

---

## File map

- `packages/shared/yeoman_shared/contracts/ipc.py` — authenticated producer headers, bounds, replay/conflict codes.
- `packages/shared/yeoman_shared/contracts/status.py` — redacted fact and generation-vector wire types.
- `packages/shared/yeoman_shared/contracts/migration.py` — canonical target migration command and source-native Edge-segment schemas.
- `packages/gateway/yeoman_gateway/storage/connection.py` — `CoreDatabase` connection and transaction ownership.
- `packages/gateway/yeoman_gateway/storage/registry.py` — typed store/schema registry.
- `packages/gateway/yeoman_gateway/storage/migrations.py` — ordered module migration coordinator.
- `packages/gateway/yeoman_gateway/storage/blobs.py` — encrypted immutable blob stage/commit/read mechanics.
- `packages/gateway/yeoman_gateway/storage/lifecycle.py` — independent monotonic lifecycle journal client/verification.
- `packages/gateway/yeoman_gateway/secrets/contracts.py` — role-limited request/receipt types.
- `packages/gateway/yeoman_gateway/secrets/store.py` — protected credential/key/session store owned only by the service.
- `packages/gateway/yeoman_gateway/secrets/service.py` — `yeoman-secret-authority.service` Unix-socket entrypoint.
- `packages/gateway/yeoman_gateway/secrets/client.py` — exact-capability client for allowed roles.
- `packages/gateway/yeoman_gateway/config/models.py` — strict public config/policy/provider-generation schemas.
- `packages/gateway/yeoman_gateway/config/activation.py` — compare-and-swap encrypted activation.
- `packages/gateway/yeoman_gateway/config/persona.py` — one immutable owner-signed static persona generation and digest.
- `packages/gateway/yeoman_gateway/capabilities/models.py`, `schema.py`, `repository.py`, `evaluation.py` — fixed capability and additive-fence records plus effective-permission evaluation.
- `packages/gateway/yeoman_gateway/evidence/models.py` — opaque evidence references and inherited protection labels.
- `packages/gateway/yeoman_gateway/evidence/repository.py` — module-local causal evidence writes and protected lookup.
- `packages/gateway/yeoman_gateway/evidence/bootstrap_import.py` — one-time verification/import of the signed Milestone 01 receipt before owner authority exists.
- `packages/gateway/yeoman_gateway/trust/models.py` — principals, platform identities, domains, audiences, membership, grants, envelopes.
- `packages/gateway/yeoman_gateway/trust/contracts.py` — verified step-up and authorization/lease protocol records shared by earlier tasks.
- `packages/gateway/yeoman_gateway/trust/schema.py` — Trust DDL and migration definitions.
- `packages/gateway/yeoman_gateway/trust/repository.py` — Trust-owned queries and writes.
- `packages/gateway/yeoman_gateway/trust/authorization.py` — pure deterministic evaluation.
- `packages/gateway/yeoman_gateway/trust/bootstrap.py` — permanently sealed first-owner enrollment ceremony.
- `packages/gateway/yeoman_gateway/trust/step_up.py` — local action-bound assertion verification.
- `packages/gateway/yeoman_gateway/trust/service.py` — revalidation/materialization authority facade.
- `packages/gateway/yeoman_gateway/app/operator.py` — authenticated local enrollment, activation, and high-risk mutation entrypoint.
- `packages/gateway/yeoman_gateway/app/migrate.py` — target importer for signed canonical migration records only.
- `tests/storage/`, `tests/secrets/`, `tests/config/`, `tests/trust/`, `tests/evidence/`, and `tests/migration/` — owner-bound contract suites.

## Task 1: Establish the one-writer core database and migration registry

**Files:**

- Create: `storage/connection.py`, `storage/registry.py`, `storage/migrations.py`
- Test: `tests/storage/test_connection.py`, `test_registry.py`, `test_migrations.py`

**Interfaces:**

- Consumes: ordered `ModuleMigration(module, from_version, to_version, apply)` registrations.
- Produces: `CoreDatabase.transaction(owner)` and one globally ordered schema generation.

- [ ] **Step 1: Write failing durability and ownership tests**

Tests must assert exact PRAGMAs, rollback on exception, rejection of nested/external-I/O markers, one writer thread/process owner, foreign-key enforcement, unknown module rejection, duplicate migration rejection, and atomic global version advancement:

```python
with core.transaction(ModuleOwner.TRUST) as tx:
    tx.execute_owned(
        "trust",
        "INSERT INTO trust_principals(principal_id, principal_kind) VALUES (?, ?)",
        ("principal-1", "human"),
    )
assert core.pragmas() == RequiredPragmas(journal_mode="wal", synchronous=2, foreign_keys=1)
```

- [ ] **Step 2: Run tests and prove absence**

```bash
uv run pytest tests/storage/test_connection.py tests/storage/test_registry.py tests/storage/test_migrations.py -q
```

Expected: FAIL on missing storage modules.

- [ ] **Step 3: Implement the minimal typed registry**

`CoreDatabase` accepts one explicit database path beneath the non-live state root, refuses symlinks and network filesystems, sets required PRAGMAs per connection, and exposes no general repository. `OwnedTransaction.execute_owned(module, sql, params)` validates the table prefix registered to that module. Migration functions receive an owned transaction and perform no filesystem/network/provider calls.

- [ ] **Step 4: Run task tests and commit**

```bash
uv run pytest tests/storage -q
uv run mypy packages/gateway/yeoman_gateway/storage
git add packages/gateway/yeoman_gateway/storage tests/storage
git commit -m "feat(storage): add owned core transaction registry"
```

## Task 2: Implement encrypted immutable blobs and domain/key-epoch identifiers

**Files:**

- Create: `storage/blobs.py`
- Create: `secrets/contracts.py` with the key-capability wire contract used by storage.
- Test: `tests/storage/test_blobs.py`, `tests/storage/test_blob_crash_safety.py`

**Interfaces:**

- Consumes: `BlobWriteRequest(domain_id, key_epoch, media_type, plaintext_stream)` and one wrapped data-key capability.
- Produces: `BlobRef(object_id, ciphertext_sha256, byte_size, key_epoch, media_type)` only after durable commit.

- [ ] **Step 1: Write failing stage/commit/crash tests**

Cover randomized ciphertext, same-plaintext/different-domain inequality, no global plaintext-hash path, stage fsync, hash verification, atomic no-replace rename, directory fsync, missing committed blob as integrity failure, and safe orphan collection. Inject crashes after each durability boundary and assert no database reference can point to an uncommitted blob.

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/storage/test_blobs.py tests/storage/test_blob_crash_safety.py -q
```

Expected: FAIL on missing `EncryptedBlobStore`.

- [ ] **Step 3: Implement exact blob operations**

Expose `stage(request, key_capability) -> StagedBlob`, `commit(staged) -> BlobRef`, `open(ref, key_capability) -> BinaryIO`, and `verify(ref) -> BlobIntegrity`. Use domain/key-epoch-scoped keyed opaque IDs, a random per-object data key, authenticated encryption, and wrapped key metadata. Reject overwrite, path traversal, changed stage inode, wrong key epoch, wrong digest, oversize input, and symlink substitution.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/storage -q
uv run ruff check packages/gateway/yeoman_gateway/storage tests/storage
git add packages/gateway/yeoman_gateway/storage/blobs.py packages/gateway/yeoman_gateway/secrets/contracts.py tests/storage
git commit -m "feat(storage): add durable encrypted blob store"
```

## Task 3: Build the fixed Secret/Key Authority

**Files:**

- Create: `secrets/store.py`, `secrets/service.py`, `secrets/client.py`
- Create: `deploy/systemd/yeoman-secret-authority.service`
- Test: `tests/secrets/test_authority.py`, `test_roles.py`, `test_session_cas.py`, `test_socket_security.py`

**Interfaces:**

- Consumes: authenticated `SecretCapabilityRequest(role, operation, alias, expected_generation, nonce, expiry)`.
- Produces: one-use `SecretCapability` or `SecretReceipt`; enumeration and bulk plaintext export do not exist.

- [ ] **Step 1: Write failing role and generation tests**

Define fixed roles `gateway_egress`, `edge_account`, `delivery_account`, `backup_snapshot`, and `recovery_operator`. Tests assert each role can perform only named alias operations, stale compare-and-swap fails, revoked generations fail immediately, peer credentials are checked, requests replay once only, and protected values never appear in response errors/log records.

- [ ] **Step 2: Write the service-boundary test**

Parse the service unit and assert no network namespace access, no conversational/model entrypoint, a private runtime directory, explicit `LoadCredentialEncrypted=`, restrictive filesystem policy, and one protected Unix socket. Assert the service import graph excludes provider/channel/turn/memory modules.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/secrets -q
```

Expected: FAIL on missing authority.

- [ ] **Step 4: Implement named one-use operations**

Implement credential resolve, wrapped object-key issue, platform-session compare-and-swap, generation rotate/revoke, encrypted backup snapshot, and recovery-only restore. Bind every capability to peer process identity, role, alias, generation, nonce, and expiry; zeroize short-lived plaintext buffers where the Python/runtime boundary permits and never copy them into core/evidence.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/secrets -q
uv run mypy packages/gateway/yeoman_gateway/secrets
git add packages/gateway/yeoman_gateway/secrets deploy/systemd/yeoman-secret-authority.service tests/secrets
git commit -m "feat(secrets): add fixed local key authority"
```

## Task 4: Implement strict immutable activation generations

**Files:**

- Create: `config/models.py`, `config/activation.py`
- Create: `trust/contracts.py`
- Modify: `app/preflight.py`
- Test: `tests/config/test_activation.py`, `test_strict_loading.py`, `test_generation_drift.py`, `test_static_persona.py`

**Interfaces:**

- Consumes: encrypted activation envelope, expected prior generation, owner action-bound assertion.
- Produces: immutable `ActiveGeneration(kind, sequence, digest, activated_by, receipt_ref)`.

- [ ] **Step 1: Write failing strict-loader tests**

Assert unknown keys, invalid content, missing digest, stale prior generation, implicit defaults, load-time writes, environment mutation, and fallback all fail. Assert pure validation leaves files and database unchanged. Static persona activation requires exact content/digest/owner signature and rejects companion evolution files, destination-hidden instructions, or runtime mutation.

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/config -q
```

Expected: FAIL on missing config package.

- [ ] **Step 3: Implement validation and compare-and-swap activation**

Model release, config, policy, storage schema, provider policy, static persona, key, and control generations as separate ordered values plus digests. This task accepts only a `VerifiedStepUpAssertionV1` contract created by an internal test fixture; the contract contains actor, action/target digest, nonce, trace, expiry, and generation. No operator entrypoint exposes activation until Task 8 supplies the production FIDO2 verifier and wires it in before milestone closure. Activation validates the encrypted payload after exact Secret Authority capability resolution, binds the assertion to prior/new digest, commits one generation receipt, and leaves the old generation active on any failure. No executable path can propose, learn, or auto-apply persona changes.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/config tests/app/test_fenced_bootstrap.py -q
git add packages/gateway/yeoman_gateway/config packages/gateway/yeoman_gateway/trust/contracts.py packages/gateway/yeoman_gateway/app/preflight.py tests/config
git commit -m "feat(config): add strict generation activation"
```

## Task 5: Implement canonical capabilities and additive fences

**Files:**

- Create: `capabilities/models.py`, `schema.py`, `repository.py`, `evaluation.py`
- Test: `tests/capabilities/test_fences.py`, `test_effective_permission.py`, `test_clearance.py`, `test_generations.py`

**Interfaces:**

- Consumes: persisted Trust authorization and current execution lease through explicit protocols, scoped fence set, and generation vector; tests use deny/allow fakes until Task 9 supplies production Trust/lease values.
- Produces: deterministic effective permission for `EDGE_CAPTURE`, `CORE_INGEST`, `PROCESS`, `EGRESS`, or `DELIVER`.

- [ ] **Step 1: Write failing fence-model tests**

Cover global, channel/account, provider/tool route, registered workload, and destination/action scope. Require issuer/sequence, cause/evidence, generation vector, issue/recheck time, stickiness, clearance authority, and proof predicate. No issuer can clear another issuer's fence; expiry or stale proof remains closed; security/integrity/restore/migration/cutover/generation mismatch is sticky.

- [ ] **Step 2: Write failing effective-permission tests**

Assert permission is true only when Trust allows, the exact lease is current, and no applicable fence exists. Unknown/stale Trust, lease, scope, generation, proof, or fence state denies. Gateway startup/security/migration issuers work now; no Overseer issuer exists until Milestone 04.

- [ ] **Step 3: Implement the owned schema and pure evaluator**

Persist canonical fence issue/clearance receipts in `core.db` with module-owned causal evidence. Keep evaluation deterministic and side-effect free; mutations require the fixed issuer role and, for owner clearance, action-bound step-up with current proof.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/capabilities -q
git add packages/gateway/yeoman_gateway/capabilities tests/capabilities
git commit -m "feat(control): add canonical capability fences"
```

## Task 6: Implement identities, domains, memberships, and audiences

**Files:**

- Create: `trust/models.py`, `trust/schema.py`, `trust/repository.py`
- Create: `evidence/models.py`, `evidence/repository.py`
- Test: `tests/trust/test_identity_domains.py`, `test_memberships.py`, `test_audiences.py`
- Test: `tests/evidence/test_protection_labels.py`

**Interfaces:**

- Consumes: native platform/account/epoch identity evidence and versioned membership snapshots.
- Produces: immutable principals, reversible identity links, one context domain per conversation lineage, and conservative reachable audience versions.

- [ ] **Step 1: Write failing same-person/different-group tests**

Create one principal linked to one WhatsApp identity and two groups containing the same members. Assert two distinct domain IDs, separate audience/membership versions, no cross-domain query result, and no implicit grant. Add conflicting privileged-link quarantine, incomplete membership, removal, expansion, and platform re-key cases.

- [ ] **Step 2: Write evidence inheritance tests**

Combine two protected source labels and assert evidence receives the union of domains, intersection of audiences, highest handling class, and conjunction of grants. Ordinary denial/status lookup must not reveal a hidden trace exists.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/trust/test_identity_domains.py tests/trust/test_memberships.py tests/trust/test_audiences.py tests/evidence -q
```

Expected: FAIL on missing Trust/Evidence types.

- [ ] **Step 4: Implement owned schemas and repositories**

Use immutable time-versioned records for `Principal`, `PlatformIdentityKey`, `IdentityLink`, `ContextDomain`, `DomainLineage`, `MembershipSnapshot`, `Audience`, and `AudienceMemberEvidence`. Every repository method is domain-scoped except the separately authorized owner audit query. Store causal evidence in the same owned transaction as each Trust mutation.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/trust/test_identity_domains.py tests/trust/test_memberships.py tests/trust/test_audiences.py tests/evidence -q
git add packages/gateway/yeoman_gateway/trust packages/gateway/yeoman_gateway/evidence tests/trust tests/evidence
git commit -m "feat(trust): add isolated identity and domain authority"
```

## Task 7: Import the signed Milestone 01 bootstrap receipt

**Files:**

- Create: `evidence/bootstrap_import.py`
- Test: `tests/migration/test_m01_receipt_import.py`

**Interfaces:**

- Consumes: exact signed/encrypted Milestone 01 receipt, its safe-reference commitment, pinned offline program public key, fresh target Evidence, and expected source/spec digests.
- Produces: one immutable protected Evidence record and sealed import marker; it creates no principal, credential, grant, lease, or capability.

- [ ] **Step 1: Write failing authenticity and one-time-import tests**

Verify the offline Ed25519 signature, safe-reference digest, source/spec/build commitments, schema, and encryption recipient before import. Reject altered or unsigned content, wrong key/spec/source, non-fresh Evidence, duplicate/conflicting import, missing preservation evidence, or any receipt containing live authority. Assert import cannot open processing, egress, delivery, workloads, activation, owner, or conversational authority.

- [ ] **Step 2: Run the failing import test**

```bash
uv run pytest tests/migration/test_m01_receipt_import.py -q
```

Expected: FAIL on missing bootstrap importer.

- [ ] **Step 3: Implement immutable bootstrap-evidence import**

Decrypt only through the approved local recovery ceremony, canonicalize and verify before writing, then atomically commit the protected receipt, signature/key commitment, safe-reference digest, import evidence, and permanent one-time marker. Preserve the original signed bytes as immutable Evidence. On any mismatch leave Evidence unchanged and all capabilities fenced.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/migration/test_m01_receipt_import.py tests/evidence -q
git add packages/gateway/yeoman_gateway/evidence/bootstrap_import.py tests/migration/test_m01_receipt_import.py
git commit -m "feat(evidence): import signed bootstrap receipt"
```

## Task 8: Bootstrap the first owner step-up credential

**Files:**

- Create: `trust/bootstrap.py`, `trust/step_up.py`, `app/operator.py`
- Modify: `packages/gateway/pyproject.toml`, `uv.lock`
- Test: `tests/trust/test_initial_owner_enrollment.py`, `test_step_up.py`, `test_recovery_replacement.py`

**Interfaces:**

- Consumes: fresh empty target Trust state, the immutable Task 7 bootstrap receipt Evidence reference, one local FIDO2 authenticator, and an exact enrollment action.
- Produces: one verified owner principal/FIDO2 public credential generation, one canonical enrollment receipt, and a sealed bootstrap-enrollment marker; it grants no conversational or effect capability.

- [ ] **Step 1: Write the failing one-time enrollment ceremony**

Require a fresh state root with no principal/credential, all capabilities fenced, the exact Task 7 immutable receipt/import marker, and a new signature from its pinned offline Ed25519 key over receipt digest, release/state-root identity, owner principal, FIDO2 registration challenge/public key, RP ID, nonce, expiry, and expected generation. Reject non-empty state, missing or mismatched bootstrap receipt/import marker, altered key/challenge, reused nonce, remote origin, absent FIDO user presence/verification, or any attempt to open processing/egress/delivery.

- [ ] **Step 2: Write replay-resistant operational step-up tests**

Use a locally attached FIDO2/WebAuthn authenticator with user presence and user verification. Bind the signed challenge to actor, operation, target/diff digest, nonce, trace, expiry, expected generation, and relying-party/origin fixed to the local operator CLI. Reuse, altered target, remote/chat-origin assertion, missing UP/UV flags, cloned/stale credential counter, expired assertion, or recovery material alone must fail. Store only the enrolled public credential and protected enrollment/revocation evidence; enrollment and replacement themselves require the offline recovery procedure plus a canonical receipt.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/trust/test_initial_owner_enrollment.py tests/trust/test_step_up.py tests/trust/test_recovery_replacement.py -q
```

Expected: FAIL on missing bootstrap and verifier.

- [ ] **Step 4: Implement and permanently seal the bootstrap path**

Expose one local `operator enroll-initial-owner` command only when Trust generation is zero and the sealed marker is absent. Atomically commit owner identity, authenticator public credential/counter, bootstrap receipt reference, enrollment evidence, generation one, and the permanent marker. The command disappears/refuses forever afterward. Replacement uses separately stored offline recovery material through an explicit recovery-only ceremony, rotates/revokes the old credential, and records a new canonical receipt; it never infers owner-private conversational status.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/trust/test_initial_owner_enrollment.py tests/trust/test_step_up.py tests/trust/test_recovery_replacement.py -q
uv run mypy packages/gateway/yeoman_gateway/trust packages/gateway/yeoman_gateway/app/operator.py
git add packages/gateway/yeoman_gateway/trust/bootstrap.py packages/gateway/yeoman_gateway/trust/step_up.py packages/gateway/yeoman_gateway/app/operator.py packages/gateway/pyproject.toml uv.lock tests/trust/test_initial_owner_enrollment.py tests/trust/test_step_up.py tests/trust/test_recovery_replacement.py
git commit -m "feat(trust): bootstrap local owner step-up"
```

## Task 9: Implement grants, authorization, and narrowing envelopes

**Files:**

- Create: `trust/authorization.py`, `trust/service.py`
- Modify: `capabilities/evaluation.py` to consume only the production Trust/lease protocols.
- Test: `tests/trust/test_authorization_properties.py`, `test_owner_read_all.py`, `test_envelopes.py`, `test_revocation.py`
- Test: `tests/capabilities/test_trust_integration.py`

**Interfaces:**

- Consumes: `AuthorizationRequest`, verified Task 8 assertion where mutation requires it, and exact current generation vector.
- Produces: persisted `TrustDecision` and immutable `ExecutionEnvelope`; children can only narrow.

- [ ] **Step 1: Write the deterministic authorization matrix**

Cover operations `read_raw`, `read_derived`, `summarize`, `quote`, `send`, `persist_in_destination`, and `export`. Property tests must prove unknown/stale inputs deny, identity links do not grant, grants are non-transitive, child envelopes never widen, membership expansion is prospective, owner read-all requires verified owner plus owner-only reachable audience, and shared output is rematerialized under destination authority.

- [ ] **Step 2: Write grant, revocation, and capability-integration tests**

Require verified step-up for identity links/revocation, cross-domain grants, bulk export, and other high-risk mutation. Wire production `TrustDecision` and reactive `ExecutionEnvelope` lease facts into the capability evaluator; allow requires Trust, current lease, and no applicable fence. Every stale, deny, revocation, generation mismatch, or fence path closes.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/trust/test_authorization_properties.py tests/trust/test_owner_read_all.py tests/trust/test_envelopes.py tests/trust/test_revocation.py tests/capabilities/test_trust_integration.py -q
```

Expected: FAIL on missing authorization service.

- [ ] **Step 4: Implement pure evaluation and persisted decisions**

Keep evaluation free of I/O: resolve all exact versions before calling it, return explicit allow/deny reasons, then persist request inputs, generation vector, decision, evidence protection, and expiry. Revalidate before materialization, sensitive recall, egress, mutation, delivery, and after waits.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/trust tests/capabilities/test_trust_integration.py -q
uv run mypy packages/gateway/yeoman_gateway/trust packages/gateway/yeoman_gateway/capabilities
git add packages/gateway/yeoman_gateway/trust/authorization.py packages/gateway/yeoman_gateway/trust/service.py tests/trust/test_authorization_properties.py tests/trust/test_owner_read_all.py tests/trust/test_envelopes.py tests/trust/test_revocation.py tests/capabilities/test_trust_integration.py
git add packages/gateway/yeoman_gateway/capabilities/evaluation.py
git commit -m "feat(trust): authorize narrowing execution envelopes"
```

## Task 10: Add lifecycle authority and target migration ingestion

**Files:**

- Create: `storage/lifecycle.py`
- Create: `app/migrate.py`
- Create: `packages/shared/yeoman_shared/contracts/migration.py`
- Test: `tests/storage/test_lifecycle_journal.py`
- Test: `tests/migration/test_target_ingest.py`, `test_legacy_unknown.py`, `test_dedup_scope.py`

**Interfaces:**

- Consumes: signed migration batches in canonical target command format, source manifest digest, and newest authenticated lifecycle head.
- Produces: exactly one receipt/disposition per source object and quarantined `legacy_unknown` objects that cannot materialize.

- [ ] **Step 1: Write failing journal and ingestion tests**

Assert monotonic sequence/MAC chaining, missing/stale/conflicting current head failure, idempotent same-object/same-digest import, quarantine on digest conflict, dedup only within domain+key epoch, one disposition per source object, and no authority import from old sessions/grants/timers/desired-running state. Require the exact immutable Task 7 bootstrap-receipt Evidence reference; any mismatch blocks target ingestion and M02 closure.

- [ ] **Step 2: Write `legacy_unknown` non-materialization tests**

Assert ordinary content read, projection, recall, model/tool egress, workload, delivery, and plaintext owner audit all deny. Only a separate action-bound `resolve_legacy_provenance` command with independent origin/audience/classification evidence may create a new available object version while preserving the quarantine record.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/storage/test_lifecycle_journal.py tests/migration -q
```

Expected: FAIL on missing lifecycle/import code.

- [ ] **Step 4: Implement the target-only importer**

Accept no old paths, SQLite table names, raw SQL, or legacy class names. Validate toolkit signature, source manifest digest, batch order, lifecycle head, and source disposition before staging blobs and atomically committing target metadata/receipts. Leave interrupted batches safely resumable by source object ID.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/storage tests/migration tests/trust tests/secrets tests/config tests/evidence -q
git add packages/gateway/yeoman_gateway/storage/lifecycle.py packages/gateway/yeoman_gateway/app/migrate.py packages/shared/yeoman_shared/contracts/migration.py tests/storage/test_lifecycle_journal.py tests/migration
git commit -m "feat(migration): ingest canonical preservation records"
```

## Task 11: Close Milestone 02

**Files:**

- Create: `artifacts/program/handoffs/m02-handoff.json`
- Modify: orchestration state after review.

- [ ] **Step 1: Run the complete gate**

```bash
uv run pytest tests/storage tests/secrets tests/config tests/capabilities tests/trust tests/evidence tests/migration tests/architecture tests/app -q
uv run ruff check .
uv run mypy packages/shared packages/gateway
uv build --all-packages
uv run python scripts/release/verify_artifact.py --manifest dist/build-manifest.json --dist dist
git diff --check
```

Expected: all pass; architecture scans prove no cross-owner SQL, secret enumeration, raw-key storage, external I/O in transactions, legacy imports, or unfenced effect entrypoint.

- [ ] **Step 2: Obtain reviewer GOs**

Architecture review must verify schema/repository ownership and transaction boundaries. Security review must verify key authority, owner/private semantics, step-up binding, domain isolation, lifecycle restoration, and quarantine. Resolve every blocker.

- [ ] **Step 3: Commit receipt and advance**

```bash
git add artifacts/program/handoffs/m02-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md
git commit -m "docs(architecture): accept trust and storage foundation"
```

Expected: next state is `READY_FOR_MILESTONE_03`.
