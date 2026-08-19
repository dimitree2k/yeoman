# WhatsApp Rotation Pre-owner Preparation Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and review the synthetic-only controllers required before the incident Task 4 owner rotation can be requested.

**Architecture:** Work only in `/home/dm/Documents/yeoman-migration-toolkit`, whose branch must start from and retain `6ba55014242953e72ec7a29e75041f704452b885` as a required ancestor, rather than pinning implementation to that forever-exact HEAD. Five incident scripts share a small `incident_evidence_lib`: runtime authentication remains under `/home/dm/.yeoman/secrets/whatsapp-auth`, while all protected operator evidence is rooted at `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`; program keys remain in their existing program-key roots.

**Tech Stack:** Python 3 standard library, `age`, `ssh-keygen -Y`, existing `scripts/whatsapp_qr_reconnect.py`, `pytest`, Ruff.

## Global Constraints

- Production additions are only `scripts/incident_evidence_lib.py`, `scripts/whatsapp_auth_quarantine.py`, `scripts/whatsapp_rotation_state.py`, `scripts/whatsapp_rotation_smoke.py`, and `scripts/whatsapp_rotation_first_gate.py`; tests are only under `tests/shared/`.
- Evidence root, auth root, recipient list, signer, allowed-signers file, and incident HMAC key are fixed constants. Provision the dedicated 32-byte incident HMAC key at fixed mode-`0600` path beneath a fixed `0700` program-key/hmac directory using `O_EXCL`, `getrandom`, and file/directory `fsync`; never derive it from signing or age keys. Synthetic tests inject fakes; production CLIs reject every root/path/key override.
- `EvidenceCommitment` is public-safe: `schema_version`, `record_hmac_sha256`, `ciphertext_sha256`, and `signature_sha256` only. Never expose a bare plaintext record SHA-256.
- JSON records may be bounded in memory. Streaming artifacts never create a plaintext file or FD-backed disk inode: they feed canonical bytes/tar-like records through bounded HMAC and `age` pipes; recipient verification streams decrypted bytes through HMAC/byte-count comparison and discards them. Build ciphertext, detached signature, and public metadata—but no plaintext—inside a private staging directory; verify ciphertext, both decryptions, and signature before public visibility, `fsync` it, then atomically rename it to a unique final artifact directory. Collision or any prepublication failure leaves no partial published artifact. Ciphertext staging may use `O_TMPFILE`/direct FD publication; unsupported primitives fail closed.
- These are incident migration tools, not permanent Gateway/Bridge architecture. Add their exact deletion, tests, documentation, and evidence-disposition boundary to the final legacy-free release inventory; delete them before final-release acceptance.
- Full quiescence is mandatory. First Luna code GO authorizes the controller to stop/verify Bridge, Gateway, and Overseer all `inactive`/`failed`, prove Overseer cannot respawn them, and retain that state through v1 inspection, v2 baseline, and owner gates. Keep Gateway/Overseer stopped throughout; only the pinned Bridge-only QR helper and direct observer may run during relink/smoke, then stop Bridge again before post capture/compare. A baseline mismatch is NO-GO. `external_effect_unknown` is terminal and never retried or replayed; `capture_failed` is terminal and blocks incident close.
- After the first Luna GO and real quiesced v1/v2 baseline, a mandatory second evidence-bound Luna GO authorizes four distinct host-backed owner turns in this order: `owner-all-devices-revoked-v1`, `owner-quarantine-authorized-v1`, `phone-ready-v1` after quarantine, and `owner-device-inventory-v1` after relink/fingerprint. No destructive transition proceeds without both standing reviewers rereading the exact owner reference and confirming statement/predecessor; this is host-API trust evidence, not cryptographic nonrepudiation.

## Component Map

| File | Responsibility |
| --- | --- |
| `scripts/incident_evidence_lib.py` | Fixed roots, private FD streaming artifacts, encrypt/sign/verify/publish, public HMAC commitments, protected owner-turn records. |
| `scripts/whatsapp_auth_quarantine.py` | Bridge-stop-verified old-auth quarantine, empty active auth, old/current identity HMACs. |
| `scripts/whatsapp_rotation_state.py` | v1-linked v2 adapters and non-destructive preservation comparator. |
| `scripts/whatsapp_rotation_smoke.py` | Host-backed inventory attestation, direct Bridge event observer, and one-attempt smoke receipts. |
| `scripts/whatsapp_rotation_first_gate.py` | Zero-argument, authenticated first-live-gate controller: it runs the fixed preflight and evidence lifecycle below, then stops for the second Luna review. |

