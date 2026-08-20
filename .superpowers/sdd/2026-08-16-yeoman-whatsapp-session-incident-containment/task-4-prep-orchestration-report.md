# Task 4 preparation — common evidence library

## Scope

Implemented only the synthetic-tested common protected-evidence primitive in
the migration toolkit. No runtime, authentication state, services, messages,
databases, QR flow, network endpoint, program evidence, or program key was
read or modified.

## Contract covered

- A dedicated 32-byte incident HMAC key is provisioned with exclusive create,
  current-user `0600` permissions, directory `0700` checks, and file/directory
  fsync; provision failures remove the incomplete key.
- Canonical JSON is bounded. Arbitrary artifact writers stream bounded chunks
  directly into the encryption session while calculating the HMAC and byte
  count; no plaintext file or disk-backed plaintext descriptor is created.
- One encryption session receives both fixed recipients. Ciphertext is signed,
  each recipient is independently decrypted into HMAC/byte-count discard, and
  the detached signature is verified before a private staged artifact is
  atomically published with `renameat2(..., RENAME_NOREPLACE)` and fsync.
- Public commitment has exactly the version and three HMAC/SHA-256 fields; no
  bare plaintext hash is persisted or emitted by the library.
- Owner records require a structurally valid exact host turn and reserve a
  crash-recoverable HMAC-indexed source journal. A matching retry resumes or
  finalizes a verified artifact; a changed fact for the same source is refused.
  The predecessor remains an explicit argument.

## Security-review hardening

- Fixed controller recipients are now the two reviewed public recipient
  identities. Production identity, signing-key, and allowed-signers inputs are
  opened through held no-follow descriptors; their derived age public keys are
  compared before encryption without outputting them.
- All evidence, staging, artifact, metadata, key, journal, cleanup, and rename
  operations are descriptor-relative. Subprocess file inputs use held
  `/proc/self/fd` references with `pass_fds`; staging cleanup does not call
  path-based recursive deletion.
- Writes handle short writes. Ciphertext, signature, metadata, and both stage
  and artifact directories are fsynced at their commit boundaries. Evidence
  writes never provision a missing HMAC key; only the explicit provision
  operation can create it.
- Every artifact starts with a fixed version/magic and length-prefixed kind
  frame, included in the encrypted HMAC commitment. Equal payloads in distinct
  domains therefore cannot share an artifact identity.
- Age and SSH subprocesses have bounded waits and terminate/kill/reap cleanup;
  an early abandoned plaintext-consumer generator also reaps its child.

## Evidence

- Toolkit commit: `9dfaa70190e94404c3b49e2032f938284b73be19`.
- Follow-up hardening commit: `729d74393e9f6dd30a14e828e3c8e740e384c86f`.
- Recovery-gap hardening commit: `de462af3dc1fbbbcdd212029374f0c343fb00baf`.
- RED: focused suite failed because the module did not exist.
- Review-fix RED: the expanded suite failed against the original lifecycle
  implementation for the missing explicit-key, framed-domain, journal,
  descriptor, public-recipient, and bounded-process behavior.
- GREEN: `uv run pytest -q tests/shared/test_incident_evidence_lib.py` — 18
  passed.
- Lint: `uv run ruff check scripts/incident_evidence_lib.py
  tests/shared/test_incident_evidence_lib.py` — passed.
- Disposable CLI proof: temporary age identities plus a temporary SSH signing
  key and allowed-signers file exercised the real subprocess adapter. No real
  program key or identity was read.
- Mutation: temporarily removed the domain frame. The focused suite failed in
  the domain parser, collision, and retry expectations; the frame was restored
  before the final GREEN/lint run.
- Recovery mutation: temporarily disabled the held owner-source lock. The
  concurrent-pending test then failed; the lock was restored before final
  verification.

## Recovery-gap review closure

- Every key-provision failure path now closes and unlinks the exact held key
  entry before a best-effort parent fsync; synthetic `getrandom`, short-write,
  and fsync faults leave no partial key.
- Incremental production-material opens roll back prior descriptors. Encryption
  sessions terminate/reap and close output exactly once on construction,
  BrokenPipe, stdin-close, and wait-failure paths. Public-key derivation uses a
  bounded nonblocking accumulator rather than `communicate`.
- A post-rename artifact-directory fsync fault deliberately leaves the visible
  artifact uncommitted. Generic callers fail; a matching owner retry must fsync
  the held artifacts directory and then re-verify the complete artifact before
  finalizing its journal.
