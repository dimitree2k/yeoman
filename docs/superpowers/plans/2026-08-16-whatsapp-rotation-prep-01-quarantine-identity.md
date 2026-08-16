# WhatsApp Rotation Pre-owner Quarantine and Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a synthetic-tested controller that quarantines only the compromised old WhatsApp auth, leaves an empty private active auth directory, and proves the later new identity is different without disclosing either identity.

**Architecture:** The controller holds descriptor-relative auth FDs, stops and verifies Bridge before mutation, streams the old tree through the common protected-artifact API, and commits an atomic active-auth swap. It is operationally non-restorable—there is no restore command or automatic rollback—although separately authorized forensic decryption of the protected ciphertext remains possible outside this controller.

**Tech Stack:** Python 3 standard library, existing `scripts/whatsapp_qr_reconnect.py`, `scripts/incident_evidence_lib.py`, `pytest`, Ruff.

## Global Constraints

- The only production auth location is `/home/dm/.yeoman/secrets/whatsapp-auth`. Protected receipts/artifacts use only `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`; program keys stay in the existing program-key roots.
- No `allow_live` switch exists. The live CLI verifies separate predecessor-linked `owner-all-devices-revoked-v1` and `owner-quarantine-authorized-v1` records before any auth/Bridge action; generic approval, a combined checkbox, or CLI confirmation is invalid.
- Before mutation require the first-Luna-authorized quiescence receipt: Bridge, Gateway, and Overseer are each `inactive`/`failed`, and a second state check proves Overseer did not respawn anything. Keep Gateway/Overseer stopped; this controller may recheck/stop Bridge but the QR helper remains Bridge-only.
- Define one versioned `canonical_auth_tree_v1` serialization. Derive old-auth HMAC with the held incident key from those exact bytes streamed to encryption; use the identical serialization for the old-tree rewalk, current fingerprint, smoke-expectation binding, and immediately-pre-send auth recheck. Every use is a held-FD no-follow traversal with before/after identity and stability checks. Do not fingerprint the empty replacement.
- Stream the old held tree into `write_protected_artifact` through its bounded HMAC+`age` pipe; never create a plaintext tar, copy, backup directory, restore operation, plaintext disk inode, or named plaintext temporary.
- Stdout contains only public `EvidenceCommitment` HMAC/ciphertext/signature commitments and fixed phases; it contains no auth path, directory entry, identity, error detail, or plaintext hash.
- Choose transaction nonce before artifact streaming and derive its deterministic artifact identity. After protected artifact streaming and immediately before PREPARED journal/exchange, rewalk/re-HMAC the canonical auth tree and require equality to the artifact canonical HMAC plus identical root/path inode/stat and held descriptor identity. Crash/rerun inspects nonce artifact, journal, and exchanged identities: it may reuse a verified unchanged nonce artifact or record protected abandoned state, may finish cleanup after verified exchange, and never exchanges/quarantines twice.

---

### Task 1: Implement pre-commit quarantine with atomic auth swap

**Files:**
- Create: `scripts/whatsapp_auth_quarantine.py`
- Create: `tests/shared/test_whatsapp_auth_quarantine.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-01-report.md`

**Interfaces:**
- `quarantine_auth(revocation: EvidenceCommitment, authorization: EvidenceCommitment, quiescence: EvidenceCommitment) -> QuarantineResult`
- `QuarantineResult(phase: Literal["quarantined", "failed_pre_commit", "failed_post_commit"], receipt: EvidenceCommitment)`

- [ ] **Step 1: Write RED phase tests**

```python
def test_precommit_failure_preserves_old_auth_and_leaves_no_plaintext(tmp_path):
    result = quarantine_for_test(tmp_path, fail="second_decrypt")
    assert result.phase == "failed_pre_commit"
    assert original_auth_tree_is_present(tmp_path)
    assert plaintext_artifacts(tmp_path) == []
```

