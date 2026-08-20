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

## Authenticated-bootstrap correction and fresh handoff

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
No live/runtime/systemctl/key/owner/QR/message operation occurred.

The authoritative orchestration-plan SHA-256 is
`9871fefbeb6ee163760ea72380c2827b10c57fb997a33e1f9a3ab41150bd6853` and the
Luna-package SHA-256 is
`b3c9d7dd2805b55a315e3129edb167f9f07a4aee386b8533c1b0e0ee79815c6f`.
They are recorded outside the documents they hash. Final review must bind the
successor source documentation commit containing this report.