- Owner journals now treat only `ENOENT` as absent, take a held nonblocking
  source lock, and reject malformed/unsafe/oversized state. Public owner calls
  close owned crypto exactly once; injected/borrowed crypto remains caller-owned.

## Explicit limitations / next gates

- The only real subprocess exercise used disposable temporary keys. The fixed
  production public recipients are embedded, but production key provisioning,
  evidence-root use, and any runtime action remain unperformed and require the
  later owner/reviewer gate.
- `HostUserTurn` is structural evidence only. The orchestrator must reread the
  trusted host event and bind it to an authenticated owner turn; this library
  does not claim cryptographic owner authentication.
- Quarantine, state comparison, inventory attestation, and smoke execution are
  intentionally not implemented by this task.

## Independent acceptance

- The same Terra security reviewer re-read the complete final implementation
  and tests at toolkit commit
  `de462af3dc1fbbbcdd212029374f0c343fb00baf`; it returned **ACCEPT** with no
  Critical or Important findings.
- Fresh controller verification repeated the focused suite (18 passed), Ruff,
  and `git diff --check` from the committed toolkit tree. The toolkit worktree
  was clean and no live runtime, program key, evidence root, service, or
  authentication boundary was accessed.

## Full-bundle synthetic acceptance

The later prep-01, prep-02, and prep-03 work is accepted as one synthetic
pre-owner bundle.  Accepted plan hashes are prep-01
`0a668c0a5aea847beca23e62359cc480903f6b18d2c548e39c714160bd8267c6`, prep-02
`600396a3ed9510003b48401dcb24cc6acd34f3b96b10afb191dcd71ce57f4e2a`, and
prep-03 `6074344d5f1c6e6d6f1a3d40fab82983ee34825002a74fb224bcf415184abe30`.

Terra accepted common/quarantine at `68839a3`, state evidence at `f17ebe0`,
and final prep-03 Tasks 1-3 at `b788608` with SPEC ACCEPT and QUALITY ACCEPT,
no findings.  The integration/task review accepted the full bundle at
`995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`, again with SPEC ACCEPT and
QUALITY ACCEPT, no findings.

At that head, the exact five-file synthetic suite passed `339 passed in
16.98s`; exact scoped Ruff passed; `git diff --check` and toolkit status were
clean.  Integration and all prep work used disposable synthetic fakes only.
No production controller/wrapper, runtime service, auth/evidence/key path,
socket, port, network, QR, JID, message, archive, memory, or owner turn was
invoked.  Runtime remains offline.

This does not authorize the incident.  The next gate is the standing Luna
major-turn code review.  Its first GO may authorize only full quiescence and
real read-only `inspect-v1`/`capture-v2`; a second evidence-bound Luna GO is
required before every owner, quarantine, QR, relink, observer, or smoke action.

## First Luna review correction and durable re-handoff

