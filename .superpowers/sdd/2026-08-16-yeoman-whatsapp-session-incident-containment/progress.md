# SDD ledger — plan: docs/superpowers/plans/2026-08-16-yeoman-whatsapp-session-incident-containment.md

Task 1: implementation complete; independent task review pending — metadata-only runtime log helper; no live log invocation permitted
Task 1: fix round 1/5 (test boundary finding implemented; scoped re-review pending — test SHA-256 `a98099196478223a1f2d16c7487f8a2890dfa5258637afa5190849c2eca5c261`)
Task 1: fix round 1/5 (1 addressed, 0 open — complete stdout allowlist; no production change)
Task 1: complete (non-Git skill artifacts reviewed by exact SHA-256; review clean)
Task 2: in progress from toolkit base `bdc4c8c9e33602c5c8abd69f054703469d212dab` — metadata-only exposure-scope scanner
Task 2: fix round 1/5 in progress — descriptor-anchored traversal, bounded grammar, and missing contract tests
Task 2: fix round 1/5 (5 addressed, 0 open — commits `530b851..686a1ee`)
Task 2: complete (commits `bdc4c8c..686a1ee`, scoped re-review clean; restricted scan receipt verified)
Task 3: in progress from toolkit base `686a1ee84f060b4bcb5acd35b65887cbafed2df3` — harden canonical QR reconnect boundary
Task 3: complete (commits `4b39f40..6ba5501`; 32 focused tests, Ruff clean, 629 Gateway tests; scoped Terra re-review ACCEPT)
Task 3b: complete (installed reconnect skill aligned; wrapper removed; contract/validator green; scoped Terra review ACCEPT)
Task 4: pending / NO-GO — owner-authorized WhatsApp revocation and QR relink remain forbidden.
Task 4 pre-owner prep-03: initial synthetic bundle accepted at toolkit `995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`; Tasks 1-3 Terra acceptance at `b788608`, integration/task SPEC ACCEPT and QUALITY ACCEPT with no findings.  The initial standing-Luna review found material first-gate gaps: Agent A gave only a narrow GO for quiescence/read-only inspect/capture, contingent on a fixed bounded controller and a fresh capture-bound q2/final read-only check; Agent B gave the governing NO-GO, requiring trusted-ancestor validation and authenticated `VerifiedV1Scope`/artifact provenance.  Both identified the stale local recovery-report reference.  Six correction commits now end at toolkit `62acf5b73ca9b9abaed79004deed7f93ef2280b3`; fresh controller verification recorded 376 passing tests, relevant Ruff clean, `git diff --check` clean, and a clean toolkit worktree.  Final Terra SPEC ACCEPT and QUALITY ACCEPT had no findings.  Runtime remains offline/disabled, but the first live gate is **CLOSED** until the same standing Luna Agents A and B independently re-review fixed range `6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`; their strictest decision governs.  No live evidence exists.

Historical Task 4 standing-Luna re-review (source baseline `b74207c10940f9c13335ebe25b0d6e53af63f83e`, toolkit historical range `6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`): Agent B returned a narrow GO, but Agent A returned the governing NO-GO. At that point the required toolkit correction was pending: fixed crypto/toolchain identities, a zero-argument controller preflight, and protected attempt/failure lifecycle. The then-current plan/package hashes were `32c1c1831c73306d891ee20efa0f33a3defdc327bd6a4345588cd17f61b5e11f` and `ce94fcad2a651779547d7cf8787c16b57853181df30a3fce7806de92024cb27a`. Those hashes and the 376-pass evidence describe the historical state only. Runtime remained offline; no live action was authorized.

Historical Task 4 f55 authenticated-bootstrap correction (toolkit range `62acf5b73ca9b9abaed79004deed7f93ef2280b3..f55a5e28cfabc5010aa84031bd699aa8e4054645`): the `459 passed in 60.93s` suite, `f55a5e2` fixture closure, and f55 hashes are historical only; they do not establish the later capability correction.

Historical Task 4 bb37 production-capability attempt: f55/eb272/a950 were historical rejected stages; bb37 is also historical **REJECT**, not the current head. Its direct fixed-path gates still lost `_Dir` path/permit provenance through ancestors, exposed legacy/key/age/signing reads, returned a composable state parent FD, dropped the authenticated journal permit before q1, admitted quarantine fixed-root overlap, and overclaimed safety. The `148 passed`/`440 passed` evidence and prior hashes are historical only.

