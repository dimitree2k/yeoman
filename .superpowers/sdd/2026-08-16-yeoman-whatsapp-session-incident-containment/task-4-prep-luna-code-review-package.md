# Luna re-review package — corrected first WhatsApp rotation gate

## Decision requested

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
  `c/turn-engine-v2`, `26cd7555ee9715b46afa8e49109f862c6b1b2933` before this
  documentation-only handoff commit.
- Source documentation handoff head: `726429e742daaa838e5ee5806faeec8f28f701d9`
  (`docs(incident): record corrected first Luna re-review`).
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

At the final head, every held ancestor FD is checked and closed fail-safe; the
legacy descriptor commitment is exact and all-field; `VerifiedV1Scope` is
authenticated; q2/final have dedicated causal receipts; and q1, provenance,
q2, pre, and final are durably bound.  The verifier is required at the
controller and second-Luna handoff.  Inventory, pre, post, and comparator have
exact provenance and semantic-quiescence continuity, and public compare fails
closed.

## Evidence to reproduce

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

The controller's fresh verification total was 376 passing tests.  The 285 + 91
split was solely the task-runner time boundary.  Final Terra returned SPEC
ACCEPT and QUALITY ACCEPT with no Critical, Important, or Minor finding.
Mutations were run and restored across rounds.

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