The initial Luna package range was
`6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.
Agent A offered only a narrow conditional GO for quiescence/read-only
inspect/capture and required a fixed bounded controller plus a fresh
capture-bound q2/final read-only check.  Agent B returned the governing NO-GO,
requiring trusted-ancestor validation and authenticated `VerifiedV1Scope` and
artifact provenance.  Both also found the stale local recovery-report
reference; the report is actually in the separate prep-03 SDD directory at
`../2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`.
No live action occurred.

The ordered correction chain ends at toolkit
`62acf5b73ca9b9abaed79004deed7f93ef2280b3`.  It closes every held-ancestor FD
fail-safe; commits the exact all-field legacy descriptor; authenticates
`VerifiedV1Scope`; adds dedicated causal q2/final receipts and durable
q1-to-provenance-to-q2-to-pre-to-final binding; requires verifier checks at
controller and second-Luna handoff; preserves inventory/pre/post/comparator
exact provenance and semantic-quiescence continuity; and makes public compare
fail closed.  Mutations were run and restored across the correction rounds.

Fresh controller verification at `62acf5b` recorded 376 passing tests (split
285 + 91 only by the task-runner time boundary), relevant Ruff clean,
`git diff --check` clean, and a clean toolkit worktree.  Final Terra SPEC
ACCEPT and QUALITY ACCEPT had no Critical, Important, or Minor finding.  No
live/systemctl/socket/key/v1/owner/QR/quarantine/observer/smoke production call
occurred.  Runtime remains offline/disabled and the first live gate is
**CLOSED** until both same standing Luna sessions re-review corrected range
`6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`.
Their strictest decision governs.  See `task-4-prep-luna-code-review-package.md`
as the sole re-review handoff and `task-4-first-luna-review-response.md` for
the durable initial-decision/correction record.

## Historical f55 authenticated-bootstrap correction

The corrected toolkit range is
`62acf5b73ca9b9abaed79004deed7f93ef2280b3..f55a5e28cfabc5010aa84031bd699aa8e4054645`.
Fresh Terra review of source `48ca1a93957c85385ea2bfd995b9f3491fe9799f`
and toolkit `c4bc00a77ade352c24610d152cdeede67da9ec53` rejected two remaining
boundaries: SPEC required exact installed-bootstrap mode `0755`; SECURITY found
public release/direct-import runtime reachability and procedural-only owner
approval.

Production correction `960168a` requires the executable bootstrap to be
root-owned regular exact `0755` and the non-executable JSON authority to be a
root-owned regular file non-writable by group or world; deletes global
permit/loader state; creates both locally inside `main()` only after exact
process/signature/module/tool authentication; and makes evidence/controller
imports capture the same per-execution permit so normal imports fail before
production runtime. This is an accidental/stale-entry fence, not a same-process
or malicious-owner sandbox.

Owner approval v1 has fixed paths and its own namespace. It binds incident,
exact unsigned canonical candidate hash, fixed owner/scope, `APPROVED`, exact
text/hash, and inclusive validity no longer than 24 hours. Candidate, owner
approval, decisions, and release must be exact canonical bytes. Release v3
binds candidate, owner-approval, and both decision hashes plus single-use
authorization. Preflight v5 carries bootstrap/decision/owner hashes and owner
metadata and reconstructs canonical candidate/release hashes.

An initial aggregate claim at `960168a` missed nine stale test fixtures. Parent
verification exposed `450 passed, 9 failed`; test-only `f55a5e2` centralizes the
canonical v5 builder. Final parent verification at clean `f55a5e28` passed the
exact seven-file suite: `459 passed in 60.93s`; Python 3.13 `py_compile`, scoped
Ruff, `git diff --check`, and status passed/clean. Bootstrap SHA-256 is
`95f5d30c71ee3607c97d884a965b6897450521dbfcba647531f6505e30894ae6`;
module hashes are `fddc847bd34fb87d6e68837fb9af62196e2039706402fbf96e8412aa322cd721`,
`285a49cf5a99cce38d64a06a9419a8c6a08ee0f37ef767ca6f26b9c709d345c1`,
and `ab80783d4ad4f648525f38a424d5ae455d1ba483838a8dc78cbd772d5d8b3883`.

No bootstrap, authority, release key, candidate, owner approval, decision,
release, or signature was installed or created. Required order: fresh Terra and
standing-Luna mechanism review; exact unsigned candidate draft; owner prompt
naming its hash and approving privileged prep plus signing that candidate;
signed owner/candidate envelopes; both Luna reviews of exact signed candidate;
signed decisions/release; differing-byte verification/re-review; invocation.
No live/runtime/systemctl/key/owner/QR/message operation occurred. This f55
record and its hashes are historical only.

## Historical eb272 production-capability correction

Fresh Terra rejected source `1b18fba1b427e0069d80c8e0a583500d90e1cea0` with
toolkit `f55a5e28cfabc5010aa84031bd699aa8e4054645`: executable authority was
accepted, but ordinary imports still exposed production actions. Toolkit
`eb272c52965a7d2dd1a4c66eaa40c8e499079f7a` corrected the authority/two-signer
contract and parent verification passed `472 passed in 61.11s`. Fresh Terra
then rejected source `41b2915684311010a6c96061afeb2bef1ab86dd3` with toolkit
eb272 because normally callable underscored evidence/state helpers and
quarantine/smoke production adapters remained. A leading underscore is not a
capability boundary. The eb272 count and hashes are historical only.

## Historical a950 strict capability correction

Toolkit `a9504c233a14f92a12b4807855e94e621e8aa4ce` physically removes the
quarantine fixed-root production runtime and smoke production Bridge client,
observer transport/launcher, credential readers, expectation/send paths, and
lease registry. It guards the advertised production config/tool/crypto and
exact state runtime paths and removes arbitrary subprocess capture helpers.
Pure/test-injected later-phase cores remain for a future authenticated
controller capability.

Parent exact seven-file verification at clean a950 passed `433 passed in
56.67s`. `/usr/bin/python3.13` compiled all six production files; scoped Ruff,
`git diff --check`, clean status, required-ancestor, and zero skipped/retired
test scans passed. The count reduction is physical deletion of obsolete
adapter tests, not skipped coverage. Production hashes: bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
`15069931dc07771a45435b9490ea06e4d5db30798dc62bfe1b61a8786d29b016`;
quarantine `01306f100e86864748206c65672309dab89e2d215ce6fa058fdca916d6688618`;
state `0a5573ed619689450fd9b6116ea4097f8ec6ecfe5ed30f697747bc04c87c13ed`;
smoke `74652a00b5ace59f0b36d3c47a2f5fb630727e5462503ec3f7e2136a96794030`;
controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.
Exact-head Terra returned **REJECT** despite the green suite: fixed legacy
descriptor plus attacker test crypto exposed key/data FDs, raw evidence/key
open helpers were ungated, and exact-only checks missed evidence/state
descendants. a950 is historical only. No production artifact or action exists.

## Historical bb37 raw-root and descendant rejection

Toolkit `276105f8d2145ea8facab1692f49ad220cfc2783` permit-gates fixed evidence,
legacy, HMAC, age-identity, signing, allowed-signers, and state roots plus
descendants at config, raw directory/file/root helper, legacy-core, and
inventory boundaries. Authenticated operations propagate the same captured
permit. Direct tests monkeypatch `os.open` and prove refusal before effects.
Toolkit `bb37b577fc191084be74dff72a87b84ac9cf08ab` made containment normalized,
separator-safe, and sibling-prefix resistant while restoring bounded runtime.

Historical parent state verification passed `148 passed in 45.70s`; the exact seven-file
suite passed `440 passed in 65.41s`. Python 3.13 compile, scoped Ruff,
`git diff --check`, clean status, required-ancestor, and historical no-new skipped/retired
scans passed. Production hashes: bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
`7ff18521a8af2de78382d1d233a3ef707438727f9e6a26d63b4501fbaba0513e`;
quarantine `01306f100e86864748206c65672309dab89e2d215ce6fa058fdca916d6688618`;
state `2a6beab7699da11caf327a5cd332d98b0a28c84f8d887394b2856f67379d9071`;
smoke `74652a00b5ace59f0b36d3c47a2f5fb630727e5462503ec3f7e2136a96794030`;
controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.
No production artifact or action exists.

Historical next-order proposal: fresh Terra -> standing Luna mechanism review -> exact
unsigned draft -> explicit owner prompt naming its hash and authorizing two-key
privileged preparation/signing -> signed candidate/owner approval -> exact
signed-candidate Luna reviews -> decisions/release -> reverify -> invoke.
Persona evolution is omitted; proactivity, consciousness, and speak-up remain
mandatory later capabilities. Final review must bind the current toolkit
successor and the successor source documentation commit.

## Current successor handoff

bb37 is a historical **REJECT**, not the current capability boundary. The
review proved Critical ancestor `_Dir` path/permit loss that enabled
legacy/key/age/signing reads; state exposed a composable raw parent FD; and
Important authenticated journal opens dropped the permit and deadlocked q1.
Quarantine accepted fixed descendants and ancestors, and the documents
overclaimed the boundary.

The current clean toolkit is `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`, with
review range `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83` and full range
`f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
Symmetric normalized separator-safe overlap rejects protected ancestors and
descendants. `_Dir` carries path/permit provenance, exposes no `.fd`, and
authorizes effect-specific transitions before effects; only
`_authenticated_fd(exact captured permit)` supports production crypto. State
uses opaque path+permit `_RootHandle`, all journal opens propagate
`config.permit`, and quarantine rejects any fixed-target overlap while sibling
prefix fixtures remain allowed. Same-process introspection and a malicious
owner remain out of scope. No stale production adapters remain.

