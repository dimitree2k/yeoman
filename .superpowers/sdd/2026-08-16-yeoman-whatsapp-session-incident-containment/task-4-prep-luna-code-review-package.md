# Luna re-review package — authenticated-bootstrap first WhatsApp rotation gate

## Re-review status — current successor pending acceptance

The current production-capability successor is toolkit
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83`; bb37 is a historical **REJECT**,
not an accepted current head. The first live gate remains **CLOSED**. This
package requests fresh review of the exact authenticated-bootstrap design and
does not authorize installation, signing, authority preparation, runtime
access, or any live operation.

Fresh Terra review of source `1b18fba1b427e0069d80c8e0a583500d90e1cea0`
and toolkit `f55a5e28cfabc5010aa84031bd699aa8e4054645` returned **REJECT**.
It found that the implementation accepted an executable authority and that
ordinary imports still exposed state, evidence, and owner-turn production
actions. Toolkit `eb272c5` corrected the executable-authority/two-signer
contract, but fresh Terra review of source `41b2915684311010a6c96061afeb2bef1ab86dd3`
and toolkit eb272 returned **REJECT** because normally imported underscored
evidence/state/quarantine/smoke helpers still exposed production-capable
effects without the bootstrap permit. Toolkit a950 deleted those adapters, but
exact-head Terra review rejected its fixed legacy-descriptor/test-crypto FD
escape, raw evidence/key open helpers, and evidence/state descendant aliases.
bb37 attempted to permit-gate those paths, but review found ancestor `_Dir`
path/permit loss, a composable state raw parent FD, journal permit loss before
q1, quarantine overlap admission, and overclaiming documentation. Its result
is historical **REJECT**. Fresh Terra and both standing Luna reviews of the
current successor remain pending.

## Decision requested after the correction

Standing Luna Agent A and Agent B must independently review this exact fixed
range and return **GO** or **NO-GO** for the mechanism/procedure only. The live
gate remains **CLOSED**. Their strictest decision governs.

The initial fresh Terra acceptance and standing Luna reviews are only a
mechanism/procedure gate. Then prepare the exact unsigned canonical candidate
draft and stop. The first owner prompt must identify that draft's SHA-256 and
explicitly authorize privileged bootstrap/authority/key preparation plus
signing exactly that candidate. Only after this approval is captured in its
signed fixed-path envelope may the candidate be signed. Both standing Luna
sessions then review the exact signed candidate bytes/hash and return GO; their
exact outputs are captured in signed decisions, followed by the signed final
release and verification/re-review of differing installed/artifact bytes. Only
then may the first gate perform quiescence, read-only v1 inspection, pre-v2
capture, and protected commitment handoff. It does **not** authorize an owner turn,
revocation, quarantine, QR/relink, observer startup, smoke, a message, or any
later production action. There is no live evidence yet.

## Exact scope and heads

- Toolkit repository/branch: `/home/dm/Documents/yeoman-migration-toolkit`,
  `c/yeoman-migration-toolkit`.
- Current successor range/head:
  `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
- Full authenticated-bootstrap correction range:
  `f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
- Required toolkit ancestor:
  `6ba55014242953e72ec7a29e75041f704452b885`.
- Source handoff repository/branch baseline: `/home/dm/Documents/yeoman`,
  `c/turn-engine-v2`, documentation baseline
  `5e89c59e473496f6f5cdee8d33c26a4c755d28b0`. The final review must bind the
  exact successor source documentation commit that carries this package.
- Initial Luna package range (not the range to approve now):
  `6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.

## Initial decisions and correction closure

- Agent A initially gave a narrow conditional GO limited to quiescence and
  read-only inspect/capture.  Its Important gaps were a fixed bounded
  controller and a fresh capture-bound q2/final read-only check.
- Agent B initially gave **NO-GO**, which governed.  Its Important gaps were
  trusted-ancestor validation and authenticated `VerifiedV1Scope`/artifact
  provenance.
- Both identified the stale local `task-1-3-recovery-report.md` reference.
  The actual report is
  `.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`.
- No live action occurred during either initial review or the correction work.

The correction commits, in order, are:

