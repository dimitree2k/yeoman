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

Toolkit a950 then removed quarantine's fixed-root production runtime and
smoke's production sender, transport, credential, observer, lease, and run
paths. It guarded the advertised production config/tool/crypto and exact state
runtime paths and removed arbitrary capture subprocess helpers, while keeping
pure/test-injected later-phase cores for a future authenticated controller.
Parent verification passed `433
passed in 56.67s`, but exact-head Terra returned **REJECT**: a normal import
could pair the fixed legacy descriptor with attacker-controlled test crypto and
receive fixed key/data FDs; raw evidence/key open helpers were ungated; and
exact-path checks missed evidence/state descendants. a950 is historical only.

The historical strict correction ended at toolkit
`bb37b577fc191084be74dff72a87b84ac9cf08ab`. Fixed evidence, legacy, HMAC,
age identity, signing, allowed-signers, and state roots plus descendants require
the exact bootstrap permit at config, raw directory/file/root helper, legacy
core, and inventory boundaries. Direct tests monkeypatch `os.open` and prove
refusal before effects. Separator-safe lexical containment rejects sibling
prefix aliases and preserves bounded performance.

Historical independent parent verification passed state `148 passed in 45.70s` and the
exact seven-file suite `440 passed in 65.41s`. Python 3.13 compiled all six
production files; scoped Ruff, diff/status, required-ancestor, and historical
no-new-skipped/retired-test scans passed. Production SHA-256 values are bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`, evidence
`7ff18521a8af2de78382d1d233a3ef707438727f9e6a26d63b4501fbaba0513e`, quarantine
`01306f100e86864748206c65672309dab89e2d215ce6fa058fdca916d6688618`, state
`2a6beab7699da11caf327a5cd332d98b0a28c84f8d887394b2856f67379d9071`, smoke
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

## Current successor correction — review still required

bb37 is historical **REJECT**, not an accepted head. Fresh review found its
Critical ancestor `_Dir` path/permit loss permitted legacy/key/age/signing
reads; state returned a composable raw parent FD. Important findings were the
authenticated journal permit drop that prevented q1, quarantine fixed-target
ancestor/descendant admission, and documentation that overclaimed safety.

The clean current toolkit head is `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
Review `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83` (full
`f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`).
The successor uses symmetric normalized separator-safe overlap; provenance
carrying opaque `_Dir` and `_RootHandle` handles; effect-specific authorization
before filesystem effects; exact-permit production crypto; journal permit
propagation; and quarantine overlap refusal with sibling-prefix fixtures
allowed. No stale production adapters remain. Same-process introspection and a
malicious owner remain out of scope.

Current production hashes: bootstrap
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
`8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9`;
quarantine `14df959da264d7580f3bc78a5bce693284e917bb799fddb35bd506e787297dcb`;
state `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16`;
smoke `4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`;
controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.

Parent exact evidence: state `150 passed in 55.10s`; seven-file suite `445
passed in 77.47s`; six-file Python 3.13 compile, full scoped Ruff, clean
diff/status, required ancestor, no newly added skips/retired coverage, and
adapter scans pass. Two bootstrap conditional system-`ssh-keygen` skips
pre-exist; this is not a global zero-skip result. The gate remains **CLOSED**:
no privileged action, install, key, authority, candidate, approval, decision,
release, signature, runtime, auth, message, or artifact action occurred;
services are offline. Fresh Terra then standing Luna A/B exact-head mechanism
reviews remain pending. Persona evolution is omitted; proactivity,
consciousness, and speak-up remain mandatory later capabilities.

## Superseding security reconciliation — 2026-08-20

The preceding `2967e451d10d83c0a07ff4ed54dad7a556b6bf83` successor is
historical **REJECT**. Fresh Terra security session
`01a01dfa-4f0f-7f61-bb31-f52e6daa5046` found that `_HeldAuth.parent_fd`/`.fd`
enabled ordinary-import composition from an allowed sibling into fixed WhatsApp
auth. Parallel Terra specification session
`01a01dfa-4f22-7500-b1e3-2df4834d1af7` returned GO, but the strictest security
REJECT governs. Its `445` seven-file and `150` state results and old hashes are
historical only.

Current clean toolkit successor `02720889cd4088dfae16f991fe19da460e5effb1`
(parent `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`) requires fresh review of
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83..02720889cd4088dfae16f991fe19da460e5effb1`
and full `f55a5e28cfabc5010aa84031bd699aa8e4054645..02720889cd4088dfae16f991fe19da460e5effb1`.
It name-mangles/opaque-holds `_HeldAuth` FDs and target name, exposes no global
FD registry/getter/raw directory-descriptor API, bounds effects to exact
`.auth-quarantine-<32hex>` siblings, requires same-parent exchange with
internal name swap, and closes acquired recursive child FDs across `dup`/`fstat`
failure. Quarantine SHA-256 is
`b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`; other
production hashes are unchanged.

Root verification is seven-file `448 passed in 83.62s`, state `150 passed in
63.78s`, six-file Python 3.13 compile, full scoped Ruff, diff, required
ancestor, and no-new-skip checks; two pre-existing conditional system
`ssh-keygen` skips remain. Implementation design re-review gave GO/no
findings, but fresh exact-head independent Terra specification/security
acceptance remains pending. Luna is blocked and the gate is **CLOSED**: no key,
install, signing, authority, candidate, approval, release, runtime, auth,
message, artifact, or live action occurred; services are offline. Persona
evolution remains omitted; proactivity, consciousness, and speak-up remain
mandatory later. Current external hashes: orchestration plan
`3b7889c005c99d7ad33986585d670b514945794f72ff4e044f3e5e813eb134af`; Luna
package `88216620ecbdc9d2ef6d65947b5176e6edb117de67ca23043c1ab8dd8c7f1d97`.

## Exact-head Terra gate accepted — standing Luna review next

Fresh independent Terra specification task `/root/terra_spec_acceptance_027`
and adversarial security task `/root/terra_security_acceptance_027` both
returned **GO** with no Critical, Important, or Minor finding at toolkit
`02720889cd4088dfae16f991fe19da460e5effb1` and source
`966ec4555bca1430f812390c522455f59a3ba8b8`. Specification rebound all exact
heads/hashes and passed 63 focused quarantine tests. Security collected 448
tests and passed 14 targeted adversarial cases, including opacity, fixed-root,
same-parent exchange, cleanup, permit/q1, splice, and direct-import checks.

The security GO covers only the authenticated three-module first-gate graph;
it does not authorize QR reconnect, quarantine, relink, observer, smoke,
delivery, or any live operation. `whatsapp_qr_reconnect.py` remains a recorded
future-phase capability-hardening residual outside this graph. The same standing
Luna Agent A/B sessions must now review the exact accepted mechanism and this
source handoff; the strictest result governs. The first gate stays **CLOSED**,
services remain offline, and no privileged or live artifact/action exists.
