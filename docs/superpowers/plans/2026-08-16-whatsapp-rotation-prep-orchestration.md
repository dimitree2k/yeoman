# WhatsApp Rotation Pre-owner Preparation Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and review the synthetic-only controllers required before the incident Task 4 owner rotation can be requested.

**Architecture:** Work only in `/home/dm/Documents/yeoman-migration-toolkit`, whose branch must retain `6ba55014242953e72ec7a29e75041f704452b885` as a required ancestor. Six incident sources include a tiny standard-library-only pre-import trust anchor, `bootstrap/first_gate_bootstrap.py`, plus five scripts sharing `incident_evidence_lib`. The bootstrap authenticates a root-owned authority, a signed immutable candidate, two signed time-bounded Luna decision envelopes, and a signed final release before loading three authenticated modules from held bytes; the worktree controller and runtime Git state are never authorization. Runtime authentication remains under `/home/dm/.yeoman/secrets/whatsapp-auth`, protected operator evidence is rooted at `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`, and release artifacts use `/home/dm/.local/share/yeoman-program-release/whatsapp-first-gate-v1`.

**Tech Stack:** Python 3 standard library, `age`, `ssh-keygen -Y`, existing `scripts/whatsapp_qr_reconnect.py`, `pytest`, Ruff.

## Global Constraints

- Production additions are only `bootstrap/first_gate_bootstrap.py`, `scripts/incident_evidence_lib.py`, `scripts/whatsapp_auth_quarantine.py`, `scripts/whatsapp_rotation_state.py`, `scripts/whatsapp_rotation_smoke.py`, and `scripts/whatsapp_rotation_first_gate.py`; tests are only under `tests/shared/`.
- Evidence root, auth root, recipient list, signer, allowed-signers file, and incident HMAC key are fixed constants. Provision the dedicated 32-byte incident HMAC key at fixed mode-`0600` path beneath a fixed `0700` program-key/hmac directory using `O_EXCL`, `getrandom`, and file/directory `fsync`; never derive it from signing or age keys. Synthetic tests inject fakes; production CLIs reject every root/path/key override.
- `EvidenceCommitment` is public-safe: `schema_version`, `record_hmac_sha256`, `ciphertext_sha256`, and `signature_sha256` only. Never expose a bare plaintext record SHA-256.
- JSON records may be bounded in memory. Streaming artifacts never create a plaintext file or FD-backed disk inode: they feed canonical bytes/tar-like records through bounded HMAC and `age` pipes; recipient verification streams decrypted bytes through HMAC/byte-count comparison and discards them. Build ciphertext, detached signature, and public metadata—but no plaintext—inside a private staging directory; verify ciphertext, both decryptions, and signature before public visibility, `fsync` it, then atomically rename it to a unique final artifact directory. Collision or any prepublication failure leaves no partial published artifact. Ciphertext staging may use `O_TMPFILE`/direct FD publication; unsupported primitives fail closed.
- These are incident migration tools, not permanent Gateway/Bridge architecture. Add their exact deletion, tests, documentation, and evidence-disposition boundary to the final legacy-free release inventory; delete them before final-release acceptance.
- Full quiescence is mandatory. The first standing Luna mechanism/procedure GO decisions authorize only preparation of an exact unsigned candidate draft for a separate owner prompt; they do not authorize installation, signing, or the live gate. The owner prompt must identify that draft's exact canonical SHA-256 and explicitly authorize privileged bootstrap/authority/key preparation plus signing exactly that candidate. Only after the owner approval is captured in the fixed signed approval envelope, the candidate is signed, both standing Luna sessions approve the exact signed candidate, decision envelopes and release are signed, and installed/artifact bytes are verified may the bootstrap stop/verify Bridge, Gateway, and Overseer all `inactive`/`failed`, prove Overseer cannot respawn them, and retain that state through v1 inspection and v2 baseline. The first gate then stops for the mandatory second evidence-bound Luna review. Keep Gateway/Overseer stopped throughout later owner gates; only the pinned Bridge-only QR helper and direct observer may run during relink/smoke, then stop Bridge again before post capture/compare. A baseline mismatch is NO-GO. `external_effect_unknown` is terminal and never retried or replayed; `capture_failed` is terminal and blocks incident close.
- After the installed first gate produces the real quiesced v1/v2 baseline, a mandatory second evidence-bound Luna GO authorizes four distinct host-backed owner turns in this order: `owner-all-devices-revoked-v1`, `owner-quarantine-authorized-v1`, `phone-ready-v1` after quarantine, and `owner-device-inventory-v1` after relink/fingerprint. No destructive transition proceeds without both standing reviewers rereading the exact owner reference and confirming statement/predecessor; this is host-API trust evidence, not cryptographic nonrepudiation.