Add tests for quiescence/Overseer-respawn failure, tree change after artifact stream, root/path inode/stat change before journal, encryption/signature/decrypt failure, prepared-journal publication failure, and failed empty-auth prepare. If artifact is published but PREPARED journal fails, rerun must verify/reuse the unchanged nonce artifact or record protected abandoned state, never duplicate it. Every pre-commit failure preserves original auth. Add crash/rerun tests at prepared publication, exchange, parent fsync, recursive delete, and final receipt publication. Each post-exchange failure requires Bridge stopped, active auth empty/0700, protected error journal/receipt, cleanup-only rerun, and no success marker.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/shared/test_whatsapp_auth_quarantine.py`

Expected: FAIL because the controller is absent.

- [ ] **Step 3: Implement explicit phases**

```python
def quarantine_auth(revocation: EvidenceCommitment, authorization: EvidenceCommitment, quiescence: EvidenceCommitment) -> QuarantineResult:
    verify_separate_predecessor_linked_owner_records(revocation, authorization)
    verify_full_quiescence(quiescence)
    active = open_held_private_auth(AUTH_DIR)
    tx_nonce = choose_or_recover_nonce(active)
    artifact = write_or_reuse_nonce_artifact(tx_nonce, active, stream_canonical_auth_tree)
    require_rewalk_matches_artifact(active, artifact.canonical_hmac)
    replacement = prepare_empty_private_sibling(active.parent_fd)
    journal = publish_prepared_journal(tx_nonce, artifact.commitment, artifact.canonical_hmac, active, replacement)
    renameat2_exchange(active.name, replacement.name, active.parent_fd)
    fsync_parent_or_postcommit_failure(journal)
    return remove_old_tree_or_record_post_commit_failure(active, artifact, journal)
```

`prepare_empty_private_sibling` creates and revalidates a held-parent empty current-user `0700` sibling. Before exchange, publish/fsync a protected `PREPARED` transaction journal binding nonce, deterministic artifact identity/commitment, canonical old-auth HMAC, and active/replacement inode identities. `renameat2_exchange(..., RENAME_EXCHANGE)` atomically exchanges the held siblings; unsupported Linux/filesystem behavior fails precommit and preserves original auth. Immediately fsync the held parent; re-identify both names/held inodes so the exact sibling containing old auth is known before no-follow recursive removal. Fsync failure or post-exchange receipt failure is `failed_post_commit`: Bridge remains stopped, active auth remains empty, and rerun is cleanup-only after journal/identity inspection. Failure or proof that the exact sibling remains likewise never returns success. There is no restore API. The `quarantined` receipt binds phase, old-auth canonical HMAC, artifact commitment, and transaction nonce.

- [ ] **Step 4: Verify GREEN and mutation gates**

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py
uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py
```

Temporarily skip the stopped-state proof and prove its test fails. Temporarily report success after recursive deletion failure and prove the post-commit test fails. Restore both and rerun GREEN.

- [ ] **Step 5: Commit and report**

```bash
git add scripts/whatsapp_auth_quarantine.py tests/shared/test_whatsapp_auth_quarantine.py
git commit -m "feat(incident): add non-restorable auth quarantine"
```

Record only commands, counts, and public commitments in the task report.

### Task 2: Add post-relink current-identity comparison

**Files:**
- Modify: `scripts/whatsapp_auth_quarantine.py`
- Modify: `tests/shared/test_whatsapp_auth_quarantine.py`

**Interfaces:**
- `fingerprint_current(phone_ready: EvidenceCommitment) -> EvidenceCommitment`
- Consumes the protected quarantine receipt's old-auth HMAC/artifact binding and a verified post-quarantine `phone-ready-v1` owner turn; it never consumes the empty active directory as identity evidence.

- [ ] **Step 1: Write RED identity tests**

```python
def test_current_post_relink_fingerprint_differs_from_protected_old_fingerprint(tmp_path):
    old_auth_hmac, current_auth_hmac = decrypt_auth_fingerprints_for_test(tmp_path)
    assert old_auth_hmac != current_auth_hmac
```

Also assert an empty active auth is refused, equal protected auth HMAC is a failure, and a missing protected pre receipt is refused. Neither auth HMAC value appears in stdout; stdout exposes only the separate public record commitment.

- [ ] **Step 2: Implement and verify**

```python
def fingerprint_current(phone_ready: EvidenceCommitment) -> EvidenceCommitment:
    verify_phone_ready(phone_ready)
    current = open_held_private_auth(AUTH_DIR, require_nonempty=True)
    current_hmac = stable_canonical_auth_tree_v1_hmac(current.fd, INCIDENT_HMAC_KEY_FD)
    require_not_equal(current_hmac, load_quarantine_receipt().old_auth_hmac)
    return write_protected_record("current-auth-fingerprint-v1", {
        "serialization": "canonical_auth_tree_v1", "hmac": current_hmac,
    })
```

The protected old-auth receipt must carry the same `serialization` identifier. A changed/unstable current tree, or mismatched serialization identifier, fails before expectation construction or network I/O.

Run the focused module and Ruff, temporarily replace `require_not_equal` with a no-op and prove the equality test fails, restore it, then commit:

```bash
git add scripts/whatsapp_auth_quarantine.py tests/shared/test_whatsapp_auth_quarantine.py
git commit -m "feat(incident): verify rotated auth identity"
```

## Execution Handoff

Live `quarantine` remains prohibited until the pre-action owner gate is a freshly verified protected record. `fingerprint-current` is only after the separate phone-ready turn and relink.
