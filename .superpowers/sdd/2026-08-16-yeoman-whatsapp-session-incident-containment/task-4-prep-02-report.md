# Task 4 pre-owner v1-derived state-evidence report

## Status

Synthetic implementation and the post-review correction round are complete. Production/live invocation remains NO-GO. The earlier blocked states below are retained as chronology; the final current evidence is in **Terra rejection correction round**.

## Exact contract conflict

The accepted state-evidence plan requires `inspect_v1()` to decrypt and verify a fixed *external* v1 artifact, prove both recipient decryptions and its signature, and then compare the resulting exact external serialization SHA-256 with `6ae6476466c728bbb423be16182b6341e47e38c0d55073f9831751452346f833` (plan lines 7 and 15; controller pseudocode lines 65-69).

The accepted common library offers no external-v1 reader contract. Its only protected-record reader is private `_read_protected_record(kind, commitment, config)` and requires a complete `EvidenceCommitment` (library lines 963-1013). It derives the artifact directory from `commitment.record_hmac_sha256`, requires `public.json` to reconstruct the exact same `EvidenceCommitment`, and validates ciphertext/signature hashes bound to that record. The state plan provides only the plaintext serialization commitment; it supplies neither a fixed artifact locator nor a record HMAC, ciphertext SHA-256, signature SHA-256, kind/envelope, or the external v1 encryption/signature format.

The quiescence handoff is available and compatible: `_record_quiescence_receipt` / `_verify_quiescence_receipt` retain the dedicated receipt journal and `EvidenceCommitment` contract (library lines 1227-1299). It cannot authenticate the unrelated external v1 artifact.

Consequently, implementing the requested v1 inspection by fabricating an `EvidenceCommitment`, treating the known plaintext SHA-256 as a record HMAC, guessing a production artifact path, or adding an unreviewed external decrypt/signature path would weaken the accepted common-library contract and violate the plan's fail-closed/no-output requirement. The task instruction requires stopping for exactly this conflict.

## Required resolution

Provide one accepted source of truth for the external v1 artifact: either (1) a reviewed common-library API plus fixed v1 locator/commitments/kind that returns bounded verified bytes without output, or (2) a reviewed v1 artifact descriptor containing its fixed protected location, artifact format, exact ciphertext/signature commitments, signer/namespace, both recipient verification semantics, and canonical serialization envelope. The resolved contract must be usable by disposable fakes for RED/GREEN/mutation tests and fixed production wiring without production invocation.

## Controller resolution proposed for independent review

The controller performed a metadata-only lookup of the already-recorded evidence directory and parsed only the public SSH signature envelope fields. It then read only the historical controller tool-call that originally created and verified this artifact, not the affected sensitive implementer transcript. It did not decrypt v1, open an age identity, read manifest plaintext, inspect runtime state, or invoke a production controller. This established the immutable locator, artifact sizes, exact creation schema, signature target, signer/namespace, and original dual-decryption verification procedure that the durable note omitted.

The first independent Terra plan review rejected the initial correction with Important findings. It required a separate legacy crypto path, held-FD hash-to-use binding, exact cumulative caps, distinct identity-to-recipient verification, explicit legacy type names, and legacy-specific child/FD/no-output tests. The amended plan adds all of those controls and freezes new-format `EvidenceCommitment`, namespace, and ciphertext-signature semantics unchanged.

The plan now specifies a narrow common-library legacy reader with source-pinned directory/file names, sizes, the already-recorded plaintext/ciphertext/signature SHA-256 values, signer identity `yeoman-preservation-2026-08-16`, SSH namespace `git`, signature over exact decrypted plaintext bytes, both fixed age identities/recipients, and the fixed allowed-signers file. The adjacent legacy commitments file is explicitly non-authoritative. The reader uses the same held FDs from metadata/hash through age/SSH consumption, enforces explicit cumulative bounds, two independent equal decryptions, the exact plaintext hash, and detached-signature verification before strict parsing. The exact original v1 JSON schema/counts/root labels are now pinned; `workspace_persona_evolution` is historical inert preservation, not a target behavior. New-format `EvidenceCommitment` and `_read_protected_record()` semantics remain unchanged.

Implementation remains paused until the same Terra plan reviewer accepts this corrected compatibility boundary. The metadata/history lookup changed no service, runtime, auth, key, artifact, message, memory, archive, QR, or evidence bytes.

## Corrected plan acceptance