## Component Map

| File | Responsibility |
| --- | --- |
| `bootstrap/first_gate_bootstrap.py` | Standard-library-only pre-import trust anchor; authenticates the root authority, signed candidate, both signed Luna decision envelopes, signed final release, fixed toolchain, and held module bytes before loading any toolkit code. |
| `scripts/incident_evidence_lib.py` | Fixed roots, private FD streaming artifacts, encrypt/sign/verify/publish, public HMAC commitments, protected owner-turn records. |
| `scripts/whatsapp_auth_quarantine.py` | Bridge-stop-verified old-auth quarantine, empty active auth, old/current identity HMACs. |
| `scripts/whatsapp_rotation_state.py` | v1-linked v2 adapters and non-destructive preservation comparator. |
| `scripts/whatsapp_rotation_smoke.py` | Host-backed inventory attestation, direct Bridge event observer, and one-attempt smoke receipts. |
| `scripts/whatsapp_rotation_first_gate.py` | Zero-argument, authenticated first-live-gate controller: it runs the fixed preflight and evidence lifecycle below, then stops for the second Luna review. |

## First-live-gate controller contract

This is a deliberately narrow incident controller, not a permanent runtime
module. Its only production invocation, byte/order exact, is:

```bash
/usr/bin/env -i LANG=C LC_ALL=C TZ=UTC PATH=/usr/bin:/bin /usr/bin/python3.13 -I -S -E -B /usr/local/libexec/yeoman/first_gate_bootstrap.py
```

The installed bootstrap must be root-owned regular mode `0755` at
`/usr/local/libexec/yeoman/first_gate_bootstrap.py`, non-writable by group and
world, with candidate source SHA-256 submitted for re-review
`95f5d30c71ee3607c97d884a965b6897450521dbfcba647531f6505e30894ae6`.
The root-owned non-writable authority is fixed at
`/etc/yeoman/first-gate-release-authority.json`; it pins signer identity,
allowed-signers content and hash, bootstrap path and hash, and all fixed
candidate, owner-approval, decision, and release paths. Both authority and
installed bootstrap must be root-owned regular files and non-writable by group
or world. The executable bootstrap additionally requires exact mode `0755`;
looser and stricter bootstrap modes both refuse. The release root is
`/home/dm/.local/share/yeoman-program-release/whatsapp-first-gate-v1` and
contains only the fixed names `candidate.json`/`candidate.sig`,
`agent_a-decision.json`/`agent_a-decision.sig`,
`agent_b-decision.json`/`agent_b-decision.sig`, and
`owner-approval.json`/`owner-approval.sig`, and `release.json`/`release.sig`.

No installed bootstrap, authority descriptor, release-signing key, candidate,
owner approval, decision, release, or signature was created during synthetic
correction. The approval order is strict: first fresh Terra acceptance and
standing Luna mechanism/procedure review of exact code/docs; prepare the exact
unsigned canonical candidate draft; then stop and ask the owner to approve the
draft's exact SHA-256 plus privileged bootstrap/authority/key preparation and
signing that exact candidate; only after approval perform that preparation and
sign the candidate and owner-approval envelopes; then both standing Luna
sessions review the exact signed candidate bytes/hash and return GO; capture
their exact outputs in signed, time-bounded decision envelopes; create the
final signed release; re-review any differing installed or artifact bytes;
only then may the byte-exact invocation run.

