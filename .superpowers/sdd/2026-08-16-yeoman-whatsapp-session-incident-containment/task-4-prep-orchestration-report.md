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