1. `e90402f7fd6132e0df919a9c4b6e5f2c91b03580`
2. `7a2dad692511b056696aa0fda95327ec4f0bd14e`
3. `9dc098c67b5c1afc0ba9bfd6439bae64cda54534`
4. `056c2a10bde8a90a006d7d6ba2cba8d877ad1542`
5. `c9d5a0c670f9a9cf426a6afbbaaa672d366524ba`
6. `62acf5b73ca9b9abaed79004deed7f93ef2280b3`

At the historical final head, every held ancestor FD is checked and closed fail-safe; the
legacy descriptor commitment is exact and all-field; `VerifiedV1Scope` is
authenticated; q2/final have dedicated causal receipts; and q1, provenance,
q2, pre, and final are durably bound.  The verifier is required at the
controller and second-Luna handoff.  Inventory, pre, post, and comparator have
exact provenance and semantic-quiescence continuity, and public compare fails
closed.

## Historical evidence to reproduce

Run from `/home/dm/Documents/yeoman-migration-toolkit` at the corrected head:

```text
git rev-parse HEAD
# 62acf5b73ca9b9abaed79004deed7f93ef2280b3

uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py
# 285 passed

uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py tests/shared/test_whatsapp_rotation_first_gate.py
# 91 passed

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_state.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_first_gate.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_first_gate.py tests/shared/test_whatsapp_rotation_integration.py
# All checks passed!

git diff --check
# no output

git status --short
# no output; clean toolkit worktree
```

The controller's historical fresh verification total was 376 passing tests. The 285 + 91
split was solely the task-runner time boundary.  Final Terra returned SPEC
ACCEPT and QUALITY ACCEPT with no Critical, Important, or Minor finding.
Mutations were run and restored across rounds.

## Historical f55 correction evidence

At `960168a`, an aggregate claim initially missed nine stale fixtures: parent
verification exposed `450 passed, 9 failed`. Test-only `f55a5e2` centralizes the
canonical preflight-v5 fixture builder and removes those stale copies. Parent
verification at final clean head `f55a5e28cfabc5010aa84031bd699aa8e4054645`
then passed the exact seven-file suite: `459 passed in 60.93s`. Python 3.13
`py_compile`, scoped Ruff, `git diff --check`, and status all passed/clean. The
false aggregate claim is withdrawn; earlier current-head counts are stale. No
production artifact or live boundary was used.

## Historical eb272 verification and fresh rejection

At clean toolkit `eb272c52965a7d2dd1a4c66eaa40c8e499079f7a`, the parent exact
seven-file suite passed `472 passed in 61.11s`; `/usr/bin/python3.13`
`py_compile`, scoped Ruff, `git diff --check`, and status were clean. Fresh
Terra nevertheless rejected the normally callable private production paths;
the passing count did not prove the capability boundary. This evidence is
historical only and created no production artifact or action.

## Historical a950 verification and fresh rejection

At clean toolkit `a9504c233a14f92a12b4807855e94e621e8aa4ce`, independent parent
verification passed the exact seven-file suite: `433 passed in 56.67s`.
`/usr/bin/python3.13` compiled all six production files; the scoped Ruff set,
`git diff --check`, clean status, required-ancestor check, and scan for skipped
or hidden retired tests all passed. The lower count is deliberate deletion of
obsolete tests for removed production adapters, not skipped coverage. Exact-head
Terra nevertheless returned **REJECT**: ordinary import could compose the fixed
legacy descriptor with attacker-controlled test crypto and receive identity,
signature, and allowed-signers FDs; raw evidence/key open helpers were ungated;
and exact-only checks missed evidence/state root descendants. The passing suite
did not establish the claimed boundary.

The strict correction removes quarantine's fixed-root production runtime and
smoke's production Bridge client, observer transport/launcher, credential
readers, expectation/send paths, and lease registry. It guards the advertised
production config/tool/crypto and exact state runtime paths, removes arbitrary
capture subprocess helpers, and refuses exact fixed live auth through the
quarantine test seam. Pure/test-injected cores remain available for
synthetic verification; later production phases require a future authenticated
controller capability. These adapter-removal facts remain valid, but a950 is
historical and not accepted.

