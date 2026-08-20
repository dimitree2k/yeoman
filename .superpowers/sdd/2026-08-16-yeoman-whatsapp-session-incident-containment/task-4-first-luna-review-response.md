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

## Strict production-capability correction — pending fresh review

Fresh Terra first rejected source `1b18fba1b427e0069d80c8e0a583500d90e1cea0`
and toolkit `f55a5e28cfabc5010aa84031bd699aa8e4054645`: the implementation
accepted an executable authority and ordinary imports exposed production
actions. Toolkit eb272 corrected the authority/two-signer contract, and parent
verification passed `472 passed in 61.11s`. Fresh Terra then rejected source
`41b2915684311010a6c96061afeb2bef1ab86dd3` and toolkit
`eb272c52965a7d2dd1a4c66eaa40c8e499079f7a`: underscored evidence/state
helpers and quarantine/smoke production adapters were still ordinary callable
module attributes. Both earlier counts and claims are historical only.

The strict correction now ends at toolkit
`a9504c233a14f92a12b4807855e94e621e8aa4ce`. Quarantine's fixed-root
production runtime and smoke's production sender, transport, credential,
observer, lease, and run paths are deleted. Evidence fixed-root configuration,
pinned tool execution, production crypto, and legacy reads require the exact
bootstrap-injected permit; arbitrary capture subprocess helpers are absent.
State fixed-root runtime construction and service quiescence require the same
permit. The fixed live auth root is refused by the quarantine test seam. Pure
and test-injected later-phase cores remain, so later production phases require
a future authenticated controller capability.

Independent parent verification at clean a950 passed the exact seven-file
suite: `433 passed in 56.67s`. Python 3.13 compiled all six production files;
scoped Ruff, diff/status, required-ancestor, and zero skipped/retired-test scans
passed. The reduced count comes from physical deletion of obsolete production
adapter tests, not skips. Production SHA-256 values are bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`, evidence
`15069931dc07771a45435b9490ea06e4d5db30798dc62bfe1b61a8786d29b016`, quarantine
`01306f100e86864748206c65672309dab89e2d215ce6fa058fdca916d6688618`, state
`0a5573ed619689450fd9b6116ea4097f8ec6ecfe5ed30f697747bc04c87c13ed`, smoke
`74652a00b5ace59f0b36d3c47a2f5fb630727e5462503ec3f7e2136a96794030`, and
controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.

Authority schema v4 remains root-owned regular exact `0644`; bootstrap remains
root-owned regular exact `0755`. Authority pins distinct canonical Ed25519
release and owner signers. Candidate/decisions/release use the release signer;
owner approval uses the owner signer and a distinct namespace. Same-process
malicious introspection and a malicious owner remain outside this stale-entry
threat boundary.

The only intended invocation remains byte/order exact:

```bash
/usr/bin/env -i LANG=C LC_ALL=C TZ=UTC PATH=/usr/bin:/bin /usr/bin/python3.13 -I -S -E -B /usr/local/libexec/yeoman/first_gate_bootstrap.py
```

No bootstrap, authority, release key, candidate, owner approval, decision,
release, or signature was installed or created. Fresh Terra and both standing
Luna mechanism/procedure reviews remain pending. Only after their acceptance
may an exact unsigned candidate draft be prepared, followed by an explicit
owner prompt naming its hash and authorizing two-key privileged preparation
and signing. No live action occurred; the first gate remains **CLOSED**.
Persona evolution is omitted; proactivity, consciousness, and speak-up remain
mandatory later capabilities.