The bootstrap accepts no arguments or overrides and requires the exact sterile
environment and interpreter flags above. It validates that every fixed
executable is a root-owned regular `0755` file, is not group- or world-writable,
has the exact size and SHA-256 below, and is executed only through a held-FD
pinned path under the sterile environment. `/usr/bin/python3` is a symlink and
is not the authenticated launcher. Any mismatch is **NO-GO before mutation**.

| Executable | Size (bytes) | SHA-256 |
| --- | ---: | --- |
| `/usr/bin/env` | 68464 | `a1a366aeec990c18d7ff97358e523925e449e2df224029fe048fc3ae35a97720` |
| `/usr/bin/age` | 4162312 | `374f65bfbb3646f15f5b3296507c7860067915da27af187995db7b4fec5fc035` |
| `/usr/bin/age-keygen` | 2433984 | `859e2e6edbe0f5afe2a6e5c340f1f07969195de272888a303de2746902d46e5a` |
| `/usr/bin/ssh-keygen` | 592376 | `e80f38fc532ca57dd82879c4dd169ae17bf76cc11c3da9c037c7495234fbb9bd` |
| `/usr/bin/systemctl` | 331504 | `c418667a6fce4553f5faa61fd62f887787e7fc3d5ad5c2c4afff9d44ad09d475` |
| `/usr/bin/python3.13` | 6673720 | `5a8d634b3cf42fa618c2a39c7e674206cefc3b0be3d2f7023d5b1f8ebb51a013` |

The authorization is deliberately not one monolithic manifest. The signed
immutable candidate v1 binds all static executable and provenance facts: the
incident; exact source and toolkit commits; plan/package hashes; required
ancestor; installed bootstrap; invocation; closed module names, paths, sizes,
and hashes; and fixed toolchain. The distinct owner-approval v1 envelope binds
the incident, exact unsigned canonical candidate hash, fixed owner identity and
scope, `APPROVED`, exact approval text/hash, and an inclusive validity window
of at most 24 hours. Each signed decision v1 binds that exact candidate hash,
one standing role/session, fixed scope, `GO`, exact review text/hash, and the
same bounded inclusive validity rule. The signed release v3 binds exact hashes
of candidate, owner approval, and both decision envelopes plus a single-use
`authorization_id`. Candidate, owner approval, decisions, and release must all
be their exact canonical bytes; alternate JSON encodings refuse.

The four artifact classes use distinct SSH signature namespaces. Owner and
review signatures attest exact transcript capture in their respective
envelopes; Luna session IDs are trace/process evidence, not cryptographic
nonrepudiation.
The standing reviewer identities are:

- Agent A: `01a009b3-e740-7972-992b-5d63d6066b8c`
- Agent B: `01a009b3-f495-7a90-8e50-8a22d4d306d2`

The bootstrap loads no toolkit code before authentication. It enforces strict
environment, flags, arguments, and standard-library roots; performs bounded,
no-follow authority, candidate, decision, release, signature, module, and
tool reads; verifies every SSH signature from held descriptors; and
authenticates held executable identities. `/usr/bin/env` and
`/usr/bin/python3.13` are root-owned pathname launcher trust boundaries
verified after startup. Only later `ssh-keygen`, `age`, `age-keygen`, and
`systemctl` subprocesses execute through held FDs. Only
`incident_evidence_lib`, `whatsapp_rotation_state`, and
`whatsapp_rotation_first_gate` are loaded, from authenticated held bytes via
an in-memory finder. Direct execution of the worktree controller is always
NO-GO. Neither Git nor a dynamically discovered current head participates in
runtime authorization; strict schema checks also refuse booleans where integer
fields are required. The global execution permit and reusable loader are
absent: `main()` constructs one local permit and one local in-memory loader only
after exact argv/environment/process, authority, signature, module, and tool
authentication. The authenticated evidence and controller modules capture that
same per-execution permit during import; normal/direct imports cannot mint a
release or reach `_production_runtime`. This is an accidental/stale-entry
boundary, explicitly not a Python sandbox against same-process introspection or
a malicious owner, both outside the trusted-launcher threat model.