- The same independent Terra plan reviewer re-read the correction and returned **ACCEPT** with no remaining Critical or Important issue.
- Accepted plan SHA-256: `ff993684668bec71b11078e1f9718ef9f4883871203a7914f5ec3fe144418080`.
- Accepted boundary: fixed legacy descriptor/schema, held-FD hash-to-consumption binding, a separate plaintext/`git` signature verifier, exact sizes and cumulative caps, distinct pinned identities/recipients, test-only injection, cleanup/no-output coverage, and inert historical preservation of `workspace_persona_evolution`; new-format `EvidenceCommitment` behavior stays frozen.
- State implementation may resume from RED against this exact plan. Production/live invocation remains NO-GO.

## Implementation stop: missing complete SQLite profile registry

The corrected plan resolves the legacy-v1 crypto-reader conflict, but its source-defined profile requirement cannot be implemented without forbidden information. It requires a production registry keyed by the exact v1 manifest paths and, for every supported SQLite snapshot, its exact `PRAGMA user_version`, table name, ordered SELECT columns, and stable unique-key tuple. The plan supplies none of those profile entries.

The permitted source-only inspection identifies multiple possible SQLite stores (`memory.db`, `reply_context.db`, `chat_registry.db`, persona-evolution state, contacts, and others), but source code neither identifies which of them are among the 248 legacy-v1 entries nor fixes their v1-time schemas/versions. Selecting any subset or current-source schema would be speculative; an empty registry would reject every real v1 SQLite entry and violate the required full coverage. Determining the missing facts requires decrypting the legacy manifest or live runtime inspection, both explicitly forbidden for this implementation task.

Per the task instruction, implementation stops before RED rather than shipping an incomplete registry. No toolkit source/test was changed and no production/live action was invoked after the corrected-plan read.

### Required resolution

Add an accepted source-pinned registry table to the plan or another reviewed source artifact. It must enumerate every SQLite entry expected in legacy v1 with its exact root/relative path, `user_version`, table, ordered columns, unique key tuple, and explicit DB/WAL/SHM held-member names. Then synthetic-only RED/GREEN implementation can resume without decrypting v1 or inspecting the runtime.

## Architectural simplification: exact-byte state proof

The controller stepped back from the missing registry. Because the authenticated v1 already pins the complete 248-file path set plus every file's mode, size, `mtime_ns`, and SHA-256; pre-baseline requires exact live equality to all of those facts; Gateway/Overseer remain stopped; Bridge-only smoke may not touch the four roots; and post comparison permits no root growth, per-table logical adapters add no preservation power. They instead risk omitting internal/legacy tables or normalizing unversioned schemas.

Two independent Terra source/architecture reviews returned ACCEPT for one `immutable_exact_v1` adapter for every file. SQLite DB/WAL/SHM/journal, JSONL, and all opaque files remain ordinary exact entries. The controller must enumerate the complete roots twice through no-follow descriptors, reject every unexpected path/type/link, require exact v1 facts at baseline, add current security metadata to protected v2, and require exact pre/post v2 equality. Only the separately rooted authenticated observer chain may grow with verified causal classification.

This proves perfect preservation of the sealed legacy state; semantic import into the new target remains a later migration-coverage/receipt gate. Any sidecar change caused by shutdown is a pre-owner mismatch, never an allowed normalization. No source/runtime/live file was changed during these reviews.

The final plan reviewer rejected the first written simplification until it pinned all four label-to-path mappings, separated and freshly bound pre/post quiescence/inventory kinds, made directory-prefix and empty-directory equality explicit, and defined Unicode NFC collision semantics. The plan now includes those corrections plus dedicated state journals and wrong-kind/replay/swap tests; implementation remains paused for final re-review.

Final re-review returned **ACCEPT** with no remaining Critical or Important issue. Accepted final plan SHA-256: `f99b243a757d6448fe873bb37c64a4e082db63e693d4156d5d8f75cabdef7f10`. The execution handoff now fixes the first code GO, pre capture, mandatory second evidence-bound GO before owner work, post capture/compare, and final Luna GO in order. Synthetic implementation may resume; every production/live action remains NO-GO.

## Evidence

- Toolkit branch: `c/yeoman-migration-toolkit`, clean at inspection; required ancestor `6ba55014242953e72ec7a29e75041f704452b885` is present.
- Read-only contract inspection: `scripts/incident_evidence_lib.py` and the accepted Task 4 common/quarantine report.
- No RED/GREEN/mutation command was run because implementation remained paused pending final plan acceptance; the corrected simplified interface is now specified.