## Historical bb37 rejection and current opaque-capability successor

bb37 is historical **REJECT**. Its direct-path checks did not preserve `_Dir`
path/permit provenance: an ordinary importer could compose an ancestor handle
into legacy/key/age/signing reads. State returned a composable raw parent FD;
authenticated journal opens lost `config.permit` before q1; quarantine admitted
fixed ancestors/descendants; and the documentation overclaimed safety.

The current clean toolkit head is `2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
The review range is `bb37b577fc191084be74dff72a87b84ac9cf08ab..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`; the full correction range is
`f55a5e28cfabc5010aa84031bd699aa8e4054645..2967e451d10d83c0a07ff4ed54dad7a556b6bf83`.
Symmetric normalized separator-safe overlap rejects both protected ancestors
and descendants, while sibling-prefix fixtures remain valid. `_Dir` carries
path/permit provenance, exposes no `.fd`, and authorizes effect-specific
transitions before filesystem effects. Only `_authenticated_fd` with the exact
captured permit supports production crypto. State uses an opaque path+permit
`_RootHandle`; journal helpers propagate `config.permit`; quarantine rejects
any fixed-target overlap. No stale production adapters remain. Same-process
introspection and a malicious owner remain outside this private stale-entry
boundary.

Parent verification at the current head passed state `150 passed in 55.10s`
and the exact seven-file suite `445 passed in 77.47s`; six-file Python 3.13
compile, full scoped Ruff, diff/status, required-ancestor, no newly added
skips/retired coverage, and adapter scans passed. Two conditional bootstrap
`pytest.skip` cases for unavailable system `ssh-keygen -Y` support pre-exist;
this package makes no global zero-skip claim.

Exact production source identities at the current head are:

| Production source | SHA-256 |
| --- | --- |
| `bootstrap/first_gate_bootstrap.py` | `ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd` |
| `scripts/incident_evidence_lib.py` | `8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9` |
| `scripts/whatsapp_auth_quarantine.py` | `14df959da264d7580f3bc78a5bce693284e917bb799fddb35bd506e787297dcb` |
| `scripts/whatsapp_rotation_state.py` | `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16` |
| `scripts/whatsapp_rotation_smoke.py` | `4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5` |
| `scripts/whatsapp_rotation_first_gate.py` | `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e` |

The f55, eb272, a950, and bb37 evidence/hashes above are historical only. The current candidate bootstrap source submitted for re-review is
`bootstrap/first_gate_bootstrap.py`, SHA-256
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`.
Its intended installed path is
`/usr/local/libexec/yeoman/first_gate_bootstrap.py`, root-owned regular `0755`
and non-writable by group/world. The authority at
`/etc/yeoman/first-gate-release-authority.json` must be root-owned regular exact
mode `0644`; as a JSON descriptor it is not executable. Authority schema v4 pins
the fixed candidate, owner-approval, both decision, and final-release paths beneath
`/home/dm/.local/share/yeoman-program-release/whatsapp-first-gate-v1`, one canonical
Ed25519 release signer, and one owner signer. Each has separately hashed
allowed-signers content and a distinct decoded public-key blob. None of those installed/release
artifacts or their key, authority, candidate, owner approval, decisions,
release, or signatures exists or was created by this phase.

The only production invocation is byte/order exact:

```bash
/usr/bin/env -i LANG=C LC_ALL=C TZ=UTC PATH=/usr/bin:/bin /usr/bin/python3.13 -I -S -E -B /usr/local/libexec/yeoman/first_gate_bootstrap.py
```

`/usr/bin/python3` is a symlink and is not the authenticated launcher. Runtime
Git/current-head discovery is not part of authorization. `/usr/bin/env` and
`/usr/bin/python3.13` are root-owned pathname launcher trust boundaries checked
after startup; only later `ssh-keygen`, `age`, `age-keygen`, and `systemctl`
subprocesses execute by held FD. The fixed executable baseline is:

