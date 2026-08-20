# Luna re-review package — authenticated-bootstrap first WhatsApp rotation gate

## Re-review status — corrected code pending acceptance

The architecture correction is implemented at toolkit head
`c4bc00a77ade352c24610d152cdeede67da9ec53`, but it is not yet Terra- or
Luna-accepted. The first live gate remains **CLOSED**. This package requests
fresh review of the exact authenticated-bootstrap design and does not authorize
installation, signing, authority preparation, runtime access, or any live
operation.

Fresh Terra reviews rejected `9c6c9f`: SPEC required an immutable reviewed
candidate, exhaustive all-phase failure-prefix tests, and corrected launcher
wording; SECURITY required a signed candidate, authenticated decision envelopes,
final-release binding, exact preflight binding, real signature/mutation tests,
and boolean refusal. The corrected chain ends at `c4bc00a77ade352c24610d152cdeede67da9ec53`.
It replaces the old single-release description with a separately installed
trust anchor, signed candidate, signed reviewer decisions, signed final release,
held-FD execution, and a single-use causal evidence chain. Fresh Terra and both
standing Luna reviews remain pending.

## Decision requested after the correction

Standing Luna Agent A and Agent B must independently review this exact fixed
range and return **GO** or **NO-GO** for the first live gate.  The gate is
currently **CLOSED**.  Their strictest decision governs.

The initial fresh Terra acceptance and these standing Luna reviews are only a
mechanism/procedure gate. Then stop for separate explicit owner approval of
privileged bootstrap/authority/key preparation and signed-candidate creation.
Only after that approval may both standing Luna sessions review the exact
signed candidate bytes/hash and return GO. Their exact outputs are then captured
in signed time-bounded decision envelopes, followed by the signed final release
and any required re-review of differing installed/artifact bytes. Only then may
the first gate perform quiescence, read-only v1 inspection, pre-v2 capture, and
protected commitment handoff. It does **not** authorize an owner turn,
revocation, quarantine, QR/relink, observer startup, smoke, a message, or any
later production action. There is no live evidence yet.

## Exact scope and heads

- Toolkit repository/branch: `/home/dm/Documents/yeoman-migration-toolkit`,
  `c/yeoman-migration-toolkit`.
- Current correction range/head:
  `62acf5b73ca9b9abaed79004deed7f93ef2280b3..c4bc00a77ade352c24610d152cdeede67da9ec53`.
- Required toolkit ancestor:
  `6ba55014242953e72ec7a29e75041f704452b885`.
- Source handoff repository/branch baseline: `/home/dm/Documents/yeoman`,
  `c/turn-engine-v2`, prior documentation heads
  `726429e742daaa838e5ee5806faeec8f28f701d9` and
  `b74207c10940f9c13335ebe25b0d6e53af63f83e`, followed by documentation-only
  commits `5da50485a125c5adbc50318a24385ea017353ae1` and
  `4551d46b22d81ad01014d8862271b02b60a030c6`. The final review must bind the
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

At `c4bc00a77ade352c24610d152cdeede67da9ec53`, the stable exact seven-file
suite passed `441 passed in 57.42s`; Python 3.13 `py_compile` and scoped Ruff
passed, and the toolkit worktree was clean. The final head differs from that
441-pass head only by removing unused legacy bootstrap aliases; its focused
bootstrap suite passed 35 tests, Python 3.13/Ruff were clean, and the parent
reruns the exact suite before acceptance. Earlier 376/388/407 evidence
and every prior verdict are historical only. No production artifact or live
boundary was used.

The candidate bootstrap source submitted for re-review is
`bootstrap/first_gate_bootstrap.py`, SHA-256
`0ad5fda7cdcf738ec692269aab6eaa42458bdd73afeb9f9307af61c5778f2613`.
Its intended installed path is
`/usr/local/libexec/yeoman/first_gate_bootstrap.py`, root-owned regular `0755`
and non-writable by group/world. The root-owned non-writable authority is
`/etc/yeoman/first-gate-release-authority.json`; it pins the fixed candidate,
both decision, and final-release paths beneath
`/home/dm/.local/share/yeoman-program-release/whatsapp-first-gate-v1` as well
as the signer and allowed-signers content/hash. None of those installed/release
artifacts or their key, authority, candidate, decisions, release, or signatures
exists or was created by this phase.

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
names/paths/sizes/hashes; and toolchain. Each signed decision v1 binds the
candidate hash, standing role/session, fixed scope, `GO`, exact review text/hash,
and inclusive `issued_at <= now <= not_after`. The signed release v2 binds the
candidate hash, exact decision-envelope hashes, and a single-use
`authorization_id`. Candidate, decision, and release use distinct signature
namespaces. The owner signature attests exact transcript capture; the Luna
session IDs are trace/process evidence, not cryptographic nonrepudiation.
Reviewer identities are Agent A
`01a009b3-e740-7972-992b-5d63d6066b8c` and Agent B
`01a009b3-f495-7a90-8e50-8a22d4d306d2`.

Only three authenticated held-byte modules may be loaded:

| Closed module | SHA-256 |
| --- | --- |
| `incident_evidence_lib` | `6914cebaf9410b09991c03435777edec1f3ac75db01e11a31bf008639c575979` |
| `whatsapp_rotation_state` | `285a49cf5a99cce38d64a06a9419a8c6a08ee0f37ef767ca6f26b9c709d345c1` |
| `whatsapp_rotation_first_gate` | `edc08386ecf761276eeff22b0d346ae2dbbe63f1173b4e7cb4048c59e936ac4e` |

The bootstrap authenticates strict environment/flags/no-args/stdlib roots,
bounded no-follow authority/candidate/decision/release/signature/module reads,
held-FD SSH signature verification, and held-FD tool identities before loading
toolkit code through an in-memory finder. Strict schema checks refuse booleans
in integer fields. Direct worktree execution is always NO-GO. The causal chain
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
- the exact seven-file 441-pass suite, Python 3.13/Ruff result, focused
  35-pass cleanup check, and both `9c6c9f` Terra rejection/closure sets;
- root-authority, candidate, decision, and release schemas; fixed paths,
  signer/allowed-signers content/hash, distinct namespaces, exact reviewer
  sessions/review hashes, closed modules, toolchain, invocation, boolean
  refusal, and single-use authorization lifecycle;
- the proposed privileged installation and offline signing/key/authority
  preparation procedure, including verification of installed bootstrap and
  authority plus re-review of any differing installed artifact;
- proof that no install, authority, release key, candidate, decision, release,
  signature, runtime, service, key, owner, QR, observer, smoke, or message
  operation occurred.

Do not create any artifact during these reviews. The signed candidate comes
only after separate owner approval; only its exact hash/bytes can receive the
later two Luna GOs; only then may signed decisions and final release be created.
The eventual first-gate package must include exact invocation/exit/allowlisted
JSON, signed candidate/decisions/release and
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