## Final exact-byte implementation

Status: complete for synthetic implementation and ready for independent source/security review. No integration or smoke run was started.

### Commits

- `0281c28e20fd75ae9cebc7d93a65ae52e4ad3045 feat(incident): inspect v1 rotation scope`
- `b5bae26b73100940add0daecea68f0ad9b94512b feat(incident): reconcile v1-derived rotation state`
- `32a3b27812bdea6e8b406200c377f68e67a1d376 fix(security): revalidate observer chain prefix`

### RED and GREEN evidence

- Task 1 state RED: `uv run pytest -q tests/shared/test_whatsapp_rotation_state.py` failed 26 tests because `scripts/whatsapp_rotation_state.py` did not exist.
- Task 1 legacy RED: `uv run pytest -q tests/shared/test_incident_evidence_lib.py -k legacy` failed 21 legacy tests because `LegacyV1Descriptor`, `_LegacyV1TestCore`, the frozen legacy constants, and bounded legacy child runner did not exist.
- Task 1 GREEN/gate: `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py` passed 105 tests; scoped Ruff passed; `git diff --check` produced no output.
- Task 2 RED: `uv run pytest -q tests/shared/test_whatsapp_rotation_state.py -k 'comparator or observer_growth or every_v1_file_is_exact'` failed 27 tests because the exact comparator and authenticated observer-chain test seams did not exist.
- Task 2 GREEN/gate: the focused common/state suite passed 132 tests; scoped Ruff passed; `git diff --check` produced no output.
- Follow-up security RED: a protected observer prefix containing a keyed-HMAC-valid but unknown historical classification incorrectly returned `preserved`; its dedicated regression failed 1 test. The comparator now revalidates allowed classification and causality over the entire prior/current chain while counting only additions.
- Final committed-tree verification: `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py` passed 188 tests in 7.22 seconds. `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py` returned `All checks passed!`; `git diff --check` and `git status --short` produced no output.

### Required mutation evidence

- Removed the exact legacy plaintext serialization SHA-256 check. `test_inspect_refuses_noncanonical_or_wrong_hash_and_generic_receipt` failed because the valid canonical fixture with a substituted expected hash no longer raised. Restored.
- Removed complete current-file path-set rejection and explicitly skipped unexpected files. The `added` inventory case failed because a new runtime-root file was accepted. Restored.
- Omitted the first old exact file from pre/post comparison. The `changed-jsonl` comparator case failed because the changed first entry returned `preserved`. Restored.
- Changed comparison to require every old file but ignore extra post files. The `new-file` comparator case failed because the added root entry returned `preserved`. Restored.

### Security and preservation invariants

- The new legacy reader is separate from new-format `EvidenceCommitment` crypto. It pins the legacy directory, names, sizes, ciphertext/signature/plaintext hashes, signer, `git` namespace, two identities/recipients, allowed-signers file, and cumulative caps. It hashes held no-follow FDs and consumes those same ciphertext/signature/identity FDs; exact plaintext is copied only to an anonymous memfd/`O_TMPFILE` FD for signature verification. Both distinct identities must derive the pinned distinct recipients, decrypt independently to identical bounded bytes, and pass the exact plaintext hash before signature verification. The adjacent commitments file is ignored. New-format namespace and ciphertext-signature behavior are unchanged.
- State quiescence stops only Overseer, Bridge, then Gateway through fixed absolute systemctl commands, then reuses the accepted two-sample unit/PID/socket/port validation and dedicated common receipt. Fresh pre/post nonces, fixed protected kinds, exact predecessors, and crash-safe `.state-receipts` / `.state-inventories` journals reject generic records, replay, swapped phases, wrong kinds, and wrong predecessors.
- The fixed four-root walker opens every component/name descriptor-relatively with no-follow semantics. It requires strict UTF-8/NFC names, private owner-UID directories/files, single-link regular files, unique inodes and normalized paths, the exact v1-derived directory/file sets, exact legacy file facts, stable held-FD hashing, and two identical complete passes. SQLite, WAL/SHM/journal, JSONL, and all opaque files receive only `immutable_exact_v1`; no semantic parser, connection, normalization, checkpoint, rewrite, deletion, restore, retry, or replay exists.
- Pre/post comparison authenticates fixed kinds and journals, independently verifies phase quiescence receipts/nonces and post-to-pre binding, then requires exact directory/file inventory equality. Only the fixed authenticated observer kind may have ordered-prefix growth; every prior/current item HMAC, predecessor, classification, and expected-reply causality is revalidated. Public results contain only status and aggregate classification counts.