| Current production source | SHA-256 |
| --- | --- |
| `bootstrap/first_gate_bootstrap.py` | `ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd` |
| `scripts/incident_evidence_lib.py` | `8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9` |
| `scripts/whatsapp_auth_quarantine.py` | `14df959da264d7580f3bc78a5bce693284e917bb799fddb35bd506e787297dcb` |
| `scripts/whatsapp_rotation_state.py` | `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16` |
| `scripts/whatsapp_rotation_smoke.py` | `4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5` |
| `scripts/whatsapp_rotation_first_gate.py` | `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e` |

Parent verification at the exact current head: state `150 passed in 55.10s`;
exact seven-file suite `445 passed in 77.47s`; six-file Python 3.13 compile,
full scoped Ruff, clean diff/status, required ancestor, no newly added
skips/retired coverage, and adapter scans all pass. Bootstrap retains two
pre-existing conditional `pytest.skip` cases for unavailable system
`ssh-keygen -Y` support; do not describe this as globally zero skips.

Current authoritative document hashes, recorded here rather than in the
self-hashed plan/package: orchestration plan
`21c864f97b599b9a0dfd0660521d198d82c791bd60aca851ae02caf7947fe63c`; Luna
package `fa0c154d6c14f125c681c75c6994d49552f2a3e132778316d123fb12ce27fb7c`.
The final review binds the successor source documentation commit.

