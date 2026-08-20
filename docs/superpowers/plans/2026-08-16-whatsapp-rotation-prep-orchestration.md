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
| `scripts/whatsapp_auth_quarantine.py` | Pure/test-injected old-auth quarantine core, empty active auth, and old/current identity HMACs; no ordinary-import fixed-root production runtime. |
| `scripts/whatsapp_rotation_state.py` | v1-linked v2 adapters and non-destructive preservation comparator. |
| `scripts/whatsapp_rotation_smoke.py` | Pure/test-injected inventory, observer, and one-attempt smoke state machine; no ordinary-import production transport, credential reader, or sender. |
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
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`.
The root-owned non-writable authority is fixed at
`/etc/yeoman/first-gate-release-authority.json`. Authority schema v4 requires
it to be root-owned regular exact mode `0644`; it pins one canonical Ed25519
release signer and one owner signer, each with separately hashed
allowed-signers content and a distinct decoded public-key blob. Candidate,
decisions, and release use the release signer; owner approval uses the owner
signer and its own namespace. The installed bootstrap is root-owned regular
exact mode `0755`; looser and stricter bootstrap modes both refuse. The release root is
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

The authorization is deliberately not one monolithic manifest. Authority v4 is
the root descriptor for the two signer records, fixed paths, and bootstrap
identity. The signed
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

The four artifact classes use distinct SSH signature namespaces. The release
signer signs candidate, both decisions, and release; the owner signer signs
owner approval. Owner and review signatures attest exact transcript capture in their respective
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
authentication. The authenticated evidence, state, and controller modules
capture that same per-execution permit during import; state production runtime,
service quiescence, and legacy-manifest reading require it. Evidence fixed-root
configuration and descendants, raw directory/file helpers, legacy test-core
composition, pinned tool execution, and production crypto require that exact
permit at every constructor/operation; a test config cannot select either fixed
production evidence path or descendant without it. State test runtime and raw
root helpers cannot select fixed production roots or descendants without it.
Quarantine and smoke contain no dormant
fixed-root runtime, production transport, credential reader, observer launcher,
or Bridge sender. Their legacy public production wrappers refuse before any
effect. Later phases therefore require a future authenticated controller
capability. This is an
accidental/stale-entry boundary, explicitly not a Python sandbox against
same-process introspection or a malicious owner, both outside the
trusted-launcher threat model.

The release candidate must bind these candidate module hashes:

| Closed module | SHA-256 |
| --- | --- |
| `incident_evidence_lib` | `8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9` |
| `whatsapp_rotation_state` | `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16` |
| `whatsapp_rotation_first_gate` | `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e` |

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

Historical record: bb37 was **REJECTED**, not accepted. Its descendant-only capability design lost `_Dir` path/permit provenance across ancestor composition, exposed legacy/key/age/signing reads, returned a composable raw state parent FD, dropped the authenticated journal permit before q1, admitted fixed auth/evidence/key overlaps in quarantine, and overclaimed the resulting boundary. Its tests and hashes are historical evidence only.

The current clean toolkit head is `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`, reviewing `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83` (full correction range `f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`). Symmetric normalized separator-safe overlap refuses both ancestors and descendants. Evidence `_Dir` carries path/permit, exposes no `.fd`, and authorizes every effect-specific transition before it touches the filesystem; only `_authenticated_fd` with the exact captured permit supports production crypto. State uses an opaque path+permit `_RootHandle`; journal helpers carry `config.permit`; quarantine refuses every fixed-target overlap while sibling-prefix fixtures remain allowed. Same-process introspection and a malicious owner remain outside this stale-entry boundary. No stale production adapters remain.

Parent evidence at this head: state `150 passed in 55.10s`; exact seven-file suite `445 passed in 77.47s`; six-file Python 3.13 compile, full scoped Ruff, clean diff/status, required-ancestor, no newly added skips/retired coverage, and adapter scans all pass. Bootstrap retains two pre-existing conditional `pytest.skip` cases for unavailable system `ssh-keygen -Y` support; this is not a global zero-skip claim. No production artifact or action exists; services remain offline.

The required next sequence is fresh Terra review, standing Luna mechanism review, an exact unsigned candidate draft, and an explicit owner prompt naming that hash and authorizing two-key privileged bootstrap/authority/key preparation plus signing exactly that candidate—not a live first gate. Then capture signed candidate and owner approval; obtain both Luna reviews of the exact signed candidate; create decisions/release; reverify differing bytes; and invoke only if every gate remains GO. Owner turns begin only after the mandatory second evidence-bound Luna GO. Persona evolution is omitted; proactivity, consciousness, and speak-up remain mandatory later capabilities.

## Execution Handoff Reconciliation — 2026-08-20

This section supersedes the prior `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`
current-successor handoff. That head is historical **REJECT**: fresh Terra
security session `01a01dfa-4f0f-7f61-bb31-f52e6daa5046` found that
`_HeldAuth.parent_fd` and `.fd` enabled ordinary-import composition from an
allowed sibling into fixed WhatsApp auth. Parallel Terra specification session
`01a01dfa-4f22-7500-b1e3-2df4834d1af7` returned GO, but the strictest security
REJECT governs. Its `445` seven-file and `150` state results, plus the earlier
production hashes, are historical evidence only.

The current clean toolkit successor is
`02720889cd4088dfae16f991fe19da460e5effb1`, parent
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83`. Review
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83..02720889cd4088dfae16f991fe19da460e5effb1`
and full range
`f55a5e28cfabc5010aa84031bd699aa8e4054645..02720889cd4088dfae16f991fe19da460e5effb1`.
`_HeldAuth` descriptors and target name are name-mangled/opaque; there is no
global FD registry, getter, or raw directory-descriptor API. Its bounded
effects validate exact `.auth-quarantine-<32hex>` sibling names, require
same-parent exchange with internal name swap, and give recursive children owned
parent descriptors with guarded `dup`/`fstat` cleanup. The current quarantine
SHA-256 is `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
the other production hashes are unchanged from the historical table.

