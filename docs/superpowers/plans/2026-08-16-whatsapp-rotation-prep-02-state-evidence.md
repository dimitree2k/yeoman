# WhatsApp Rotation Pre-owner State Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a v1-derived v2 baseline and comparator that covers every preserved entry byte-for-byte and classifies growth only in the separately protected observer-evidence domain.

**Architecture:** This controller reuses the accepted common full-quiescence adapter/receipt before any baseline read. Its read-only `inspect-v1` gate requires that receipt, decrypts/verifies the fixed legacy v1 artifact under protected no-output conditions, validates its exact external serialization SHA-256 against `6ae6476466c728bbb423be16182b6341e47e38c0d55073f9831751452346f833`, and derives all v2 scope from v1's only facts: relative path, mode, size, mtime, and SHA-256. The legacy v1 artifact predates `EvidenceCommitment`; a narrow common-library compatibility reader authenticates its source-pinned location, ciphertext hash, detached-signature hash, signer/namespace, both age decryptions, identical bounded plaintext, and plaintext hash. It does not fabricate a new-format record HMAC or trust the adjacent commitments file. Every v1-scoped file—including SQLite databases, WAL/SHM/journal sidecars, JSONL, JSON, Markdown, media, and opaque historical files—uses one immutable exact-byte adapter. Nothing parses or opens SQLite/JSONL semantically. Any path-set or byte/metadata difference is a baseline/post mismatch; only the separately rooted authenticated observer-evidence chain may grow.

**Tech Stack:** Python 3 standard library, `scripts/incident_evidence_lib.py`, `pytest`, Ruff.

## Global Constraints

- Auth is always excluded. `inspect-v1` rejects auth entries, absolute/escaping paths, symlinks, non-regular files, duplicates, metadata mismatch, and scope outside v1's allowed roots: runtime data, workspace sessions, inert historical workspace persona-evolution state, and policy audit.
- Protected operator evidence, including the separate incident receipt chain, is only `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`. Public output is phase, public HMAC/ciphertext/signature commitments, and aggregate counts only.
- Reverify v1 signature and both recipient decryptions before capture, despite its exact serialization staying external. V2 stores the explicitly named legacy plaintext SHA-256, verified-v1 status, encrypted exact inventory, and becomes the live protected pre/post baseline.
- V1 inspection and both complete state captures run only under full verified quiescence of Bridge/Gateway/Overseer. Every v1 entry and every current entry beneath the four roots is covered. A changed, missing, renamed, reordered, truncated, or added path/file is `mismatch`. Baseline mismatch is NO-GO.
- Runtime data is expected unchanged because Gateway stays stopped. Expected smoke/unsolicited inbound classifications live only in the separate protected direct-observer evidence chain; expected smoke receipt records are the protected evidence HMAC chain, not a guessed runtime receipt file. No raw event is deleted.

## Fixed Legacy-v1 Descriptor

The implementation must add a narrow, test-injectable legacy reader to `incident_evidence_lib.py`; `_read_protected_record()` is intentionally not used because v1 predates the HMAC-framed protected-record format. Production wiring accepts no caller override and pins all of the following in source:

- private directory: `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16`;
- ciphertext: `pre-rotation-state-manifest.json.age`, exact size 51,823 bytes, SHA-256 `0f94535433f4d833c1c7e51b512369314b407a5e6c88cf6b1330f98172c4448d`;
- detached signature: `pre-rotation-state-manifest.json.sig`, exact size 294 bytes, SHA-256 `8f75bb199a977e43861fb7ba5c2a8fc395ba70bbc0f2d8e55224ebf5bdd81621`;
- plaintext external serialization SHA-256: `6ae6476466c728bbb423be16182b6341e47e38c0d55073f9831751452346f833`;
- signer identity: `yeoman-preservation-2026-08-16`; SSH signature namespace: `git`; signature target: the exact decrypted plaintext bytes;
- the two fixed `AGE_IDENTITIES` and fixed `ALLOWED_SIGNERS` already held by the common library.

Freeze all new-format constants, `_ProductionCrypto`, `_read_protected_record()`, and ciphertext-signature behavior unchanged. Add a separate legacy crypto protocol/production implementation with fixed `LEGACY_V1_SIGNATURE_NAMESPACE = "git"`; it verifies the exact plaintext, never ciphertext. The public production legacy reader accepts no root, locator, hash, signer, namespace, identity, crypto, or `EvidenceCommitment` argument; dependency injection exists only behind a clearly named test-only core.

