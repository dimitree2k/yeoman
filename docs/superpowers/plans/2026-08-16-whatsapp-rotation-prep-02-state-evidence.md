# WhatsApp Rotation Pre-owner State Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a v1-derived v2 baseline and comparator that covers every preserved entry, retains old logical rows exactly, and classifies only causally supported growth.

**Architecture:** This controller owns `quiesce_and_record()` before any baseline read. Its read-only `inspect-v1` gate requires that receipt, decrypts/verifies the fixed v1 artifact under protected no-output conditions, validates its exact external serialization SHA-256 against `6ae6476466c728bbb423be16182b6341e47e38c0d55073f9831751452346f833`, and derives all v2 scope from v1's only facts: relative path, mode, size, mtime, and SHA-256. A source-defined fixed profile registry keyed by exact v1 path—not v1 metadata—then selects adapters only after each live file proves byte-identical to those v1 facts.

**Tech Stack:** Python 3 standard library, `scripts/incident_evidence_lib.py`, `pytest`, Ruff.

## Global Constraints

- Auth is always excluded. `inspect-v1` rejects auth entries, absolute/escaping paths, symlinks, non-regular files, duplicates, metadata mismatch, and scope outside v1's allowed roots: runtime data, workspace sessions, retained persona/proactivity historical state, and policy audit.
- Protected operator evidence, including the separate incident receipt chain, is only `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`. Public output is phase, public HMAC/ciphertext/signature commitments, and aggregate counts only.
- Reverify v1 signature and both recipient decryptions before capture, despite its exact serialization staying external. V2 stores v1 commitment, verified-v1 status, encrypted adapter inventory, and becomes the live protected pre/post baseline.
- V1 inspection and SQLite baseline capture run only under full verified quiescence of Bridge/Gateway/Overseer. Every v1 entry remains covered. Changed/missing/reordered/truncated old entries are `mismatch`. Baseline mismatch is NO-GO.
- Runtime data is expected unchanged because Gateway stays stopped. Expected smoke/unsolicited inbound classifications live only in the separate protected direct-observer evidence chain; expected smoke receipt records are the protected evidence HMAC chain, not a guessed runtime receipt file. No raw event is deleted.

## V1-Driven Scope and Adapter Inventory

| V1-declared entry type | Explicit v2 adapter | Required check |
| --- | --- | --- |
| JSONL regular file | ordered line-digest sequence | exact sequence equality while runtime is quiesced |
| supported SQLite snapshot profile | logical-table profile with fixed `PRAGMA user_version`, `PRAGMA table_info`, SELECT columns and stable unique key tuple | every old key maps to identical canonical row digest |
| other regular file | immutable exact-file adapter | mode, size, mtime, and content hash remain exact |
| protected incident evidence receipt (separate from v1 scope) | evidence-chain adapter | prior receipt HMAC chain is ordered prefix/classified growth |

The source-defined fixed profile registry selects a SQLite profile by exact v1 path and contains the supported schema version, table, SELECT columns, and stable unique key tuple. V1 itself supplies none of those facts. WAL/SHM are explicit held-file members of that database snapshot profile, with stable pre/post hashes around each logical read; they are not independent mutable exceptions. An unlisted table, duplicate/missing/changed key, changed `user_version`/columns, unstable held-file hash, or file with no adapter fails closed. The protected incident receipt chain is compared separately and is never represented as a v1-declared runtime entry. Adapter inventory is encrypted evidence; public output reports counts by adapter/status only.

---

### Task 1: Add read-only v1 inspection and v2 capture

**Files:**
- Create: `scripts/whatsapp_rotation_state.py`
- Create: `tests/shared/test_whatsapp_rotation_state.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-02-report.md`

**Interfaces:**
- `quiesce_and_record() -> EvidenceCommitment`
- `inspect_v1(quiescence: EvidenceCommitment) -> V1Scope`
- `capture_v2(scope: V1Scope) -> EvidenceCommitment`

- [ ] **Step 1: Write RED fixture-v1 tests**

```python
def test_inspect_v1_derives_exact_scope_and_rejects_guessed_paths(tmp_path):
    scope = inspect_v1_for_test(fixture_v1(tmp_path))
    assert scope.commitment == V1_COMMITMENT
    assert scope.allowed_roots == {"runtime_data", "workspace_sessions", "retained_history_proactivity", "policy_audit"}
    assert all(entry.adapter in {"jsonl_prefix", "sqlite_logical", "immutable_exact"} for entry in scope.entries)
```