## First-live-gate controller contract

This is a deliberately narrow incident controller, not a permanent runtime
module. Its only production invocation is:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C TZ=UTC /usr/bin/python3 /home/dm/Documents/yeoman-migration-toolkit/scripts/whatsapp_rotation_first_gate.py
```

It accepts **no arguments**, configuration overrides, alternate roots, or
environment-supplied paths. It constructs the same sanitized environment for
every child process. Before any mutation it authenticates the static
repository/controller contract and validates that every fixed executable is a
root-owned regular `0755` file, is not group- or world-writable, has the exact
size and SHA-256 below, and is invoked only by its fixed absolute path. Any
mismatch is a protected preflight **NO-GO** before quiescence or other
mutation.

| Executable | Size (bytes) | SHA-256 |
| --- | ---: | --- |
| `/usr/bin/age` | 4162312 | `374f65bfbb3646f15f5b3296507c7860067915da27af187995db7b4fec5fc035` |
| `/usr/bin/age-keygen` | 2433984 | `859e2e6edbe0f5afe2a6e5c340f1f07969195de272888a303de2746902d46e5a` |
| `/usr/bin/ssh-keygen` | 592376 | `e80f38fc532ca57dd82879c4dd169ae17bf76cc11c3da9c037c7495234fbb9bd` |
| `/usr/bin/systemctl` | 331504 | `c418667a6fce4553f5faa61fd62f887787e7fc3d5ad5c2c4afff9d44ad09d475` |
| `/usr/bin/git` | 4081272 | `a0e562e4bd3c4c79379e91d8c07a10104b2cefe8fac966dc6bd4874a57a807f3` |
| `/usr/bin/python3` | 6673720 | `5a8d634b3cf42fa618c2a39c7e674206cefc3b0be3d2f7023d5b1f8ebb51a013` |

After successful preflight, the controller writes a durable protected attempt
record that binds the incident/schema, a fresh burned nonce, source/toolkit
heads, controller identity, and fixed toolchain identities. Only then may it
run this single causal chain: `q1` full quiescence, v1 provenance, fresh `q2`,
pre-v2 capture, final read-only receipt, and protected binding. It retains
every partial artifact. Any later failure writes protected allowlisted failure
evidence linked to the attempt and available public commitments; a failure to
write that evidence is still NO-GO and never permits continuation. On success
it emits only the allowlisted public commitments and stops for the mandatory
second, evidence-bound Luna review.

This controller authorizes neither a later rotation nor any owner, revocation,
quarantine, QR/relink, observer, smoke, message, or other production action.
Persona-evolution is outside target behavior and historical artifacts remain
inert. Proactivity, consciousness, and speak-up are postponed but mandatory
later capabilities; they are not widened by this incident gate.

---

### Task 1: Establish the common protected-evidence contract

**Files:**
- Create: `scripts/incident_evidence_lib.py`
- Create: `tests/shared/test_incident_evidence_lib.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-orchestration-report.md`

**Interfaces:**
- `write_protected_record(kind: str, payload: Mapping[str, object]) -> EvidenceCommitment`
- `write_protected_artifact(kind: str, write_canonical: Callable[[BinaryIO], None]) -> EvidenceCommitment`
- `record_owner_turn(kind: str, fields: Mapping[str, object], source: HostUserTurn, predecessor: EvidenceCommitment | None) -> EvidenceCommitment`

- [ ] **Step 1: Write RED fixture tests**

```python
def test_record_encrypts_before_signing_and_public_commitment_has_no_plain_hash(tmp_path, capsys):
    commitment = write_protected_record("synthetic", {"code": "000000"})
    assert set(asdict(commitment)) == {"schema_version", "record_hmac_sha256", "ciphertext_sha256", "signature_sha256"}
    assert decrypt_both_and_verify_for_test(commitment) is True
    assert "000000" not in capsys.readouterr().out
