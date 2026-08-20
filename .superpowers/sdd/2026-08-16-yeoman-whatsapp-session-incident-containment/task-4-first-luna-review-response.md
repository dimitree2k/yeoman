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

The standing Luna A/B exact-head result for toolkit
`d7485311d52de206470b4f61c2c8eb3b613ee2ba` and source
`cd80a1feb34c16bb88fc294707a6492570e2ee90` was initial GO, explicitly
clarified as **CANDIDATE-ONLY**. To avoid authorizing bytes known to be
disposable, the program skipped a candidate. Old candidate
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79` is obsolete,
unapproved, unsigned, and uninstalled; no new candidate has been made.

Toolkit Task 4A is `ffdcd2f` with parent `d748531`; Task 4B is `aa84928` with
parent `ffdcd2f`; the exact combined range is `d748..aa`. The contract set
contains the repair for the prior false-positive direct-import test. Terra
review was clean for Task 4A spec/quality and Task 4B spec/quality; combined
Terra security review is GO. Controller verification is `458 passed in
77.84s`; state verification is `151 passed in 55.25s`; six-file Python 3.13
compile, scoped Ruff, diff, ancestor/range, and clean checks passed.

Task 4A: on any normal `Exception` opening the four crypto files, attempt every
prior FD once in order; a per-FD `OSError` cannot stop later cleanup. Clear
`_owned` and `_material`; normalize open `OSError` to protected `EvidenceError`;
preserve all other exceptions after best-effort cleanup. Never catch
`BaseException` and never retry a possibly reused FD. Task 4B: permit cleanup
only after exact verified seven-chain attempt/q1/provenance/q2/pre/final/binding.
After inner GO close failure, record and verify cleanup failure and return
NO-GO with a seven-plus-verified receipt. Record/verify failure returns bare
NO-GO; preserve already-correlated inner NO-GO. Close once: no retry, no secret
detail, no variable prefix. The old direct-import close test never crossed its
captured permit fence. The replacement local test loader injects one shared
bootstrap-equivalent permit into the exact three modules, while real state tests
verify protected record/verify semantics.

Current artifact identities: bootstrap (26,420 bytes)
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
(92,811 bytes)
`f48de368d22399d8ac0c1e5f90c0a0f3b2f1fe0eb1c1df417e0e459e5d835277`;
quarantine `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
state (85,635 bytes)
`8124da1ed0c9046f08234cebf8ae09e32603fa36dadae767c9564dd5da291caa`; smoke
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller
(7,249 bytes) `4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`.

The gate stays **CLOSED** and offline. Four user services are
inactive/dead/disabled with PID 0; fixed privileged paths do not exist. QR,
quarantine, smoke, observer, and relink are deferred hard gates. Persona
evolution is omitted; proactivity, consciousness, and speak-up remain mandatory
later capabilities. Next: commit these docs; compute plan/package hashes outside
the self-hashed plan/package files; make a docs-only acceptance successor;
standing Luna A/B reviews that exact head; one candidate only if both GO. No
runtime, auth, memory, service, network, or privileged action is authorized.

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