Walk every absolute directory component with held no-follow directory descriptors. Require the evidence directory to be an exact owner-UID `0700` directory and both artifacts to be owner-UID regular `0600` files with the exact sizes above. Hash the held artifact FDs and pass those same FDs—or byte-identical verified anonymous `O_TMPFILE`/memfd copies—to age and SSH; never reopen a name after hashing. Apply cumulative source constants `LEGACY_V1_MAX_CIPHERTEXT_BYTES = 1 << 20`, `LEGACY_V1_MAX_SIGNATURE_BYTES = 4096`, and `LEGACY_V1_MAX_PLAINTEXT_BYTES = 1 << 20`; reject artifact `st_size` before hashing/spawn and stop a child on cumulative output overflow. Open two distinct fixed identity FDs, derive and compare their recipients with the two fixed distinct `AGE_RECIPIENTS`, and reject duplicate/substituted identities before decrypting.

Decrypt independently with both fixed identities, require byte-for-byte equality and the exact plaintext hash, then verify the detached signature over those exact plaintext bytes with the fixed signer and legacy namespace. Bound child time, input/output, stderr, descriptor lifecycle, terminate/kill/reap paths, and never surface plaintext, paths, subprocess output, or exception text. Parse only after every proof succeeds. The adjacent legacy `.commitments` file is informational only and must not authorize or replace any source-pinned fact. Tests inject disposable artifacts/crypto through the test-only core and cover wrong location/name, symlink/type/mode/owner/size, replacement after hash, ciphertext/signature/plaintext hash, one-recipient failure, unequal/duplicate/substituted identities or decryptions, wrong signer/namespace/target, new-format namespace/ciphertext signing, every cumulative cap, timeout/nonzero/child cleanup, FD cleanup, output leakage, and a substituted commitments file. No production wrapper is invoked before the first Luna code GO.

The authenticated plaintext has this exact external schema and no extra keys: top level `{manifest_version: 1, scope: "pre-whatsapp-session-rotation-nonauth-state", file_count: 248, byte_count: 228519348, files: [...]}`. Every file object has exactly `{root, relative_path, mode, size, mtime_ns, sha256}`. `root` is one of `runtime_data`, `workspace_sessions`, `workspace_persona_evolution`, or `policy_audit`; `relative_path` is a normalized nonempty relative POSIX path with no `.`/`..`, absolute, NUL, or escaping form; integer fields reject booleans and invalid ranges; SHA-256 is lowercase hexadecimal; `(root, relative_path)` is unique; `file_count` equals list length; and `byte_count` equals the checked sum of entry sizes. The plaintext hash authenticates the original compact sorted-key JSON plus terminal newline; signature verification is over those original bytes, never parsed/re-serialized JSON. `workspace_persona_evolution` is preserved only as historical inert state; it does not restore persona-evolution behavior to the target architecture.

## V1-Driven Exact Scope and Evidence Inventory

| Domain | Explicit v2 adapter | Required check |
| --- | --- | --- |
| every regular file declared by v1 | `immutable_exact_v1` | exact path set, file type, mode, size, `mtime_ns`, and SHA-256 under quiescence |
| protected incident evidence receipt (separate from v1 scope) | evidence-chain adapter | prior receipt HMAC chain is ordered prefix/classified growth |

The production walker pins this exact source mapping and exposes no caller override: `runtime_data -> /home/dm/.yeoman/data`, `workspace_sessions -> /home/dm/.yeoman/workspace/sessions`, `workspace_persona_evolution -> /home/dm/.yeoman/workspace/persona-evolution`, and `policy_audit -> /home/dm/.yeoman/policy/audit`. It walks sorted names descriptor-relatively, rejects symlinks and every non-regular/non-directory object, rejects hard-linked files or repeated `(st_dev, st_ino)`, and requires owner-UID private roots/files. Every filename must decode as strict UTF-8 and already equal Unicode NFC; the collision key is `(root_label, NFC(relative_posix_path))`, with NUL, dot segments, absolute/escape forms, and duplicate keys rejected.

The v1 file list derives the exact required directory-prefix set, including each fixed root. Baseline enumeration rejects any missing/renamed directory and any extra directory, including an empty one. It captures root and per-directory type/mode/UID/GID/link/`mtime_ns` facts into v2 and post requires their exact equality. It enumerates the complete current directory and file path sets, not merely v1 file names. For each file it compares held-FD `fstat` before/after streaming SHA-256, then performs a second complete enumeration/hash pass and requires the same directory/file sets and facts before publication. A test-only core injects disposable roots; production never accepts roots/walkers/hashes.