The gate remains **CLOSED**. No installation, key, authority, candidate, owner
approval, decision, release, signature, runtime, auth, message, or artifact
action occurred; services remain offline. Fresh Terra and standing Luna A/B
exact-head mechanism reviews remain pending. Persona evolution is omitted;
proactivity, consciousness, and speak-up remain mandatory later capabilities.

## Superseding current handoff — 2026-08-20

Toolkit `2967e451d10d83c0a07ff4ed54dad7a556b6bf83` is now historical
**REJECT**. Fresh Terra security session `01a01dfa-4f0f-7f61-bb31-f52e6daa5046`
found `_HeldAuth.parent_fd`/`.fd` ordinary-import composition from an allowed
sibling into fixed WhatsApp auth. Parallel Terra specification session
`01a01dfa-4f22-7500-b1e3-2df4834d1af7` returned GO, but the strictest security
REJECT governs. The former `445` seven-file and `150` state results and their
hashes are historical.

The current clean successor is `02720889cd4088dfae16f991fe19da460e5effb1`,
parent `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`; review
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83..02720889cd4088dfae16f991fe19da460e5effb1`
and full `f55a5e28cfabc5010aa84031bd699aa8e4054645..02720889cd4088dfae16f991fe19da460e5effb1`.
`_HeldAuth` descriptors and target name are name-mangled/opaque, with no global
FD registry, getter, or raw directory-descriptor API. Bounded effects require
exact `.auth-quarantine-<32hex>` siblings, same-parent exchange/internal name
swap, and owned recursive-child parent FDs with `dup`/`fstat` cleanup
regressions. Quarantine SHA-256 is
`b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`; other
production hashes are unchanged.

Root evidence is seven-file `448 passed in 83.62s`, state `150 passed in
63.78s`, six-file Python 3.13 compile, full scoped Ruff, diff, required
ancestor, and no-new-skip checks; two pre-existing conditional system
`ssh-keygen` skips remain. Design re-review gave GO/no findings, but fresh
exact-head independent Terra specification/security acceptance remains pending.
Luna is blocked and the gate is **CLOSED**. No key, install, signing,
authority, candidate, approval, release, runtime, auth, message, artifact, or
live action occurred; services remain offline. Persona evolution is omitted;
proactivity, consciousness, and speak-up remain mandatory later. The current
external document hashes are plan
`3b7889c005c99d7ad33986585d670b514945794f72ff4e044f3e5e813eb134af` and Luna
package `88216620ecbdc9d2ef6d65947b5176e6edb117de67ca23043c1ab8dd8c7f1d97`.

## Fresh Terra acceptance of 027

At exact clean toolkit `02720889cd4088dfae16f991fe19da460e5effb1`
and source `966ec4555bca1430f812390c522455f59a3ba8b8`, fresh independent
specification task `/root/terra_spec_acceptance_027` returned **GO** with no
findings. It rebound the exact heads/parents, required ancestor, all six
production identities, the plan/package hashes, and passed the 63-test focused
quarantine suite. Fresh adversarial security task
`/root/terra_security_acceptance_027` also returned **GO** with no findings;
448 tests collected and 14 targeted adversarial tests passed.

That acceptance is narrowly bound to the authenticated three-module first-gate
graph. It does not authorize or accept QR, quarantine, relink, observer, smoke,
delivery, or any live operation. The security review recorded
`whatsapp_qr_reconnect.py` as a later-phase capability-hardening residual outside
the gate graph. Standing Luna A/B exact-head mechanism review is now the next
required step, with the strictest decision governing. The gate remains
**CLOSED**, services remain offline, and no key/install/signing/authority/
candidate/approval/release/runtime/auth/message/artifact/live action occurred.

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

## Exact-head Terra acceptance successor — 2026-08-20

This documentation-only successor binds toolkit
`d7485311d52de206470b4f61c2c8eb3b613ee2ba`, reviewed source documentation
head `ad82d10201ae44d2d7f86e46e63b1f5ce4e0b9f6`, plan SHA-256
`a171fd7af9e10e58b3e8462b77a93054a0f32408f29f1eff08eca552ff34bcef`, and
Luna-package SHA-256
`75f6203930561ca0d1071df4cad5f03d7cfbf414850bca9d3cbf85ac2d7b9f9b`.

Fresh Terra specification GO from `/root/terra_cleanup_spec_acceptance` found
no Critical or Important findings and two Minor documentation clarifications;
it independently reproduced the focused test, seven-file `449`, and state
`150` results. Fresh Terra security GO from
`/root/terra_cleanup_security_acceptance` found no Critical, Important, or
Minor findings across the first/middle/last `OSError`, re-entrant close, and
unexpected-exception adversarial matrix; it recorded `107 passed` evidence
tests. Only OS-level cleanup uncertainty remains, with no retry as the safe
FD-reuse posture.

The canonical seven-file command is:

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py tests/shared/test_first_gate_bootstrap.py tests/shared/test_whatsapp_rotation_first_gate.py
```

