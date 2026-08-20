# Luna re-review package — authenticated-bootstrap first WhatsApp rotation gate

## Re-review status — corrected code pending acceptance

The architecture correction is implemented at toolkit head
`f55a5e28cfabc5010aa84031bd699aa8e4054645`, but it is not yet Terra- or
Luna-accepted. The first live gate remains **CLOSED**. This package requests
fresh review of the exact authenticated-bootstrap design and does not authorize
installation, signing, authority preparation, runtime access, or any live
operation.

Fresh Terra review of source `48ca1a93957c85385ea2bfd995b9f3491fe9799f`
and toolkit `c4bc00a77ade352c24610d152cdeede67da9ec53` returned **REJECT**.
SPEC found that the installed bootstrap was not required to be exact mode
`0755`; SECURITY found that public `VerifiedRelease` construction/direct import
could reach production runtime and that owner approval remained procedural
rather than an authenticated, candidate-bound input. Production correction
`960168a` and test-only canonical-fixture correction `f55a5e2` close those
findings. Fresh Terra and both standing Luna reviews remain pending.

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
- Current correction range/head:
  `62acf5b73ca9b9abaed79004deed7f93ef2280b3..f55a5e28cfabc5010aa84031bd699aa8e4054645`.
- Required toolkit ancestor:
  `6ba55014242953e72ec7a29e75041f704452b885`.
- Source handoff repository/branch baseline: `/home/dm/Documents/yeoman`,
  `c/turn-engine-v2`, prior documentation head
  `48ca1a93957c85385ea2bfd995b9f3491fe9799f`. The final review must bind the
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

## Current synthetic correction evidence

At `960168a`, an aggregate claim initially missed nine stale fixtures: parent
verification exposed `450 passed, 9 failed`. Test-only `f55a5e2` centralizes the
canonical preflight-v5 fixture builder and removes those stale copies. Parent
verification at final clean head `f55a5e28cfabc5010aa84031bd699aa8e4054645`
then passed the exact seven-file suite: `459 passed in 60.93s`. Python 3.13
`py_compile`, scoped Ruff, `git diff --check`, and status all passed/clean. The
false aggregate claim is withdrawn; earlier current-head counts are stale. No
production artifact or live boundary was used.

The candidate bootstrap source submitted for re-review is
`bootstrap/first_gate_bootstrap.py`, SHA-256
`95f5d30c71ee3607c97d884a965b6897450521dbfcba647531f6505e30894ae6`.
Its intended installed path is
`/usr/local/libexec/yeoman/first_gate_bootstrap.py`, root-owned regular `0755`
and non-writable by group/world. The authority at
`/etc/yeoman/first-gate-release-authority.json` must be root-owned, regular, and
non-writable by group/world; as a JSON descriptor it is not executable. It pins
the fixed candidate, owner-approval, both decision, and final-release paths beneath
`/home/dm/.local/share/yeoman-program-release/whatsapp-first-gate-v1` as well
as the signer and allowed-signers content/hash. None of those installed/release
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

There is no monolithic manifest. The signed candidate v1 binds all static
executable/provenance facts: incident; exact source/toolkit commits;
plan/package hashes; required ancestor; bootstrap; invocation; exact module
names/paths/sizes/hashes; and toolchain. Owner approval v1 uses a fixed path and
distinct namespace and binds the exact unsigned canonical candidate hash,
fixed owner/scope, `APPROVED`, exact approval text/hash, and an inclusive window
no longer than 24 hours. Each signed decision v1 binds the
candidate hash, standing role/session, fixed scope, `GO`, exact review text/hash,
and the same bounded inclusive validity. Signed release v3 binds exact hashes of
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
| `incident_evidence_lib` | `fddc847bd34fb87d6e68837fb9af62196e2039706402fbf96e8412aa322cd721` |
| `whatsapp_rotation_state` | `285a49cf5a99cce38d64a06a9419a8c6a08ee0f37ef767ca6f26b9c709d345c1` |
| `whatsapp_rotation_first_gate` | `ab80783d4ad4f648525f38a424d5ae455d1ba483838a8dc78cbd772d5d8b3883` |

The bootstrap authenticates strict environment/flags/no-args/stdlib roots,
bounded no-follow authority/candidate/decision/release/signature/module reads,
held-FD SSH signature verification, and held-FD tool identities before loading
toolkit code through an in-memory finder. Strict schema checks refuse booleans
in integer fields. There is no global permit or reusable loader: `main()` makes
both locally only after the exact process contract plus every signature, module,
and tool is authenticated. Evidence/controller imports capture the same
per-execution permit, and normal imports fail before runtime. This is not a
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
- the exact seven-file 459-pass suite, Python 3.13/Ruff/diff/status results,
  the withdrawn 960 aggregate claim, its 450-pass/9-fail reproduction, and the
  test-only `f55a5e2` fixture closure;
- root-authority, candidate, owner-approval, decision, and release schemas; fixed paths,
  signer/allowed-signers content/hash, distinct namespaces, exact reviewer
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