| Path | Mode / owner | Size | SHA-256 |
| --- | --- | ---: | --- |
| `/usr/bin/env` | root / `0755` | 68464 | `a1a366aeec990c18d7ff97358e523925e449e2df224029fe048fc3ae35a97720` |
| `/usr/bin/age` | root / `0755` | 4162312 | `374f65bfbb3646f15f5b3296507c7860067915da27af187995db7b4fec5fc035` |
| `/usr/bin/age-keygen` | root / `0755` | 2433984 | `859e2e6edbe0f5afe2a6e5c340f1f07969195de272888a303de2746902d46e5a` |
| `/usr/bin/ssh-keygen` | root / `0755` | 592376 | `e80f38fc532ca57dd82879c4dd169ae17bf76cc11c3da9c037c7495234fbb9bd` |
| `/usr/bin/systemctl` | root / `0755` | 331504 | `c418667a6fce4553f5faa61fd62f887787e7fc3d5ad5c2c4afff9d44ad09d475` |
| `/usr/bin/python3.13` | root / `0755` | 6673720 | `5a8d634b3cf42fa618c2a39c7e674206cefc3b0be3d2f7023d5b1f8ebb51a013` |

There is no monolithic manifest. Authority v4 is the root descriptor for two
signers, fixed paths, and bootstrap identity. The signed candidate v1 binds all static
executable/provenance facts: incident; exact source/toolkit commits;
plan/package hashes; required ancestor; bootstrap; invocation; exact module
names/paths/sizes/hashes; and toolchain. Owner approval v1 uses a fixed path,
the owner signer, and a distinct namespace and binds the exact unsigned canonical candidate hash,
fixed owner/scope, `APPROVED`, exact approval text/hash, and an inclusive window
no longer than 24 hours. Each signed decision v1 binds the
candidate hash, standing role/session, fixed scope, `GO`, exact review text/hash,
and the same bounded inclusive validity. The release signer signs candidate,
both decisions, and release. Signed release v3 binds exact hashes of
candidate, owner approval, and both decision envelopes plus a single-use
`authorization_id`. Candidate, owner approval, decisions, and release are exact
canonical bytes and use distinct signature namespaces. Signatures attest exact
transcript capture; session IDs remain trace/process evidence, not cryptographic
nonrepudiation.
Reviewer identities are Agent A
`01a009b3-e740-7972-992b-5d63d6066b8c` and Agent B
`01a009b3-f495-7a90-8e50-8a22d4d306d2`.

Only three authenticated held-byte modules may be loaded:

| Closed module | SHA-256 |
| --- | --- |
| `incident_evidence_lib` | `8be29004648eac2fe58841635c45414d1483102a2d2e7d00e1f1f5cb73d2a2b9` |
| `whatsapp_rotation_state` | `5f041e361fbff68809cbe77477749495991488ebc4000f59d87969f22fcd7e16` |
| `whatsapp_rotation_first_gate` | `b0e157bf8c356491091accdd21d58cdac6adfafbc006c9d6c5d8d6d228c8b58e` |

The bootstrap authenticates strict environment/flags/no-args/stdlib roots,
bounded no-follow authority/candidate/decision/release/signature/module reads,
held-FD SSH signature verification, and held-FD tool identities before loading
toolkit code through an in-memory finder. Strict schema checks refuse booleans
in integer fields. There is no global permit or reusable loader: `main()` makes
both locally only after the exact process contract plus every signature, module,
and tool is authenticated. Evidence, state, and controller imports capture the
same per-execution permit. Evidence fixed-root configuration/descendants, raw
directory/file opens, legacy-core composition, tool/crypto effects, state
fixed-root runtime/raw-root inventory, service quiescence, and legacy-manifest
reading require it. Quarantine/smoke production adapters are absent and their
legacy public wrappers refuse before effect; later phases require a future
authenticated controller capability. This is not a
same-process or malicious-owner sandbox; those are outside the threat model.
Preflight v5 carries bootstrap, decision, and owner hashes/metadata and
reconstructs exact canonical candidate/release hashes. The causal chain
is signed release -> single-use attempt reservation ->
attempt-linked `q1` -> provenance -> `q2` -> pre-v2 -> final -> binding.
`q1`/`q2`/final use held-FD pinned `systemctl` under a sterile environment;
every phase has an exact failure prefix and verified failure record. Failure
publication is best effort, the durable attempt and partial evidence remain,
and only safe commitments may be emitted.