The focused test's “Break caught” docstring names the old retryable-descriptor
regression; its assertions enforce no retry. The already-reviewed test remains
unchanged for this editorial clarification.

This successor is documentation-only over accepted source `ad82d10`; the exact
current source head after this commit must be bound by Luna and the successor
candidate. The gate remains **CLOSED** and offline, the old candidate remains
obsolete, QR remains a hard later gate, and only standing Luna exact-head review
is next.

## Superseding Task 4 cleanup hard-gate reconciliation

Standing Luna A/B initially gave exact-head GO for toolkit
`d7485311d52de206470b4f61c2c8eb3b613ee2ba` and source
`cd80a1feb34c16bb88fc294707a6492570e2ee90`, with the binding clarification
**CANDIDATE-ONLY**. The program accordingly skipped a knowingly disposable
candidate. The old
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79` remains
obsolete, unapproved, unsigned, and uninstalled; no new candidate exists.

Task 4A is `ffdcd2f` over `d748531`; Task 4B is `aa84928` over `ffdcd2f`;
the combined reviewed range is `d748..aa`. The exact contracts include the
repair of the previous false-positive direct-import test. Terra Task 4A spec
and quality reviews are clean, as are Task 4B spec and quality reviews; the
combined Terra security verdict is GO. Controller evidence is `458 passed in
77.84s`, state evidence is `151 passed in 55.25s`, and six-file Python 3.13
compile, scoped Ruff, diff, ancestor/range, and clean checks all passed.

Task 4A contract is exact: on any normal `Exception` opening four crypto
files, attempt all prior FDs once and in order; per-FD `OSError` cannot stop a
later cleanup. Clear `_owned` and `_material`; normalize an open `OSError` to
protected `EvidenceError`; preserve other exceptions after best-effort cleanup.
Never catch `BaseException` or retry a possibly reused FD. Task 4B permits
cleanup only for the exact verified seven-chain
attempt/q1/provenance/q2/pre/final/binding. After an inner GO close failure,
record and verify cleanup failure, then return NO-GO with a seven-plus-verified
receipt. Record/verify failure returns bare NO-GO; an already-correlated inner
NO-GO is preserved. Close once: no retry, no secret detail, no variable prefix.
The old direct-import close test did not cross its captured permit fence. Its
replacement local loader injects one shared bootstrap-equivalent permit into
the exact three modules; real state tests verify protected record/verify
semantics.

Authenticated artifacts now are bootstrap (26,420 bytes)
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
(92,811 bytes)
`f48de368d22399d8ac0c1e5f90c0a0f3b2f1fe0eb1c1df417e0e459e5d835277`;
quarantine `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
state (85,635 bytes)
`8124da1ed0c9046f08234cebf8ae09e32603fa36dadae767c9564dd5da291caa`; smoke
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller
(7,249 bytes) `4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`.

Gate state is **CLOSED** and offline: all four user services are
inactive/dead/disabled at PID 0 and fixed privileged paths are absent. QR,
quarantine, smoke, observer, and relink remain later hard gates. Persona
evolution is omitted; proactivity, consciousness, and speak-up remain
mandatory later. Next, and only next: commit docs; compute plan/package hashes
outside their self-hashed files; form a docs-only acceptance successor; conduct
standing Luna A/B exact-head review; construct one candidate only if both GO.
No runtime, auth, memory, service, network, or privileged action is authorized.

## Task 4 acceptance successor