V2 adds current file type, UID, GID, link count, and root/directory security facts to encrypted evidence and requires those v2 facts unchanged post-rotation; the legacy v1 does not retroactively prove metadata it never recorded. SQLite databases and every `-wal`, `-shm`, or `-journal` file are ordinary exact entries. No SQLite/JSONL connection, parser, normalization, checkpoint, backup, or reconstruction is allowed. This deliberately preserves opaque and unversioned historical schemas more strongly than a logical adapter could. If stopping a writer checkpoints, removes, or adds a sidecar, the pre-owner baseline is a mismatch and the incident stops for investigation.

The protected incident observer chain is compared separately and is never represented as a v1-declared runtime entry. Its production comparator accepts only authenticated `EvidenceCommitment` inputs for fixed protected kinds and verifies the ordered HMAC/predecessor chain plus allowed causal classifications; plain decoded records are test-core only. Inventory is encrypted evidence; public output reports fixed phase, status, commitments, and aggregate counts only.

---

### Task 1: Add read-only v1 inspection and v2 capture

**Files:**
- Modify: `scripts/incident_evidence_lib.py`
- Create: `scripts/whatsapp_rotation_state.py`
- Modify: `tests/shared/test_incident_evidence_lib.py`
- Create: `tests/shared/test_whatsapp_rotation_state.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-02-report.md`

**Interfaces:**
- `quiesce_and_record_pre() -> EvidenceCommitment`
- `inspect_v1(pre_quiescence: EvidenceCommitment) -> V1Scope`
- `capture_pre_v2(scope: V1Scope, pre_quiescence: EvidenceCommitment) -> EvidenceCommitment`
- `quiesce_and_record_post(pre: EvidenceCommitment) -> EvidenceCommitment`
- `capture_post_v2(scope: V1Scope, post_quiescence: EvidenceCommitment, pre: EvidenceCommitment) -> EvidenceCommitment`

- [ ] **Step 1: Write RED fixture-v1 tests**

```python
def test_inspect_v1_derives_exact_scope_and_rejects_guessed_paths(tmp_path):
    scope = inspect_v1_for_test(fixture_v1(tmp_path))
    assert scope.legacy_v1_plaintext_sha256 == LEGACY_V1_PLAINTEXT_SHA256
    assert scope.allowed_roots == {"runtime_data", "workspace_sessions", "workspace_persona_evolution", "policy_audit"}
    assert all(entry.adapter == "immutable_exact_v1" for entry in scope.entries)
```

Test reuse of the common synthetic fixed-service stop/state/PID/socket/port and bounded fake Overseer-respawn receipt, v1 SHA/signature/decrypt failure, incomplete quiescence, auth/path escape/symlink/nonregular/hard-link entry, duplicate inode/path, Unicode/path normalization collision, changed/missing/added file, DB/WAL/SHM/journal as ordinary exact files, full-root second-pass drift, and public-output silence. Assert paths/content never reach stdout.

Add common-library RED tests for the complete Fixed Legacy-v1 Descriptor boundary above. The synthetic signature target is the exact decrypted plaintext, never the ciphertext or parsed/re-serialized JSON. Assert the public production wrapper has no path, root, identity, signer, namespace, commitment, or crypto override.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/shared/test_whatsapp_rotation_state.py`

Expected: FAIL because the controller is absent.

- [ ] **Step 3: Implement read-only inspection and capture**

```python
def inspect_v1(pre_quiescence: EvidenceCommitment) -> V1Scope:
    verify_state_quiescence_pre(pre_quiescence)
    plain = decrypt_verify_external_v1_without_output()
    require_exact_serialization_sha256(plain, LEGACY_V1_PLAINTEXT_SHA256)
    return parse_and_validate_v1_scope(plain)

def capture_pre_v2(scope: V1Scope, pre_quiescence: EvidenceCommitment) -> EvidenceCommitment:
    verify_state_quiescence_pre(pre_quiescence)
    inventory = capture_two_pass_exact_inventory(scope)
    return write_state_inventory("whatsapp-rotation-state-pre-v2", {
        "phase": "pre", "quiescence": asdict(pre_quiescence),
        "legacy_v1_plaintext_sha256": LEGACY_V1_PLAINTEXT_SHA256,
        "legacy_v1_verified": True, "exact_inventory": inventory,
    })