Historical document hashes: orchestration plan `bc33f8e4d9969deea8ccfc66de5e79511a9af36d7de7928febcb32ad17b51721`; Luna package `7fe470d3466191c840f9243f81d5cde2fc5d5328a9df1ad344815f21bee511ef`.

Current Task 4 successor: toolkit clean head `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`; review range `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`; full range `f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`. Symmetric normalized separator-safe overlap rejects ancestors and descendants. `_Dir` carries path/permit, exposes no `.fd`, and authorizes each effect-specific transition; only `_authenticated_fd(exact captured permit)` supports production crypto. State uses opaque `_RootHandle`, journal opens propagate `config.permit`, and quarantine rejects fixed-target overlap while sibling-prefix fixtures remain allowed. No stale production adapters remain; same-process introspection and malicious owner are out of scope.

Parent evidence: state `150 passed in 55.10s`; exact seven-file suite `445 passed in 77.47s`; six-file Python 3.13 compile, full scoped Ruff, clean diff/status, required ancestor, no newly added skips/retired coverage, and adapter scans pass. Bootstrap has two pre-existing conditional system-`ssh-keygen` `pytest.skip` cases; do not claim global zero skips. Production hashes: bootstrap `ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence `8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9`; quarantine `14df959da264d7580f3bc78a5bce693284e917bb799fddb35bd506e787297dcb`; state `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16`; smoke `4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e`.

Current authoritative document hashes (recorded outside their self-hashed documents): orchestration plan `21c864f97b599b9a0dfd0660521d198d82c791bd60aca851ae02caf7947fe63c`; Luna package `fa0c154d6c14f125c681c75c6994d49552f2a3e132778316d123fb12ce27fb7c`. Final review must bind the successor source documentation commit.

Gate remains **CLOSED**: no install/key/authority/candidate/owner approval/decision/release/signature/runtime/auth/message artifact/action; services offline. Fresh Terra then standing Luna A/B exact-head mechanism review remain pending. Persona evolution omitted; proactivity, consciousness, and speak-up mandatory later.

Current reconciliation: toolkit `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`
is historical **REJECT** after fresh Terra security session
`01a01dfa-4f0f-7f61-bb31-f52e6daa5046` found ordinary-import composition from
an allowed sibling into fixed WhatsApp auth through `_HeldAuth.parent_fd`/`.fd`.
Parallel Terra specification session `01a01dfa-4f22-7500-b1e3-2df4834d1af7`
returned GO, but the strictest security REJECT governs. Its `445` seven-file
and `150` state results and old hashes are historical. Current clean successor
`02720889cd4088dfae16f991fe19da460e5effb1` (parent `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`)
must be reviewed over `2967e451d10d83c0a07ff4ed54dad7a556b6bf83..02720889cd4088dfae16f991fe19da460e5effb1`
and full `f55a5e28cfabc5010aa84031bd699aa8e4054645..02720889cd4088dfae16f991fe19da460e5effb1`.
`_HeldAuth` FDs/target name are name-mangled opaque; no global FD registry,
getter, or raw directory-descriptor API remains. Effects validate exact
`.auth-quarantine-<32hex>` names, exchange only same-parent handles with an
internal swap, and recursive children own/clean up FDs across `dup`/`fstat`
failure. Quarantine SHA-256 is `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
other production hashes are unchanged. Root evidence: seven-file `448 passed
in 83.62s`; state `150 passed in 63.78s`; six-file Python 3.13 compile, full
scoped Ruff, diff, required ancestor, and no-new-skip checks pass. Two
pre-existing conditional system-`ssh-keygen` skips remain. Design re-review
gave GO/no findings, but fresh exact-head Terra specification/security
acceptance is pending; Luna is blocked and the gate remains **CLOSED**. No
key/install/signing/authority/candidate/approval/release/runtime/auth/message/
artifact/live action occurred; services offline. Persona evolution is omitted;
proactivity, consciousness, and speak-up remain mandatory later. Current
authoritative hashes, recorded outside self-hashed documents: orchestration
plan `3b7889c005c99d7ad33986585d670b514945794f72ff4e044f3e5e813eb134af`; Luna
package `88216620ecbdc9d2ef6d65947b5176e6edb117de67ca23043c1ab8dd8c7f1d97`.