## Required next review and owner gate

Fresh Terra and both standing Luna reviewers must bind their decisions to:

- exact corrected source documentation head and toolkit head/range, clean-tree
  proof, required ancestor, and exact plan/package/bootstrap/module hashes;
- state `150 passed in 55.10s`, the exact seven-file `445 passed in 77.47s`
  suite, six-file Python 3.13 compile, full scoped Ruff/diff/status,
  required-ancestor, no-new-skip/retired, and adapter-scan results; the f55,
  eb272, a950, and bb37 evidence remains historical only. Bootstrap retains
  two pre-existing conditional system-`ssh-keygen` skips;
- authority schema v4, candidate, owner-approval, decision, and release schemas; fixed paths,
  release/owner signer allowed-signers content/hash and distinct decoded key blobs,
  distinct namespaces, exact reviewer
  sessions/review hashes, closed modules, toolchain, invocation, boolean
  refusal, and single-use authorization lifecycle;
- the proposed privileged installation and offline signing/key/authority
  preparation procedure, including verification of installed bootstrap and
  authority plus re-review of any differing installed artifact;
- proof that no install, authority, release key, candidate, owner approval, decision, release,
  signature, runtime, service, key, owner, QR, observer, smoke, or message
  operation occurred.

Do not create any artifact during these reviews. After mechanism acceptance,
the exact unsigned draft hash must be named in the owner prompt. Only the
approved exact candidate may be signed; only its exact signed hash/bytes can
receive the later two Luna GOs; only then may signed decisions and release be created.
The eventual first-gate package must include exact invocation/exit/allowlisted
JSON, signed candidate/owner-approval/decisions/release and
attempt/q1/provenance/q2/pre/final/binding/failure commitments as applicable,
identical quiescence, exact counts/bytes, and no prohibited effect. The gate
then stops for the mandatory second Luna evidence review.

## No-live boundary

No live/systemctl/socket/key/v1/owner/QR/quarantine/observer/smoke production
call occurred.  Runtime remains offline and disabled.  Synthetic tests and
review acceptance do not establish credentials, service behavior, capture
completeness, QR behavior, or WhatsApp delivery.

## Supporting durable records

- `task-4-first-luna-review-response.md` — initial rulings and correction
  closure.
- `task-4-prep-orchestration-report.md` — preparation and re-handoff record.
- `task-4-prep-03-report.md` — accepted synthetic prep-03 report.
- `../2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`
  — actual recovery-report location.

## Current exact-head reconciliation — Luna remains blocked

This package's former `2967e451d10d83c0a07ff4ed54dad7a556b6bf83` scope is
historical **REJECT**, not a reviewable current head. Fresh Terra security
session `01a01dfa-4f0f-7f61-bb31-f52e6daa5046` found ordinary-import
composition through `_HeldAuth.parent_fd`/`.fd` from an allowed sibling into
fixed WhatsApp auth. Parallel Terra specification session
`01a01dfa-4f22-7500-b1e3-2df4834d1af7` returned GO, but the strictest security
REJECT governs. The former `445` seven-file and `150` state results and their
production hashes are historical only.

