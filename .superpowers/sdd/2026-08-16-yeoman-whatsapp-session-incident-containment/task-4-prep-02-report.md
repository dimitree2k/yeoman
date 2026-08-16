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

## Evidence

- Toolkit branch: `c/yeoman-migration-toolkit`, clean at inspection; required ancestor `6ba55014242953e72ec7a29e75041f704452b885` is present.
- Read-only contract inspection: `scripts/incident_evidence_lib.py` and the accepted Task 4 common/quarantine report.
- No RED/GREEN/mutation command was run because no valid interface exists to write a behaviorally correct RED test.