Implementation design re-review returned GO with no findings, but fresh
exact-head independent Terra specification/security acceptance remains pending.
Luna is therefore blocked and the gate is **CLOSED**. Root verification at the
successor recorded `448 passed in 83.62s` for the seven-file suite and `150
passed in 63.78s` for state; six-file Python 3.13 compile, full scoped Ruff,
diff, required-ancestor, and no-new-skip checks passed. Two pre-existing
conditional system-`ssh-keygen` skips remain. No key, install, signing,
authority, candidate, approval, release, runtime, auth, message, artifact, or
live action occurred; services remain offline. Persona evolution remains
omitted, while proactivity, consciousness, and speak-up remain mandatory later.

## Superseding exact-head cleanup reconciliation — 2026-08-20

This section supersedes each preceding current-successor handoff while
preserving it as historical evidence. The clean toolkit successor is
`d7485311d52de206470b4f61c2c8eb3b613ee2ba`, parent
`02720889cd4088dfae16f991fe19da460e5effb1`; the exact focused range is
`02720889cd4088dfae16f991fe19da460e5effb1..d7485311d52de206470b4f61c2c8eb3b613ee2ba`
and the full ancestor range is
`f55a5e28cfabc5010aa84031bd699aa8e4054645..d7485311d52de206470b4f61c2c8eb3b613ee2ba`.

The total/idempotent cleanup contract is that `_ProductionCrypto.close()`
detaches `_owned`, clears `_material`, attempts every detached descriptor in
order, catches only a per-descriptor `OSError`, and makes a later `close()`
attempt no descriptor again. The focused RED was `1 failed, 106 deselected in
0.73s`; focused GREEN was `1 passed, 106 deselected in 0.28s`. Implementer
commit `d7485311d52de206470b4f61c2c8eb3b613ee2ba` is
`fix(evidence): make crypto cleanup idempotent`. Independent Terra review is
spec PASS and quality APPROVED, with no Critical or Important finding.

Controller evidence at the successor is root seven-file `449 passed in
78.06s` and state `150 passed in 54.81s`; six-file Python 3.13 compile, full
scoped Ruff, range-diff, required-ancestor, no-new-skip, clean-status, and
`git diff --check` all passed. Current production hashes are: bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
`45bfb8953f2174b52d109a833a38e7709f382872e83ac0281735e7086a6fd741`;
quarantine
`b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`; state
`5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16`; smoke
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller
`b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.

Unsigned checkpoint candidate
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79` is obsolete
and was never approved, signed, or installed because the mandatory
authenticated-module repair changed its bytes. The gate remains **CLOSED** and
offline: no live action occurred; QR residual remains a hard gate; persona
evolution remains omitted; proactivity, consciousness, and speak-up remain
mandatory later capabilities.

Next, in exact order: fresh Terra specification/security review of the clean
code/docs successors; record their result in a docs-only successor; standing
Luna A/B exact-head review; construct and verify one new unsigned candidate;
then request owner approval for that exact new hash. Do not put a plan/package
self-hash inside either self-hashed file. After commit, the controller will
calculate them and record them in a separate docs-only successor.
