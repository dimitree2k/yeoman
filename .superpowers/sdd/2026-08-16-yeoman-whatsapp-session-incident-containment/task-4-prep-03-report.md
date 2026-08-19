# Task 4 prep-03 smoke-readiness report

## Scope and result

This records the accepted synthetic-only completion of prep-03 Tasks 1-3 and
the Task 4 report requirement.  Work occurred only in the migration toolkit,
using disposable pytest roots, synthetic crypto, fake services, in-memory
observer transport, and fake direct Bridge responses.  The resulting controller
and comparator prove the required protected evidence graph in that synthetic
environment; they do not establish live readiness, relink, or delivery.

The accepted plan hashes are:

- prep-01: `0a668c0a5aea847beca23e62359cc480903f6b18d2c548e39c714160bd8267c6`
- prep-02: `600396a3ed9510003b48401dcb24cc6acd34f3b96b10afb191dcd71ce57f4e2a`
- prep-03: `6074344d5f1c6e6d6f1a3d40fab82983ee34825002a74fb224bcf415184abe30`

## Requirement coverage

- Task 1: separate, exact protected owner turns; verified quarantine ancestry;
  post-relink phone-ready/current-auth/device-inventory bindings; no reused,
  generic, combined, or disclosed owner evidence.
- Task 2: pre-QR authenticated protocol-v3 observer readiness; raw encrypted
  frames and normalized contiguous provenance; retained unsolicited traffic;
  only a sealed successful `observer-close-v2` reaches the state comparator.
- Task 3: held-auth/self-identity-bound direct self-chat expectation; one
  durable attempt per keyed nonce; exact acceptance binding; bounded causal
  quote observation; terminal `external_effect_unknown` or `capture_failed`
  without retry or replay.
- The real controller-produced zero-event, unsolicited-event, and causal-reply
  close graphs pass the exact state comparator under the synthetic cores.

## RED, GREEN, and restored mutations

The recovery batch began from paused WIP `fc86076352e8aaa417b7315688c02872bddf4826`.
Its RED cases exposed missing fixed public smoke APIs, unbound observer
lifecycle/readiness, missing held-auth rechecks, incomplete close/causal reply
handling, and incomplete durable one-shot recovery.  The recovery report
contains the command-level RED/GREEN chronology and reviewer-fix rounds.

All four required prep-03 mutations were executed and restored:

1. Temporarily accepted a generic owner approval; the pre-action gate test
   failed because it no longer raised `SmokeError`.
2. Temporarily discarded an unsolicited event; the controller/comparator test
   failed because the result became `preserved` rather than classified growth.
3. Temporarily retried after `AmbiguousSend`; the second synthetic send raised
   `AmbiguousSend`, proving retry is forbidden.
4. Temporarily mapped accepted timeout to `external_effect_unknown`; the
   accepted-no-reply test failed with the wrong terminal state.

Each exact branch was restored before the final verification.  The separate
integration retry mutation is recorded in the integration package.

## Review and fix chronology

- Common evidence/quarantine preparation received Terra acceptance at toolkit
  `68839a3`.
- State-evidence preparation received Terra acceptance at `f17ebe0`.
- Prep-03 recovery fixes progressed through production-boundary, observer,
  lease, transport, failure, and cancellation reviews.  The final Tasks 1-3
  code/task acceptance was at `b788608`; Terra returned **SPEC ACCEPT** and
  **QUALITY ACCEPT**, with no findings.
- The synthetic integration review then hardened execution ordering, operation
  binding, production-path fences, lifecycle teardown, and output denylisting.
  The final integration/task acceptance was at `995c3ac`; the reviewers again
  returned **SPEC ACCEPT** and **QUALITY ACCEPT**, with no findings.

## Final verification and commits

Toolkit reviewed range: `6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.
The prep-03 final code/task head is `b788608`; full-bundle integration head is
`995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.

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

## No-live statement, limitations, and next gate

No production wrapper/controller, real key, evidence root, auth tree, service,
socket, port, network, QR, JID, message, archive, memory, owner turn, or live
receipt was invoked, read, created, changed, or transmitted.  Runtime services
Overseer, Bridge, Gateway, and Pinchtab remain inactive and disabled; no live
path was touched during this resumed synthetic work.

Synthetic acceptance does not authorize Task 4 live work.  The next and only
requested decision is the standing Luna major-turn **code review**.  A first
Luna GO may authorize only full quiescence and real read-only
`inspect-v1`/`capture-v2`.  Owner turns, quarantine, QR, relink, observer
startup, and smoke remain forbidden until a later second, evidence-bound Luna
GO after those real baseline commitments are reviewed.
