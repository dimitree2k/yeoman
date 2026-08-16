# Pause checkpoint — WhatsApp rotation smoke readiness

Date: 2026-08-16T20:15:34+02:00

Status: PAUSED BY OWNER. This is a recoverable WIP checkpoint, not implementation acceptance and not authorization for any live incident step.

## Runtime state

The owner asked to pause for two or three days and keep Yeoman offline. The active Terra implementer was stopped and the read-only Terra midpoint reviewer was interrupted. A fresh user-service check then confirmed all of these units inactive/dead:

- `yeoman-overseer.service`
- `yeoman-bridge.service`
- `yeoman-gateway.service`
- `yeoman-pinchtab.service`

Do not restart a service, open a QR, touch WhatsApp auth, create live evidence, send a message, or perform an owner turn merely to resume coding.

## Exact repository checkpoints

- Source `/home/dm/Documents/yeoman`: accepted plan/code baseline `7359cff390c8f60dc1decb0e455803df158b671b` (`docs(incident): bind quarantine receipt gates`); this pause note and ledger are committed on top, and the worktree must remain clean.
- Migration toolkit `/home/dm/Documents/yeoman-migration-toolkit`: clean at WIP checkpoint `fc86076352e8aaa417b7315688c02872bddf4826` (`wip(incident): checkpoint smoke readiness pause`). This commit is deliberately not accepted for merge or release.
- Target `/home/dm/Documents/yeoman-rework`: clean at `bdc4c8c9e33602c5c8abd69f054703469d212dab`; release-baseline work remains pending.

The accepted plan hashes remain:

- prep-01: `0a668c0a5aea847beca23e62359cc480903f6b18d2c548e39c714160bd8267c6`
- prep-02: `600396a3ed9510003b48401dcb24cc6acd34f3b96b10afb191dcd71ce57f4e2a`
- prep-03: `6074344d5f1c6e6d6f1a3d40fab82983ee34825002a74fb224bcf415184abe30`

## Verified synthetic checkpoint

Immediately before the WIP commit, the controller reran:

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py
264 passed in 13.38s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_state.py scripts/whatsapp_rotation_smoke.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py
All checks passed!
```

`git diff --check` was clean before the checkpoint. No live runtime, network, QR, auth, key, evidence, message, archive, or memory operation was used by implementation or tests.

## What the WIP contains

- common fixed-kind opaque protected-artifact verification;
- gate-bound quarantine PREPARED/later journals and successful receipt;
- recursive `quiescence -> revocation -> authorization -> quarantine receipt -> phone-ready` verification;
- state-comparator authentication of the gate chain and old-auth artifact;
- an early full-schema expectation, direct protocol-v3 response parser, observer event capture, zero-event close, and durable one-shot registry scaffold.

The WIP has not passed final task review. The last active implementation checkpoint had removed the reduced expectation schema and made synthetic smoke fixtures construct the real protected gate/inventory/current-auth graph. It had started returning an `accepted_no_reply` observer close rather than unconditional `capture_failed`.

## Required work on resume

1. Read this note and the three accepted prep plans before editing.
2. Confirm all Yeoman services are still inactive; do not start them for synthetic implementation.
3. Resume from toolkit commit `fc86076352e8aaa417b7315688c02872bddf4826` without resetting or dropping the WIP commit.
4. Finish held no-follow/current-owner/private-mode `creds.json` and stable canonical-auth production adapters, with the same checks at expectation build and immediately before send.
5. Finish the successful typed DAG: exact acceptance fields and timestamps, complete `accepted_no_reply`, causal `inbound_reply_observed`, unsolicited-event retention, full close/count/head/order/readiness/completeness rules, and prove the returned close passes `compare_v2`.
6. Finish observer disconnect/drop/overflow/publication-failure terminal behavior. Unknown/capture-failed must never have a successful close.
7. Finish the durable one-shot negative matrix: reservation/intent/attempt/network/response/acceptance crash windows, every post-send publication failure, corrupt/partial/prior receipt, same-nonce substitution, concurrency, and proof the lock is released before network/observer wait. Every existing nonce burns the attempt and forbids resend.
8. Remove test-only bypasses and unused scaffolding; public APIs must use fixed production configuration and never expose JID/content/auth material.
9. Complete mutation checks and the prep-03 report, then make normal atomic implementation commits after the WIP checkpoint.
10. Run an independent fresh Terra final implementation review, then synthetic integration/task reviews. Only after all pre-owner modules are accepted should standing Luna Agent A and Agent B receive the major-step cross-review package.

## Paused agent state

- `/root/smoke_readiness_impl`: stopped at the 264-test WIP checkpoint. It can be resumed with a follow-up task if its context is still available.
- `/root/smoke_midpoint_review`: explicitly interrupted before delivering findings. Restart or replace it only after implementation resumes.
- Standing Luna Agent A and Agent B were not contacted during this partial implementation turn.

No owner confirmation or live incident gate has been consumed. The next session begins with synthetic code only.