This documentation-only successor is over accepted source docs head
`c947b020d4dd151320aa64fd9fe426386a659585` and binds toolkit
`aa84928af62d8ebf66ab8b658c5a2a0618772319`, plan SHA-256
`9f1669e5c698fad6682c727b4e52fc8bd92491ded6b5f989414474d745fcc700`, and
package SHA-256
`9bf78317cf78506078e35456b9059cf4f53f579172d6ae31a6c2128be39221e8`.
Task 4A independent review is SPEC PASS / QUALITY APPROVED with no findings;
Task 4B independent review is SPEC PASS / QUALITY APPROVED with no findings;
the combined Terra security verdict is GO with no findings. The earlier docs
initial Important finding was resolved by `c947`, then re-reviewed as SPEC PASS
/ QUALITY APPROVED. Controller verification was `458 passed in 77.84s`; state
verification was `151 passed in 55.25s`; Python 3.13 compile, scoped Ruff,
diff, ancestor, range, and clean checks passed. Current candidate material is
bootstrap 26,420 bytes
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
92,811 bytes
`f48de368d22399d8ac0c1e5f90c0a0f3b2f1fe0eb1c1df417e0e459e5d835277`; state
85,635 bytes
`8124da1ed0c9046f08234cebf8ae09e32603fa36dadae767c9564dd5da291caa`; and
controller 7,249 bytes
`4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`.

The gate remains **CLOSED** and offline. Old candidate
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79` remains
obsolete; no replacement candidate exists. QR and later hard gates remain
closed; persona evolution is omitted; proactivity, consciousness, and
speak-up remain mandatory later. This commit's current source head must be
bound by standing Luna A/B and any candidate. Only standing Luna A/B exact-head
review is authorized next; construct no candidate unless both return GO. No
runtime, candidate, privileged, or live action is authorized.

## Superseding Task 5-8 systemic cleanup acceptance — 2026-08-20

This section supersedes the preceding Task 4 next-step record while preserving
its evidence. Standing Luna A/B had clarified the earlier exact-head result as
**CANDIDATE-ONLY**, so no disposable replacement candidate was constructed.
Task 5 then accepted evidence ownership at `e3f0867`, state ownership at
`1bca8e7`, and bootstrap ownership at `9188f31`; its first combined review
nevertheless found raw terminal-close and unbounded/raw child-reap gaps. Task 6
`af3343b` repaired those local gaps, and Task 7 `31d8361` hardened the
authenticated process caller graph. Their fresh reviews exposed the architectural
cause: the controller used a separate state quiescence adapter while evidence
retained a dead production duplicate, and several acquisitions/streams entered
cleanup only after validation.

The final accepted toolkit is
`108db1ce3bd16325691afba7042e8130ae8d52d3`, parent
`31d8361b89c5fec5126ce3b40f08e96ee1c64a11`. The exact Task 8 range is
`31d8361b89c5fec5126ce3b40f08e96ee1c64a11..108db1ce3bd16325691afba7042e8130ae8d52d3`;
the combined cleanup range is
`aa84928af62d8ebf66ab8b658c5a2a0618772319..108db1ce3bd16325691afba7042e8130ae8d52d3`,
and required ancestor `6ba550` remains present. Task 8 touches exactly
bootstrap, evidence, state, and their three tests. Controller, quarantine,
smoke, and secret-scope scanner bytes are unchanged. The authenticated
production topology is now one explicit chain: bootstrap loads evidence, then
state, then controller; the controller reaches only the captured state
quiescence adapter. Evidence's dead `_live_quiescence_fields` wrapper and its
direct test are removed. Evidence retains only the pinned spawn/read/reap and
receipt-sampler primitives; no generic subprocess module, arbitrary runner, or
wider permit-derived capability was added.

The accepted lifecycle contract is uniform and terminal. An unowned initial
open failure maps directly to its protected domain with no close attempt.
Every acquired FD, directory, stream, and child has one owner; handoff detaches
the former owner before a terminal close/reap, sibling cleanup is attempt-all,
and an uncertain close is never retried or followed by an acquisition that can
reuse its number. Missing stdout and every normal process fault enter bounded
cleanup. Successful readers do not perform a second wait/reap. Age abort closes
parent stdin, performs bounded child reap, and closes output in one idempotent
attempt-all pass. State owns each stop child until bounded wait succeeds or
bounded reap is attempted, and explicitly transfers each show child to the
evidence reader. Crypto material acquisition rolls back every opened FD and
returns the uniform protected domain. No reachable production `poll()`,
`BaseException` catch, unbounded wait, raw normal resource exception, auth
path change, schema change, or capability expansion remains.

Authoritative evidence at `108db1c`: canonical seven-file suite `598 passed
in 90.67s`; standalone state `180 passed in 91.58s`; bootstrap `97 passed`;
evidence `181 passed`; independent reachable four-suite review `479 passed`;
changed three-suite review `458 passed`; adversarial security review `535
passed`. Python 3.13 compile for the seven audited production files, scoped
Ruff, diff/ancestry, exact allowlist, no-new-skip, no-`poll`,
no-`BaseException`, dead-wrapper-absence, and clean-status checks passed.
Independent final verdicts are **SPEC PASS / ARCHITECTURE GO / QUALITY
APPROVED** and security **GO**, with no findings.

Authenticated artifact identities are: bootstrap (27,584 bytes)
`55f6b93b53b07ff6bcbc94cde21f8acaa9515e58f9b0f76fe6be9ea3a5419200`;
evidence (96,014 bytes)
`ca9320c46b10aeeba90f3eb5b2c4853a6d30202d924f8c2e14b438b31f547d31`;
state (88,428 bytes)
`6630aaed152cfaa6d5de6ff9fa676a65a550b17c4025e13130475a8806db80b6`;
controller (7,249 bytes)
`4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`;
quarantine (52,374 bytes)
`b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
smoke (45,924 bytes)
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`;
secret-scope scanner (8,979 bytes)
`233f31419ef7573b7d391ef6909716acd8b1a6101f0a8767d2aa185b8b8661ea`.

The pre-reconciliation source head is
`3a44c1437a4621e777b0c7b347fde7e2ce81aa04`; preserved target baseline is
`bdc4c8c9e33602c5c8abd69f054703469d212dab`. Overseer, Bridge, and Gateway
remain inactive/disabled at PID 0; Pinchtab is inactive/not-found at PID 0.
Release authority, installed bootstrap, and fixed candidate paths are absent.
Obsolete mode-0600 candidate
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79`
remains unapproved, unsigned, uninstalled, and unusable; no replacement exists.
No runtime, auth, memory, network, service, QR, quarantine, smoke, observer,
relink, candidate, signing, install, or live action occurred.