The release candidate must bind these candidate module hashes:

| Closed module | SHA-256 |
| --- | --- |
| `incident_evidence_lib` | `fddc847bd34fb87d6e68837fb9af62196e2039706402fbf96e8412aa322cd721` |
| `whatsapp_rotation_state` | `285a49cf5a99cce38d64a06a9419a8c6a08ee0f37ef767ca6f26b9c709d345c1` |
| `whatsapp_rotation_first_gate` | `ab80783d4ad4f648525f38a424d5ae455d1ba483838a8dc78cbd772d5d8b3883` |

After authenticating the signed candidate, signed owner approval, both signed
decisions, and signed release, the preflight v5 reconstructs exact candidate and
release hashes, carries bootstrap/owner/decision hashes and owner metadata, and
the controller durably reserves its single-use `authorization_id` in
a protected attempt before mutation. The only causal chain is: signed release
-> attempt -> attempt-linked `q1` -> provenance -> `q2` -> pre-v2 -> final
read-only receipt -> protected binding. Actual
`q1`/`q2`/final service inspection uses the held-FD pinned `systemctl` executor
under the sterile environment. Each phase has an exact allowlisted failure
prefix and a verified attempt-linked failure record; failure publication is
best effort, never erases the durable attempt or partial evidence, and never
permits continuation. Output contains safe commitments only. Success stops for
the mandatory second evidence-bound Luna review.

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

First run the synthetic-fake integration dry run above. Then package each companion task report, commits, diffs, tests, and mutation evidence for three scoped task reviews. Only after those accept, request the first Agent A/B mechanism/procedure review with explicit `gpt-5.6-luna`, exact commit range, reports/review packages, and program log. That first pair of GO decisions authorizes only preparation of the exact unsigned canonical candidate draft and an owner prompt naming its SHA-256. The owner must explicitly approve privileged bootstrap/authority/key preparation plus signing exactly that candidate. Capture that approval in the fixed signed owner-approval v1 envelope, perform only the approved preparation, sign the candidate, and have both standing Luna sessions review its exact signed bytes/hash. Only after both return GO may their exact outputs be signed as decision envelopes and release v3 be created. Verify and, if necessary, re-review resulting installed/artifact bytes before the byte-exact first-gate invocation. That gate performs only full quiescence and real read-only `inspect-v1`/`capture-v2`, then stops. Package its protected commitments for a mandatory second evidence-bound Agent A/B `gpt-5.6-luna` review. Its GO authorizes revocation turn, authorization turn, quarantine, phone-ready turn, pinned helper Bridge/owner-only-QR start while owner does not scan, direct observer readiness, then QR scan, fingerprint/inventory fourth turn, and one-shot smoke. Stop Bridge, post-capture/compare, then obtain a final Luna review. The strictest NO-GO governs every gate; append only accepted commitments and residual risk to the log.

## Execution Handoff

The next sequence after fresh Terra acceptance and both first Luna mechanism/procedure GO decisions is an exact unsigned candidate draft followed by a separate owner decision naming that draft hash and authorizing privileged bootstrap/authority/key preparation plus signing it—not a live first gate. After approved preparation and signed owner/candidate envelopes, both Luna sessions separately approve exact signed candidate bytes/hash; only then may signed decisions and release v3 be created. After verification and any required artifact re-review, the byte-exact bootstrap invocation may perform only full quiescence and read-only `inspect-v1`/`capture-v2`, then must stop. Owner turns begin only after the mandatory second evidence-bound Luna GO.