```

Add a state-specific full-quiescence wrapper without changing the accepted quarantine adapter: stop exactly Overseer, Bridge, then Gateway with fixed absolute systemctl commands and bounded terminate/kill/reap behavior, then reuse the accepted two-sample unit/PID/socket/port verifier and dedicated common receipt. `quiesce_and_record_pre()` and `quiesce_and_record_post(pre)` each create a fresh internal random nonce and phase-bound protected record of fixed kind `whatsapp-rotation-state-quiescence-pre-v1` or `...-post-v1`; post also binds the exact pre-inventory predecessor. A dedicated crash-safe `.state-receipts` journal binds phase, nonce, common quiescence commitment, predecessor, and final commitment so a generic protected record cannot substitute. Production exposes no phase/nonce/kind/predecessor override.

`inspect_v1` and each capture refuse without the exact authenticated phase receipt. Open all four fixed roots component-by-component and perform the exhaustive two-pass descriptor-relative inventory above. Require the exact current directory/file path sets to equal v1-derived scope and every held file to match v1 mode/size/`mtime_ns`/SHA before recording additional v2 security facts. Any difference is NO-GO. Never open SQLite, parse JSONL, infer logical rows, or classify changes inside v1 roots. Pre inventory uses fixed kind `whatsapp-rotation-state-pre-v2`; post uses `whatsapp-rotation-state-post-v2`, binds both its fresh post receipt and the exact pre inventory, and is independently journaled. Tests reject cached/same nonces or commitments, replay, swapped phases, wrong kinds, generic records, wrong predecessors, and decoded dictionaries.

- [ ] **Step 4: Verify GREEN, mutate, and commit**

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py
uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py
```

Temporarily omit v1 serialization verification and prove RED coverage fails. Temporarily omit unknown-file/path-set rejection and prove RED coverage fails. Restore both and commit:

```bash
git add scripts/incident_evidence_lib.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): inspect v1 rotation scope"
```

### Task 2: Compare all old entries and classify only supported additions

**Files:**
- Modify: `scripts/whatsapp_rotation_state.py`
- Modify: `tests/shared/test_whatsapp_rotation_state.py`

**Interfaces:**
- `compare_v2(pre: EvidenceCommitment, post: EvidenceCommitment, observer: EvidenceCommitment) -> ComparisonResult`
- Status is `preserved`, `preserved_with_classified_additions`, or `mismatch`.

- [ ] **Step 1: Write RED preservation tests**

```python
def test_every_v1_file_is_exact_and_only_observer_chain_grows():
    result = compare_v2(pre_manifest(), post_manifest(), observer_evidence())
    assert result.status == "preserved"
    assert observer_evidence().classifications == {"expected_inbound_reply": 1, "unsolicited_inbound": 1}
```

Test changed/missing/reordered/truncated JSONL bytes, changed SQLite/database bytes, missing/added WAL/SHM/journal, any immutable file change, path/type/mode/UID/GID/link/mtime drift, new runtime file, receipt HMAC/predecessor/order/classification drift, and any unclassified observer growth. Each is `mismatch` and retains encrypted evidence.

- [ ] **Step 2: Implement exact comparator**

First authenticate and load the exact fixed pre/post protected kinds and their dedicated journals. Require distinct commitments/nonces, correct phase order, fresh independently authenticated quiescence receipts, post-to-pre predecessor binding, the same legacy plaintext SHA and verified-v1 flag, then exact directory/file entry-set and per-entry fact equality. Reject same/swapped commitments, wrong kinds, generic protected records, and plain decoded inputs. Any addition inside a v1 root is mismatch. Only the fixed protected `whatsapp-rotation-observer-chain-v1` kind permits ordered-prefix/classified growth. Direct observer evidence, not runtime DBs, classifies expected quote causality by `replyToMessageId == accepted messageId` and unsolicited inbound. Never delete, normalize, open, rewrite, or replay a v1-root file.

- [ ] **Step 3: Verify, mutate, commit, and report**

Run focused tests and Ruff. Temporarily omit one old exact entry from comparison and prove RED coverage fails; temporarily accept one new root file and prove RED coverage fails. Restore both, rerun GREEN, then commit:

```bash
git add scripts/whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): reconcile v1-derived rotation state"
```

Record encrypted adapter inventory and public counts only for integration/review.

## Execution Handoff

After implementation/synthetic reviews and the first Luna code GO, run `quiesce_and_record_pre()` -> read-only `inspect_v1(pre_receipt)` -> `capture_pre_v2(scope, pre_receipt)`. Package the exact pre receipt/inventory commitments for the mandatory second evidence-bound Luna GO before any owner turn. After authorized owner/relink/smoke work, run `quiesce_and_record_post(pre_inventory)` -> `capture_post_v2(scope, post_receipt, pre_inventory)` -> `compare_v2(pre_inventory, post_inventory, observer_chain)`, then obtain the final Luna GO. Any mismatch or wrong/replayed/swapped commitment is terminal for incident close and never authorizes deletion, restoration, retry, or replay.