### No-live attestation

All tests used disposable pytest roots, synthetic files, fake crypto, fake service results, and synthetic protected records. No production wrapper or adapter was invoked. No live runtime/evidence/key/auth path, systemd service, socket, port, network, QR, linked device, message, memory, archive, receipt, or operator-evidence artifact was read, created, changed, or deleted. The toolkit worktree is clean. Owner interaction and all live pre/owner/post actions remain NO-GO pending the required independent reviews and Luna gates.

## Terra rejection correction round

Status: all four Important findings from the independent Terra rejection, plus the subsequently identified prep-03 expectation and layering seams, are corrected in the toolkit. This remains synthetic/source-only work and does not authorize an incident transition.

### Source and implementation commits

- Accepted prep-03 typed evidence plan: `417880a84914bcf4fa2e10631a1f23dfb1b38f42`; SHA-256 `8ac9a1cfb047e892c364e5516974e0699531b1ce81101fa1e40ed5c4fcb6e2f5`.
- Prep-02 contract alignment: `e7c67710067df7eee54756e5b328af7d7834b1be`; observer-boundary clarification: `ef4e908`; current prep-02 plan SHA-256 `8ed9fc0b8c7a796399db22700511d75ae16ce7e1b7d29c9a24580f8480865086`.
- Safe legacy modes, canonical v1 bytes, strict inventories, and held-FD rewind: `f63baeb82483f13ec585199ddd84134cf9eadc28`.
- Accepted typed observer/smoke DAG: `b196e874491fe47286f5cbabe72182f3e886376d`.
- Global state-nonce reservation and crash recovery: `168ddcd30b744a1906dd9d8e9898c907dbba69bc`.
- Prep-03 smoke expectation safety graph and common-verifier layering: `252438c8eff02847cb2170d7112841ee30cfe702`.
- Quarantine-to-current-auth predecessor authentication: `f17ebe05598bf28c5f8a0f32dbc3b1d9dbb957a1`.

### Corrected findings and compatibility seams

- Legacy v1 canonical parsing now uses Python's original default JSON ASCII escaping. Default-escaped NFC non-ASCII bytes are accepted; a literal-UTF-8 reserialization is not interchangeable.
- The walker permits owner-controlled normal read/execute modes such as directories `0755` and files `0644`, while rejecting group/world write bits. It preserves and compares the exact original/pre/post modes.
- The comparator validates each inventory independently before equality: exact file count and byte total, exact four roots and derived directory closure, canonical unique paths, current owner, safe mode, regular single-link files, exact fields/types, and no directory/file collision. Two equally forged inventories cannot establish preservation.
- The legacy reader rewinds the same held ciphertext FD before each independent recipient decryption; a consuming first decrypt cannot starve the second.
- State quiescence reserves `HMAC(key, YEOMAN-ROTATION-STATE-NONCE-V1 || nonce)` directly in `.state-receipts` before record publication. The reservation is incident-global across phase, kind, and record HMAC, contains no raw nonce, recovers both pending crash windows, and refuses completed or concurrently held reuse. This is separate from prep-03's future `.smoke-one-shot` registry.
- The comparator accepts only the fixed prep-03 protected DAG: ready; raw artifacts; contiguous normalized events; expectation; intent; attempt; Bridge acceptance; optional inbound reply; and final complete close. It authenticates fixed kinds, exact schemas, commitments, predecessors, counts, timestamps, account/channel/destination/message causality, and complete capture. Unknown, capture-failed, retired, plain, disconnected, missing, or fabricated graphs are mismatches. Outbound events remain represented by `observed_outbound`.
- `smoke-expectation-v2` now binds the exact incident, a 32-lowercase-hex rotation nonce, fixed smoke text, observer ready, fixed-kind current-auth and owner device-inventory commitments, `canonical_auth_tree_v1`, current-auth HMAC, self-identity HMAC, and the deterministic client-message-ID HMAC. The owner inventory must predecessor-link an authenticated `phone-ready-v1` with exact `YEOMAN_ROTATION_PHONE_READY_V1` source content. Its exact canonical `YEOMAN_ROTATION_DEVICE_INVENTORY_V1` JSON source must equal the protected intended/no-unknown/count/unique-sorted-label fields and bind the same current-auth commitment. Swapped, missing, malformed, free-form, or semantically false variants fail closed.
- `phone-ready-v1` must predecessor-link the authenticated fixed-kind `auth-quarantine-receipt-v1`. That receipt has exactly `phase`, `nonce`, `serialization`, `old_auth_hmac`, and `artifact`; requires `quarantined`, a 32-lowercase-hex nonce, `canonical_auth_tree_v1`, a valid old-auth HMAC, and an `EvidenceCommitment` artifact; and must bind the same quarantine artifact as current-auth. The old and current auth HMACs must differ. Missing/wrong predecessors, failure phases, extra/malformed fields, swapped artifacts, or equal old/current identities fail closed.
- State no longer invents or requires a private `.rotation-observer-receipts` journal. It uses the common fixed-kind protected-record verifier; prep-03 owns observer/one-shot durability. Recursive exact schema/kind/predecessor/raw checks remain the authenticity boundary, and a valid same-kind protected close is accepted without state-private provenance.