Only this docs reconciliation, its independent review, a separate
plan/package-hash acceptance successor, and standing Luna A/B exact-head review
are authorized next. Construct one new unsigned candidate only if both standing
Luna reviewers return GO. Direct-script quarantine/smoke/QR cleanup debt remains
bound to those later hard gates. Persona evolution remains omitted; proactivity,
consciousness, and speak-up remain mandatory later capabilities.

Pre-reconciliation self-hashed artifact values were plan
`9f1669e5c698fad6682c727b4e52fc8bd92491ded6b5f989414474d745fcc700`
and Luna package
`9bf78317cf78506078e35456b9059cf4f53f579172d6ae31a6c2128be39221e8`.
They identify source head `3a44c143` before this five-document append and are
historical only. The controller must calculate the new plan/package hashes
after the reconciliation commit and bind those new values in a separate
three-document acceptance successor before standing Luna review.


## Task 8 documentation hash acceptance — 2026-08-20

Independent review returns **SPEC PASS / QUALITY APPROVED** with no finding for
the exact five-document reconciliation at source commit
`3500b6f03acbad918457c7c084298964e5227424` (parent
`3a44c1437a4621e777b0c7b347fde7e2ce81aa04`). That commit binds accepted
toolkit `108db1ce3bd16325691afba7042e8130ae8d52d3` and preserved target
`bdc4c8c9e33602c5c8abd69f054703469d212dab`. Its exact current orchestration
plan SHA-256 is
`783016d712a8bfd96ee353c8bdcf7996f8138099c413483dc7fae8aa1b743531`;
its exact current standing-Luna package SHA-256 is
`8b5eb8176585e9de4c3c9260124012e1b110caf824345be7f4acead779126941`.
The plan and package do not contain either self-hash; these values are recorded
only in the three non-self acceptance records.

The docs review verified exact heads/ancestry, five Markdown files with
additions only, all Task 5-8 hashes/sizes/tests/verdicts, offline/candidate/QR
boundaries, the single state production quiescence wrapper, later direct-script
debt, and the required persona/proactivity/consciousness/speak-up disposition.
This three-document successor changes no plan/package/code bytes. Its resulting
source head must be bound by standing Luna A/B. Only their exact-head review is
authorized next. No candidate, approval, decision, release, signature, install,
runtime, auth, memory, network, service, QR, quarantine, smoke, observer,
relink, or live action is authorized.