Test synthetic fixed-service stop/state/PID/socket/port checks, bounded fake Overseer-respawn detection, v1 SHA/signature/decrypt failure, incomplete quiescence, auth/path escape/symlink/nonregular entry, unknown mutable file, unprofiled SQLite table, and WAL/SHM absent from a declared snapshot profile. Assert paths/content never reach stdout.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/shared/test_whatsapp_rotation_state.py`

Expected: FAIL because the controller is absent.

- [ ] **Step 3: Implement read-only inspection and capture**

```python
def inspect_v1(quiescence: EvidenceCommitment) -> V1Scope:
    verify_full_quiescence(quiescence)
    plain = decrypt_verify_external_v1_without_output()
    require_exact_serialization_sha256(plain, V1_COMMITMENT)
    return parse_and_validate_v1_scope(plain)

def capture_v2(scope: V1Scope) -> EvidenceCommitment:
    require_every_v1_file_byte_identical(scope)
    inventory = [capture_entry(entry) for entry in scope.entries]
    return write_protected_record("whatsapp-rotation-state-v2", {
        "prior_v1_commitment": V1_COMMITMENT, "verified_v1": True, "adapter_inventory": inventory,
    })
```

Implement `quiesce_and_record()` with the fixed user service list Bridge/Gateway/Overseer: `systemctl --user stop`, allowlisted state/PID/socket/port checks, and a bounded repeated respawn check proving Overseer did not revive anything. It never enable/disables units and writes a protected receipt. Synthetic tests use fakes until first Luna GO. `inspect_v1` refuses without that receipt. Verify full quiescence before deriving one JSONL or SQLite row digest. Open every declared source descriptor-relatively and prove it byte-identical to v1's path/mode/size/mtime/SHA facts. Any changed v1 file is NO-GO; never infer old SQLite rows from it. Only then does SQLite use the source-defined fixed registry profile/read-only held-FD connection and stable key/digest map; JSONL streams ordered rows; other regular files use immutable exact comparison. For each SQLite snapshot profile, hash every held DB/WAL/SHM member before and after the logical read and require stability.

- [ ] **Step 4: Verify GREEN, mutate, and commit**

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py
uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_state.py
```

Temporarily omit v1 serialization verification and prove RED coverage fails. Temporarily accept unprofiled WAL and prove RED coverage fails. Restore both and commit:

```bash
git add scripts/whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): inspect v1 rotation scope"
```

### Task 2: Compare all old entries and classify only supported additions

**Files:**
- Modify: `scripts/whatsapp_rotation_state.py`
- Modify: `tests/shared/test_whatsapp_rotation_state.py`

**Interfaces:**
- `compare_v2(pre: ProtectedManifest, post: ProtectedManifest, expected: SmokeExpectation) -> ComparisonResult`
- Status is `preserved`, `preserved_with_classified_additions`, or `mismatch`.

- [ ] **Step 1: Write RED preservation tests**

```python
def test_old_sqlite_key_digest_map_and_runtime_are_exactly_unchanged():
    result = compare_v2(pre_manifest(), post_manifest(), observer_evidence())
    assert result.status == "preserved"
    assert observer_evidence().classifications == {"expected_inbound_reply": 1, "unsolicited_inbound": 1}
```

Test changed/missing/reordered/truncated old JSONL line, duplicate/missing/changed SQLite key/digest, immutable file, receipt HMAC, unprofiled table, unstable WAL/SHM, schema drift, and any runtime key/line growth. Each is `mismatch` and retains encrypted evidence.

- [ ] **Step 2: Implement exact comparator**

```python
def capture_sqlite_rows(rows: Iterable[Row]) -> dict[KeyTuple, str]:
    result: dict[KeyTuple, str] = {}
    for row in rows:
        key, digest = stable_key(row), canonical_row_digest(row)
        if key in result:
            raise RuntimeError("operation failed")
        result[key] = digest
    return result

def compare_sqlite(old: Mapping[KeyTuple, str], new: Mapping[KeyTuple, str]) -> bool:
    return set(new) == set(old) and all(new[key] == old[key] for key in old)
```

First prove every v1 entry is present under the same adapter/profile. Immutable adapters, runtime JSONL, and SQLite profiles require exact equality under quiescence; SQLite rejects duplicates before map construction and requires exact key-set/per-key digest equality. Only the separate protected evidence chain permits ordered-prefix/classified growth. Direct observer evidence, not runtime DBs, classifies expected quote causality by `replyToMessageId == accepted messageId` and unsolicited inbound. Never delete/normalize rows.

- [ ] **Step 3: Verify, mutate, commit, and report**

Run focused tests and Ruff. Temporarily omit a SQLite old key from exact comparison and prove RED coverage fails; temporarily accept unknown mutable file and prove RED coverage fails. Restore both, rerun GREEN, then commit:

```bash
git add scripts/whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): reconcile v1-derived rotation state"
```

Record encrypted adapter inventory and public counts only for integration/review.

## Execution Handoff

After implementation/synthetic reviews and first Luna GO, run full `quiesce_and_record`, then read-only `inspect-v1`/`capture-v2`; their commitments require mandatory second Luna evidence-bound GO before any owner turn. Mismatch never authorizes deletion, restoration, or replay.
