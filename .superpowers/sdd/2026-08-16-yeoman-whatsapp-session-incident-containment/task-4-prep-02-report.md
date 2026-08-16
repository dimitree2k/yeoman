# Task 4 pre-owner v1-derived state-evidence report

## Status

Blocked before Task 1 RED. The implementer accessed no toolkit source beyond read-only contract inspection and made no toolkit edit, test run, or live call. The controller later performed the narrowly recorded evidence-metadata and public signature-envelope lookup below; no evidence content was decrypted and no runtime, key, auth, network, QR, message, service, socket, port, or production adapter was accessed or changed.

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