Fresh exact-head Terra acceptance at toolkit
`02720889cd4088dfae16f991fe19da460e5effb1` and source
`966ec4555bca1430f812390c522455f59a3ba8b8`: specification task
`/root/terra_spec_acceptance_027` returned **GO** with no findings after binding
the heads, parents, ancestor, six production hashes, plan/package hashes, and
`63 passed` focused quarantine suite. Security task
`/root/terra_security_acceptance_027` returned **GO** with no findings; it
collected 448 tests and passed 14 targeted adversarial cases covering opaque
FDs, fixed-root refusal, same-parent exchange, recursive cleanup, permit/q1
binding, DAG/splice resistance, and direct-import refusal. The security GO is
only for the authenticated three-module first-gate graph: QR reconnect,
quarantine, relink, observer, smoke, and delivery remain unauthorized.
`whatsapp_qr_reconnect.py` is a recorded future-phase capability-hardening risk,
not part of this gate. Next: the same standing Luna A/B sessions independently
review the exact accepted mechanism; strictest decision governs. Gate remains
**CLOSED**, services offline, and no privileged/live artifact or action exists.

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

Standing Luna A/B gave an initial exact-head GO for toolkit
`d7485311d52de206470b4f61c2c8eb3b613ee2ba` and source
`cd80a1feb34c16bb88fc294707a6492570e2ee90`, then clarified that result as
**CANDIDATE-ONLY**. We therefore skipped the knowingly disposable candidate.
The old `04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79`
candidate is obsolete, unapproved, unsigned, and uninstalled; no replacement
candidate has been constructed.

Toolkit Task 4A is `ffdcd2f` (parent `d748531`) and Task 4B is `aa84928`
(parent `ffdcd2f`); the reviewed combined range is `d748..aa`. Their exact
contracts include the repair for the earlier false-positive direct-import
test. Terra Task 4A specification/quality and Task 4B specification/quality
reviews were clean, and the combined Terra security verdict is GO. Controller
testing produced `458 passed in 77.84s`; state testing produced `151 passed in
55.25s`; six-file Python 3.13 compile, scoped Ruff, diff, ancestor/range, and
clean checks also passed.

Task 4A: on any normal `Exception` opening the four crypto files, attempt each
prior FD once in order; a per-FD `OSError` cannot stop later cleanup. Clear
`_owned`/`_material`, normalize an open `OSError` to protected `EvidenceError`,
and preserve any other exception after best-effort cleanup. Never catch
`BaseException` or retry a possibly reused FD. Task 4B: cleanup is permitted
only after exact verified seven-chain attempt/q1/provenance/q2/pre/final/binding.
After an inner GO close failure, record and verify that cleanup failure and
return NO-GO with a seven-plus-verified receipt; record/verify failure returns
bare NO-GO; preserve an already-correlated inner NO-GO. Close once, no retry,
no secret detail, no variable prefix. The old direct-import close test never
crossed its captured permit fence. Its replacement local test loader injects
one shared bootstrap-equivalent permit into the exact three modules; real state
tests verify protected record/verify semantics.

Current values: bootstrap (26,420 bytes)
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
(92,811 bytes)
`f48de368d22399d8ac0c1e5f90c0a0f3b2f1fe0eb1c1df417e0e459e5d835277`;
quarantine `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
state (85,635 bytes)
`8124da1ed0c9046f08234cebf8ae09e32603fa36dadae767c9564dd5da291caa`; smoke
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller
(7,249 bytes) `4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`.

The gate is still **CLOSED** and offline. Four user services are
inactive/dead/disabled with PID 0, and fixed privileged paths are absent. QR,
quarantine, smoke, observer, and relink are later hard gates. Persona evolution
is omitted; proactivity, consciousness, and speak-up are mandatory later.
Next: commit these docs; calculate plan/package hashes outside the self-hashed
plan/package; create a docs-only acceptance successor; have standing Luna A/B
review its exact head; construct one candidate only if both GO. No runtime,
auth, memory, service, network, or privileged action is authorized.

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