The pending exact-head review target is clean toolkit
`02720889cd4088dfae16f991fe19da460e5effb1`, parent
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83`: review
`2967e451d10d83c0a07ff4ed54dad7a556b6bf83..02720889cd4088dfae16f991fe19da460e5effb1`
and full range
`f55a5e28cfabc5010aa84031bd699aa8e4054645..02720889cd4088dfae16f991fe19da460e5effb1`.
`_HeldAuth` FDs and target names are name-mangled/opaque, with no global FD
registry/getter/raw directory-descriptor API. Effects are bounded to exact
`.auth-quarantine-<32hex>` siblings; exchange requires the same parent and
swaps private names internally; recursive children own duplicated parent FDs
and close acquired FDs on `dup`/`fstat` failure. Quarantine SHA-256 is now
`b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`; other
production hashes are unchanged.

Root evidence is `448 passed in 83.62s` for the seven-file suite and `150
passed in 63.78s` for state, plus six-file Python 3.13 compile, full scoped
Ruff, diff, required-ancestor, and no-new-skip checks. Two pre-existing
conditional system-`ssh-keygen` skips remain. Implementation design re-review
gave GO/no findings, but fresh exact-head independent Terra
specification/security acceptance is still pending. Luna is blocked and the
gate is **CLOSED**: no key, install, signing, authority, candidate, approval,
release, runtime, auth, message, artifact, or live action occurred; services
remain offline. Persona evolution is omitted; proactivity, consciousness, and
speak-up remain mandatory later.

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

## Superseding Task 4 cleanup hard-gate reconciliation

The exact-head standing Luna A/B disposition for toolkit
`d7485311d52de206470b4f61c2c8eb3b613ee2ba` and source
`cd80a1feb34c16bb88fc294707a6492570e2ee90` was initial GO, then clarified as
**CANDIDATE-ONLY**. The old
`04bbce963ad7070ee6b94426e99973f73900c6b3e394630040df9eb5f3557a79` candidate
was consequently not reused: it is obsolete, unapproved, unsigned, and
uninstalled, and no new candidate exists.

Review target is Task 4A `ffdcd2f` (parent `d748531`) plus Task 4B `aa84928`
(parent `ffdcd2f`), combined range `d748..aa`. Contracts include the repair
of the prior false-positive direct-import test. Terra Task 4A spec/quality and
Task 4B spec/quality reviews were clean; combined Terra security is GO.
Controller tests: `458 passed in 77.84s`; state tests: `151 passed in 55.25s`.
Six-file Python 3.13 compile, scoped Ruff, diff, required-ancestor/range, and
clean checks passed.

Task 4A requires that any normal `Exception` while opening four crypto files
causes every prior FD to be attempted once in order; per-FD `OSError` cannot
stop later cleanup. It clears `_owned`/`_material`, normalizes open `OSError`
to protected `EvidenceError`, and preserves other exceptions after best-effort
cleanup. It never catches `BaseException` or retries a possibly reused FD.
Task 4B permits cleanup only for exact verified seven-chain
attempt/q1/provenance/q2/pre/final/binding. An inner GO close failure is
recorded and verified, then returns NO-GO with a seven-plus-verified receipt;
record/verify failure is bare NO-GO, and an already-correlated inner NO-GO is
preserved. Close once: no retry, no secret detail, no variable prefix. The old
direct-import close test never crossed its captured permit fence. The
replacement local loader injects one shared bootstrap-equivalent permit into
the exact three modules; real state tests verify protected record/verify
semantics.

Artifact identity: bootstrap (26,420 bytes)
`ec6530570357921a4955d6a39d130db7153e67bc726ab1b61a98232d236370bd`; evidence
(92,811 bytes)
`f48de368d22399d8ac0c1e5f90c0a0f3b2f1fe0eb1c1df417e0e459e5d835277`;
quarantine `b20c9f586421af7bf3d45cfa5b4c581b6666e7f4af05fba8b01fef174199e019`;
state (85,635 bytes)
`8124da1ed0c9046f08234cebf8ae09e32603fa36dadae767c9564dd5da291caa`; smoke
`4f7a58867164c475dd6a647962258e5ff9292968a82c4eb7dbfce82452eb17e5`; controller
(7,249 bytes) `4e1b841d0a32769dc2f89df0e2d31abb9761701b39ff53fd9dc329ea537c8fcf`.

Gate state remains **CLOSED** and offline: four user services are
inactive/dead/disabled at PID 0, and fixed privileged paths are absent. QR,
quarantine, smoke, observer, and relink are later hard gates. Persona evolution
is omitted; proactivity, consciousness, and speak-up are mandatory later.
Next: commit docs; compute plan/package hashes outside their self-hashed files;
make a docs-only acceptance successor; standing Luna A/B reviews that exact
head; one candidate only if both GO. No runtime, auth, memory, service, network,
or privileged action is authorized.