### RED and GREEN evidence

- Correction baseline: `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py` passed 133 tests.
- Canonical/mode/inventory/rewind RED selection: 11 expected failures and 4 passes exposed the ASCII-escaping, normal-mode, inventory-validation, and consuming-FD gaps. After correction the 15 targeted tests passed, then the complete common/state gate passed 148 tests.
- Typed observer contract RED: 13 tests failed before the accepted prep-03 kinds and graph existed. After plan commit `417880a`, three additional account/time-order tests failed. GREEN passed 19 adversarial observer cases, 81 state tests, and 159 common/state tests.
- State nonce RED: four replay, cross-phase, crash-window, and concurrent-lock cases failed. GREEN passed all four targeted cases, then 85 state tests and 163 common/state tests.
- Expectation RED: the first fully bound valid graph failed while 15 tamper cases were already refused; predecessor RED then failed 2 of 17 cases; exact owner-source RED failed 1 of 21 cases; and the common-protected-record layering test failed 1 case while state still required its private journal. After each implementation step, the respective selections passed 16, 18, 21, and 1 tests. That stage covered 21 missing, wrong, swapped, malformed, unordered, duplicate, predecessor, and free-form-source variants plus the valid graph.
- Final quarantine-graph RED: the five missing/wrong predecessor, failure-phase, swapped-artifact, and same-old/current cases all returned `preserved` (`5 failed, 21 passed`). GREEN then passed those 26 cases plus the valid graph; the expanded exact-schema/value matrix passed all 31 tamper cases.
- Final gate: `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py` passed **249 tests in 12.15 seconds**.
- Final static gate: `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py` returned `All checks passed!`; `git diff --check` produced no output. The toolkit worktree was clean after commit `f17ebe05598bf28c5f8a0f32dbc3b1d9dbb957a1`.

### Restored mutation evidence

- Switched v1 canonicalization to literal UTF-8: the default-escaped non-ASCII fixture failed. Restored.
- Rejected all group/world bits (`0o077`) instead of only write bits (`0o022`): the `0755` directory / `0644` file acceptance test failed. Restored.
- Removed the ciphertext-FD rewind: `test_legacy_reader_rewinds_held_ciphertext_before_each_independent_decryption` failed after the first fake decrypt consumed the FD. Restored.
- Removed required directory closure from independent inventory validation: the first equal-but-invalid inventory case returned `preserved` and failed. Restored.
- Removed observer account equality: the cross-account event returned `preserved_with_classified_additions` and failed. Restored.
- Scoped the nonce reservation filename by record HMAC: `test_state_nonce_is_unique_across_phases_and_distinct_common_receipts` did not raise and failed. Restored.
- Removed exact canonical inventory source validation: the free-form signed owner-content case returned `preserved` and failed. Restored.
- Removed the old-auth/current-auth inequality check: the equal-identity quarantine graph returned `preserved` and `same-old-current-auth` failed. Restored.

### Exact no-live attestation for this correction round

Only toolkit source/tests, accepted source plans, this source report, disposable pytest directories, synthetic files, fake crypto/service results, and synthetic protected records were read or changed. No production wrapper/controller was invoked. No `/home/dm/.yeoman` runtime data, production evidence, production key, WhatsApp auth, service, process, socket, port, network, QR, linked device, message, archive, memory, or operator receipt was accessed, created, changed, deleted, or transmitted. No live command, incident transition, send, restart, or owner action occurred.