```

Also cover a failed second decryption, failed signature check, plaintext residue, named temporary fallback, or untrusted root; every case must fail before publication.

Also require `HostUserTurn(thread_id, completed_turn_id, user_message_item_id, timestamp, exact_content)` for every owner record. The primary orchestrator obtains it from trusted `codex_app__read_thread`; the CLI accepts bounded JSON only over inherited private FD/stdin—not argv/environment—then HMACs/encrypts it. Tests reject a missing/duplicate/combined source. There is no opaque fallback: if the exact host event cannot be reread and matched, refuse. The predecessor is the separate explicit function argument, never duplicated in the source object.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/shared/test_incident_evidence_lib.py`

Expected: FAIL because the library does not exist.

- [ ] **Step 3: Implement minimal reusable APIs**

```python
@dataclass(frozen=True)
class EvidenceCommitment:
    schema_version: int
    record_hmac_sha256: str
    ciphertext_sha256: str
    signature_sha256: str

def write_protected_artifact(kind: str, write_canonical: Callable[[BinaryIO], None]) -> EvidenceCommitment:
    return stream_hmac_encrypt_verify_publish(kind, write_canonical)
```

Use one `age` invocation with both recipients, sign only its ciphertext, and stream each recipient decryption through HMAC/byte-count comparison before discarding it; then verify with `ssh-keygen -Y verify`. Derive `record_hmac_sha256` from canonical bytes with the held incident key FD. `write_protected_record` serializes bounded canonical JSON through this artifact API. Emit only a fixed phase plus the public commitment.

- [ ] **Step 4: Verify GREEN, mutate, and commit**

Run:

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py
uv run ruff check scripts/incident_evidence_lib.py tests/shared/test_incident_evidence_lib.py
```

Temporarily sign plaintext rather than ciphertext and prove the test fails; restore it. Commit only this task:

```bash
git add scripts/incident_evidence_lib.py tests/shared/test_incident_evidence_lib.py
git commit -m "feat(incident): add protected evidence library"
```

### Task 2: Run the synthetic integration dry run, then reviews

**Files:**
- Create: `tests/shared/test_whatsapp_rotation_integration.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-integration-review-package.md`
- Modify: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-orchestration-report.md`

**Produces:** synthetic-fake proof of full quiescence -> v1/v2 baseline -> separate owner records -> crash-safe quarantine -> observer -> smoke -> post compare; it invokes neither real systemctl, QR, Bridge, Gateway, nor runtime paths.

- [ ] **Step 1: Write the integration test**

```python
def test_synthetic_rotation_chain_preserves_state_and_never_retries(tmp_path):
    result = run_synthetic_rotation_chain(tmp_path)
    assert result["comparison"] == "preserved_with_classified_additions"
    assert result["ambiguous_send_retries"] == 0
    assert result["live_calls"] == 0
    assert result["pre_owner_order"] == ["modules", "integration", "terra_reviews", "first_luna_code_go", "quiesced_v1_v2", "second_luna_evidence_go", "owner_turns", "quarantine", "observer_ready", "smoke", "post_compare", "final_luna_go"]
```

- [ ] **Step 2: Run the complete synthetic suite**

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_state.py scripts/whatsapp_rotation_smoke.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
```

Expected: synthetic-only GREEN.

- [ ] **Step 3: Apply integration mutation and commit**

Temporarily add a second ambiguous-send attempt; the integration test must fail. Restore it, rerun GREEN, then commit:

```bash
git add tests/shared/test_whatsapp_rotation_integration.py
git commit -m "test(incident): cover rotation preparation integration"
```

- [ ] **Step 4: Review in the required order**

First run the synthetic-fake integration dry run above. Then package each companion task report, commits, diffs, tests, and mutation evidence for three scoped task reviews. Only after those accept, request the first Agent A/B code review with explicit `gpt-5.6-luna`, exact commit range, reports/review packages, and program log; its GO authorizes only full quiescence and real read-only `inspect-v1`/`capture-v2`. Package real quiescence/baseline commitments for a mandatory second evidence-bound Agent A/B `gpt-5.6-luna` review. Its GO authorizes revocation turn, authorization turn, quarantine, phone-ready turn, pinned helper Bridge/owner-only-QR start while owner does not scan, direct observer readiness, then QR scan, fingerprint/inventory fourth turn, and one-shot smoke. Stop Bridge, post-capture/compare, then obtain a final Luna review. The strictest NO-GO governs every gate; append only accepted commitments and residual risk to the log.

## Execution Handoff

The next live sequence after first Luna code GO is full quiescence then read-only `inspect-v1`/`capture-v2`; owner turns begin only after the mandatory second evidence-bound Luna GO.
