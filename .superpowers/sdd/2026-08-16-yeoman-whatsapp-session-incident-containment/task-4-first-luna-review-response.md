# First Luna review response — durable correction record

Initial review range: `6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.

- Agent A: narrow conditional GO only for quiescence/read-only inspect/capture;
  Important gaps were the fixed bounded controller and fresh capture-bound
  q2/final read-only check.
- Agent B: **NO-GO**, the governing decision; Important gaps were
  trusted-ancestor validation and authenticated `VerifiedV1Scope`/artifact
  provenance.
- Both found the stale local recovery-report reference.  The correct report is
  `.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`.
- No live action occurred.

Six ordered corrections now end at
`62acf5b73ca9b9abaed79004deed7f93ef2280b3`: `e90402f7fd6132e0df919a9c4b6e5f2c91b03580`,
`7a2dad692511b056696aa0fda95327ec4f0bd14e`,
`9dc098c67b5c1afc0ba9bfd6439bae64cda54534`,
`056c2a10bde8a90a006d7d6ba2cba8d877ad1542`,
`c9d5a0c670f9a9cf426a6afbbaaa672d366524ba`, and
`62acf5b73ca9b9abaed79004deed7f93ef2280b3`.

The fixed state closes held ancestors fail-safe, binds the exact all-field
legacy descriptor and authenticated `VerifiedV1Scope`, binds causal q2/final
receipts through q1/provenance/pre/final, requires verification at controller
and second-Luna handoff, preserves exact inventory/pre/post/comparator
provenance plus semantic-quiescence continuity, and makes public compare fail
closed.  Mutations were run and restored.

Fresh controller verification at the final head recorded 376 passing tests
(285 + 91 because of the task-runner time boundary), relevant Ruff clean,
`git diff --check` clean, and a clean toolkit worktree.  Final Terra SPEC
ACCEPT and QUALITY ACCEPT had no Critical, Important, or Minor finding.

Runtime is offline/disabled and the first live gate remains **CLOSED** pending
independent re-review by the same standing Luna Agents A and B of corrected
range `6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`.
Their strictest decision governs.  A subsequent GO can authorize only full
quiescence plus fixed first-gate controller/read-only v1 inspect/pre-v2 capture
and protected commitment handoff; it cannot authorize owner, revocation,
quarantine, QR/relink, observer, or smoke work.

## Standing Luna re-review — governing NO-GO, correction underway

At the same exact source/toolkit heads, Agent B returned a narrow **GO** for
only the stated first-gate lifecycle. Agent A returned the governing
**NO-GO**. Its three blockers are: PATH-resolved `age`, `age-keygen`, and
`ssh-keygen` subprocesses despite sensitive descriptors; the authoritative
orchestration plan's missing fifth `whatsapp_rotation_first_gate.py` component;
and the absence of an exact production invocation/toolchain provenance plus a
protected attempt/run identity and allowlisted failure record.

Documentation now records the intended fixed executable baseline and lifecycle,
but the toolkit implementation, tests, and fresh reviews for those three
blockers are pending. The gate remains **CLOSED**. Preserve all historical
376-test evidence as evidence of the prior synthetic head only; it does not
establish the later correction. No live action occurred.

## Production-capability correction — pending fresh review

Toolkit correction range
`f55a5e28cfabc5010aa84031bd699aa8e4054645..eb272c52965a7d2dd1a4c66eaa40c8e499079f7a`
uses the standard-library-only pre-import trust anchor
`bootstrap/first_gate_bootstrap.py` (SHA-256
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`). Parent
verification at clean eb272 passed the exact seven-file suite: `472 passed in
61.11s`; `/usr/bin/python3.13` `py_compile`, scoped Ruff, diff check, and
status were clean. The f55 `459 passed` and `f55a5e2` fixture evidence is
historical only.

Fresh Terra review of source `1b18fba1b427e0069d80c8e0a583500d90e1cea0`
and toolkit `f55a5e28cfabc5010aa84031bd699aa8e4054645` found that the
implementation accepted an executable authority and that ordinary imports
exposed state, evidence, and owner-turn production actions. eb272 closes that
boundary: authority schema v4
requires root-owned regular exact `0644`; bootstrap remains root-owned regular
exact `0755`; authority pins a canonical Ed25519 release signer and an owner
signer with separately hashed allowed-signers content and distinct decoded
public-key blobs. Candidate, decisions, and release use the release signer;
owner approval uses the owner signer and its own namespace. One per-execution
permit is injected into all three authenticated modules; evidence, state, and
controller capture the exact identity; state production runtime, service
quiescence, and legacy manifest reader require it. Public production actions in
evidence, state, quarantine, and smoke now refuse or are absent, so later
phases need a future authenticated controller capability. Same-process
malicious introspection remains outside the threat model. Fresh Terra and both
standing Luna reviews remain pending; this is not acceptance.

Current production module hashes are evidence
`fc6ae7b178711b4847c0c1b2a091bb8802221c9ac4c7297105f29b9eb90ba203`, state
`6e75b07ee171ffa0a78a3001a114ec405c4ecd739385fe89a6b5608528058b59`, and
controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.

The only intended invocation is byte/order exact:

```bash
/usr/bin/env -i LANG=C LC_ALL=C TZ=UTC PATH=/usr/bin:/bin /usr/bin/python3.13 -I -S -E -B /usr/local/libexec/yeoman/first_gate_bootstrap.py
```

Runtime Git and the `/usr/bin/python3` symlink are not authorization.
`/usr/bin/env` and `/usr/bin/python3.13` are root-owned pathname launcher
trust boundaries verified after startup; only later `ssh-keygen`, `age`,
`age-keygen`, and `systemctl` subprocesses use held FDs. No bootstrap,
authority, release key, candidate, decision, release, or signature was
installed or created; nor does an owner-approval artifact exist. First obtain
fresh Terra acceptance and standing-Luna mechanism/procedure reviews; prepare
the exact unsigned candidate draft; then stop for an owner prompt naming its
hash and authorizing two-key privileged bootstrap/authority/key preparation plus signing
that exact candidate. Only after signed owner/candidate envelopes may both Luna
sessions review exact signed bytes/hash, followed by signed decisions/release
and differing-byte re-review. No live action occurred; the first gate remains
CLOSED. Persona evolution is omitted; proactivity, consciousness, and speak-up
remain mandatory later capabilities.
