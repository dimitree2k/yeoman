# Luna code-review package — synthetic WhatsApp rotation preparation

## Decision requested

Review the accepted synthetic pre-owner bundle and return **GO** or **NO-GO**
for the first major live gate.  A GO authorizes **only** full quiescence and
real read-only `inspect-v1` followed by `capture-v2`.  It does not authorize
owner turns, revocation, quarantine, QR/relink, Bridge observer startup, or
smoke.  Those actions remain forbidden until a later, second evidence-bound
Luna GO after the real quiesced baseline commitments are available for review.

## Exact review scope

- Toolkit repository: `/home/dm/Documents/yeoman-migration-toolkit`
- Toolkit branch: `c/yeoman-migration-toolkit`
- Exact range: `6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`
- Reviewed toolkit head: `995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`
- Source handoff repository/branch: `/home/dm/Documents/yeoman`,
  `c/turn-engine-v2`
- Source head before this documentation commit:
  `8c583d8df0dc3f4e095a41dca40b881a79032135`

## Binding plans and durable reports

- prep-01 SHA-256: `0a668c0a5aea847beca23e62359cc480903f6b18d2c548e39c714160bd8267c6`
- prep-02 SHA-256: `600396a3ed9510003b48401dcb24cc6acd34f3b96b10afb191dcd71ce57f4e2a`
- prep-03 SHA-256: `6074344d5f1c6e6d6f1a3d40fab82983ee34825002a74fb224bcf415184abe30`
- `task-4-prep-orchestration-report.md`
- `task-4-prep-01-report.md`
- `task-4-prep-02-report.md`
- `task-4-prep-03-report.md`
- `task-1-3-recovery-report.md`
- `task-4-prep-integration-review-package.md`

## Accepted review history

- Terra accepted common/quarantine preparation at `68839a3`.
- Terra accepted state evidence at `f17ebe0`.
- Terra accepted the final prep-03 Tasks 1-3 code/task result at `b788608`
  with **SPEC ACCEPT** and **QUALITY ACCEPT**, no findings.
- The final integration/task review accepted `995c3ac` with **SPEC ACCEPT**
  and **QUALITY ACCEPT**, no findings.

## Fresh final synthetic verification

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
339 passed in 16.98s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_state.py scripts/whatsapp_rotation_smoke.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
All checks passed!

git diff --check
(no output)

git status --short
(no output; clean toolkit worktree)
```

## Architectural boundaries to verify

- Evidence is fixed-root, descriptor-relative, encrypted/signed, and exposes
  only public commitments; no plaintext auth, content, or JID reaches output.
- Full quiescence of Overseer, Bridge, and Gateway is mandatory before any
  real baseline read.  A mismatch is NO-GO.
- The v1-derived v2 baseline is exact-byte/state preservation; it does not
  semantically import legacy content.  Only a sealed, authenticated observer
  DAG may classify later additions.
- Owner evidence is four distinct exact host-backed turns with explicit
  predecessor checks.  Generic, combined, missing, swapped, or reused sources
  fail closed.
- Quarantine is non-restorable and may run only after the second Luna GO.
- Any smoke reservation is one attempt only. `external_effect_unknown` is
  terminal and never replayed; `capture_failed` blocks incident close.

## Residual risks and non-claims

All coverage is synthetic.  No production wrapper/controller, live key,
evidence, auth, service, socket, port, network, QR, JID, message, archive,
memory, owner turn, or receipt was used.  Runtime Overseer, Bridge, Gateway,
and Pinchtab remain inactive and disabled.  The review therefore cannot treat
synthetic passing tests as evidence of current live credentials, service
behavior, QR behavior, capture completeness, or WhatsApp delivery.

If GO is granted, retain Gateway/Overseer stopped and execute only the
specified first-gate quiescence plus read-only inspect/capture sequence.  Stop
and return for review with the real protected commitments before requesting the
second, evidence-bound Luna decision.
