# Luna re-review package — corrected first WhatsApp rotation gate

## Re-review status — correction underway

The first standing Luna re-review reached split outcomes at the exact heads
below. Agent B returned a narrow **GO**. Agent A returned the governing
**NO-GO**, so this gate remains **CLOSED**. No pending toolkit correction has
been implemented, verified, or accepted by this document.

Agent A's required corrections are: (1) replace PATH-resolved crypto
subprocesses with fixed, authenticated toolchain identities under a sanitized
environment; (2) correct the authoritative plan's missing fifth first-gate
controller; and (3) bind each run to an exact invocation/preflight plus a
protected attempt identifier and durable allowlisted failure record. Agent B's
narrow GO is not authority to bypass those governing requirements.

## Decision requested after the correction

Standing Luna Agent A and Agent B must independently review this exact fixed
range and return **GO** or **NO-GO** for the first live gate.  The gate is
currently **CLOSED**.  Their strictest decision governs.

A later GO may authorize only full quiescence and the fixed first-gate
controller's read-only v1 inspection, pre-v2 capture, and protected commitment
handoff.  It does **not** authorize an owner turn, revocation, quarantine,
QR/relink, observer startup, smoke, a message, or any production mutation.
There is no live evidence yet.  A second, evidence-bound Luna decision remains
required before every such later action.

## Exact scope and heads

- Toolkit repository/branch: `/home/dm/Documents/yeoman-migration-toolkit`,
  `c/yeoman-migration-toolkit`.
- Corrected toolkit range/head:
  `6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`.
- Source handoff repository/branch baseline: `/home/dm/Documents/yeoman`,
  `c/turn-engine-v2`, prior documentation heads
  `726429e742daaa838e5ee5806faeec8f28f701d9` and
  `b74207c10940f9c13335ebe25b0d6e53af63f83e`. The next review package must
  name the exact successor source and toolkit heads after the pending toolkit
  correction; neither is asserted as complete here.
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

## Required next Luna evidence

The next package must contain, and both standing Luna reviewers must bind their
decision to, all of the following:

- exact corrected source and toolkit heads, clean-worktree proof, and corrected
  orchestration-plan and this-package SHA-256 values;
- fixed tool paths, root-owned `0755` non-writable metadata, sizes, SHA-256
  values, and proof that the controller and children used the sanitized
  environment rather than caller `PATH` or loader/Python overrides;
- the zero-argument controller invocation, exit status, and only allowlisted
  public JSON output;
- protected attempt, `q1`, provenance, `q2`, pre-v2, final, binding, and—if a
  failure occurred—allowlisted protected failure commitments with their causal
  predecessors;
- identical quiescence across all required receipts, exact capture
  counts/bytes, and proof of no prohibited effect.

The fixed executable baseline to be implemented and proven is:

| Path | Mode / owner | Size | SHA-256 |
| --- | --- | ---: | --- |
| `/usr/bin/age` | root / `0755` | 4162312 | `374f65bfbb3646f15f5b3296507c7860067915da27af187995db7b4fec5fc035` |
| `/usr/bin/age-keygen` | root / `0755` | 2433984 | `859e2e6edbe0f5afe2a6e5c340f1f07969195de272888a303de2746902d46e5a` |
| `/usr/bin/ssh-keygen` | root / `0755` | 592376 | `e80f38fc532ca57dd82879c4dd169ae17bf76cc11c3da9c037c7495234fbb9bd` |
| `/usr/bin/systemctl` | root / `0755` | 331504 | `c418667a6fce4553f5faa61fd62f887787e7fc3d5ad5c2c4afff9d44ad09d475` |
| `/usr/bin/git` | root / `0755` | 4081272 | `a0e562e4bd3c4c79379e91d8c07a10104b2cefe8fac966dc6bd4874a57a807f3` |
| `/usr/bin/python3` | root / `0755` | 6673720 | `5a8d634b3cf42fa618c2a39c7e674206cefc3b0be3d2f7023d5b1f8ebb51a013` |

The production invocation is fixed to `/usr/bin/env -i PATH=/usr/bin:/bin
LANG=C LC_ALL=C TZ=UTC /usr/bin/python3
/home/dm/Documents/yeoman-migration-toolkit/scripts/whatsapp_rotation_first_gate.py`.
Any metadata, hash, path, environment, controller, or repository mismatch is
NO-GO before mutation.

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
