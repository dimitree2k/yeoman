# Pause checkpoint — WhatsApp rotation smoke readiness

Date: 2026-08-16T20:15:34+02:00

Status: PAUSED BY OWNER. This is a recoverable WIP checkpoint, not implementation acceptance and not authorization for any live incident step.

## Runtime state

The owner asked to pause for two or three days and keep Yeoman offline. The active Terra implementer was stopped and the read-only Terra midpoint reviewer was interrupted. A fresh user-service check then confirmed all of these units inactive/dead:

- `yeoman-overseer.service`
- `yeoman-bridge.service`
- `yeoman-gateway.service`
- `yeoman-pinchtab.service`

The startup-policy check found Overseer, Bridge, and Pinchtab enabled; Gateway was already disabled. To preserve the requested multi-day offline state across login/reboot, the pause disabled exactly Overseer, Bridge, and Pinchtab. All four units are now both inactive and disabled. Their pre-pause enablement state is therefore: Overseer enabled, Bridge enabled, Gateway disabled, Pinchtab enabled. Restore those three enablements only when the owner explicitly resumes runtime work; do not enable Gateway merely because the others were enabled.

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
2. Confirm all Yeoman services are still inactive and disabled; do not start or enable them for synthetic implementation. When the owner later authorizes live runtime work, restore the recorded pre-pause enablement state deliberately rather than enabling every unit.
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

## Resumed 2026-08-19

The owner explicitly resumed implementation. The controller revalidated all three clean worktrees, the preserved toolkit WIP `fc86076352e8aaa417b7315688c02872bddf4826`, and all four Yeoman services still inactive and disabled. Synthetic implementation does not restore the pre-pause service enablements.

Fresh checkpoint verification before edits: 264 focused tests passed in 13.02 seconds; scoped Ruff and `git diff --check` were clean. A fresh Terra gap audit found the preserved WIP incomplete, especially fixed production smoke APIs/adapters, held credentials and canonical-auth rechecks, authenticated protocol-v3 observation, causal inbound reply/complete close, durable capture-failure evidence, successful one-shot recovery, actual controller-to-comparator proof, and the full negative/mutation matrix.

The resumed task uses the dedicated ledger and recovery brief under `.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/`. A fresh Terra implementer owns the combined Tasks 1-3 recovery batch because the pause WIP already crosses their shared files. Standing Luna Agent A/B remain deferred until Terra implementation review and synthetic integration review accept the complete pre-owner package.

## Resumed completion 2026-08-19

The synthetic recovery implementation and integration review are now accepted:
Tasks 1-3 reached Terra SPEC ACCEPT and QUALITY ACCEPT at toolkit `b788608`,
and the full synthetic bundle reached SPEC ACCEPT and QUALITY ACCEPT at
`995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`, both with no findings.  Runtime
services remain offline: Overseer, Bridge, Gateway, and Pinchtab are inactive
and disabled; synthetic resumption did not restore any enablement.

The final exact five-file synthetic suite passed `339 passed in 16.98s`; the
exact scoped Ruff command passed, `git diff --check` was clean, and toolkit
status was clean at `995c3ac`.  The next step is the standing Luna major-turn
code review only.  No owner turn, quarantine, QR, relink, observer, or smoke
operation is authorized before the later evidence-bound Luna gates.

## First Luna review correction — 2026-08-20

The initial Luna package covered toolkit range
`6ba55014242953e72ec7a29e75041f704452b885..995c3ac76ed04a77ed55dd21fbc7d80bffd4e24e`.
Agent A gave a narrow conditional GO limited to quiescence and read-only
inspect/capture, identifying the missing fixed bounded controller and fresh
capture-bound q2/final read-only check.  Agent B returned the governing NO-GO,
identifying missing trusted-ancestor validation and authenticated
`VerifiedV1Scope`/artifact provenance.  Both noted that the recovery report
was referenced at a stale local path; its actual durable location is
`.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`.
No live action occurred.

The ordered correction commits are `e90402f7fd6132e0df919a9c4b6e5f2c91b03580`,
`7a2dad692511b056696aa0fda95327ec4f0bd14e`,
`9dc098c67b5c1afc0ba9bfd6439bae64cda54534`,
`056c2a10bde8a90a006d7d6ba2cba8d877ad1542`,
`c9d5a0c670f9a9cf426a6afbbaaa672d366524ba`, and
`62acf5b73ca9b9abaed79004deed7f93ef2280b3`.  Fresh controller verification
at that final head recorded 376 passing tests (285 + 91 because the task-runner
time boundary split the controller run), relevant Ruff clean, `git diff --check`
clean, and a clean toolkit worktree.  Final Terra SPEC ACCEPT and QUALITY
ACCEPT had no findings.

Runtime remains offline and disabled.  The first live gate is still **CLOSED**
until the same standing Luna Agents A and B independently re-review corrected
range `6ba55014242953e72ec7a29e75041f704452b885..62acf5b73ca9b9abaed79004deed7f93ef2280b3`;
their strictest decision governs.  Even a later GO may authorize only full
quiescence plus the fixed first-gate controller/read-only v1 inspect/pre-v2
capture/protected commitment handoff.  It does not authorize owner work,
revocation, quarantine, QR/relink, observer startup, or smoke.

## Standing Luna re-review update — 2026-08-20

Agent B returned a narrow GO, but Agent A returned the governing NO-GO at the
same historical source/toolkit heads. Its blockers are: PATH-resolved crypto
tools; the orchestration plan omitting the fifth first-gate controller; and no
exact invocation/toolchain provenance or protected attempt/failure identity.
The documentation correction is in progress; the required toolkit code and
tests are not complete or reviewed. Retain all partial evidence on any later
failure. Services remain offline and no live action is authorized.

## Authenticated-bootstrap correction update — 2026-08-20

Synthetic correction now ends at toolkit
`c4bc00a77ade352c24610d152cdeede67da9ec53`. Fresh Terra rejected `9c6c9f`:
SPEC required immutable reviewed candidate material, exhaustive all-phase
failure-prefix tests, and launcher wording; SECURITY required a signed
candidate, authenticated decisions, final-release and exact-preflight binding,
real signature/mutation tests, and boolean refusal. The closure is root
authority plus separate signed candidate v1, per-role signed decision v1, and
signed release v2—not a monolithic manifest. The authority pins fixed paths and
signer; decisions bind candidate hash, role/session/scope/GO, exact review
text/hash, and inclusive time validity; release binds candidate and decision
hashes plus single-use `authorization_id`; namespaces are distinct. The owner
signature attests transcript capture and Luna session IDs remain process/trace,
not cryptographic nonrepudiation.

The stable exact seven-file suite passed `441 passed in 57.42s`; Python 3.13
`py_compile`, scoped Ruff, `git diff --check`, and clean status passed. Final
head `c4bc00a` removes only unused legacy bootstrap aliases after that run; its
focused bootstrap suite passed 35 tests and Python 3.13/Ruff were clean. Parent
must rerun the exact suite at final head before acceptance. Bootstrap SHA-256 is
`0ad5fda7cdcf738ec692269aab6eaa42458bdd73afeb9f9307af61c5778f2613`.

No bootstrap, authority, release key, candidate, decision, release, or
signature exists or was created. First fresh Terra acceptance and standing-Luna
mechanism/procedure review; then separate explicit owner approval for privileged
bootstrap/authority/key preparation and signed-candidate creation; then both
Luna reviews of exact candidate bytes/hash; then signed decisions and release;
then re-review of differing installed/artifact bytes; only then invocation. No
runtime, service, owner, revocation, quarantine, QR, observer, smoke, or message
action is authorized. `/usr/bin/env` and `/usr/bin/python3.13` are root-owned
pathname launcher trust boundaries checked after startup; only later crypto and
`systemctl` subprocesses use held FDs. Persona evolution remains omitted from
target behavior with historical artifacts inert; proactivity, consciousness,
and speak-up remain deferred but mandatory later.
