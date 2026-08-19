# Task 4 synthetic rotation preparation integration package

## Scope

- Added only `tests/shared/test_whatsapp_rotation_integration.py` in the linked toolkit worktree.
- The disposable integration chain calls the existing underscored test cores only: protected evidence, v1-derived v2 state capture, quarantine/fingerprint, owner-turn recording, observer, direct smoke, and comparison.
- It covers full quiescence evidence, the exact pre-owner order, exact-byte fixture preservation, four distinct host turns, strict current-auth/fingerprint, authenticated pre-QR observer readiness, retained unsolicited plus causal quote-reply v3 frames, one successful direct fake self-send, post-capture comparison, and a separate terminal ambiguous-send/restart path.

## RED / GREEN

- RED: `uv run pytest -q tests/shared/test_whatsapp_rotation_integration.py` failed with `NameError: name 'run_synthetic_rotation_chain' is not defined` before the harness existed.
- GREEN: the new integration file passed `2 passed in 0.36s`.
- Final exact synthetic suite:

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
335 passed in 17.19s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_state.py scripts/whatsapp_rotation_smoke.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_integration.py
All checks passed!

git diff --check
(no output)
```

## Required mutation

Temporarily added a second `bridge_send_text_once(...)` in the `AmbiguousSend` handler of `scripts/whatsapp_rotation_smoke.py`. The integration run failed as required: `test_synthetic_rotation_chain_preserves_state_and_never_retries` raised a second `whatsapp_rotation_smoke.AmbiguousSend` from `_AmbiguousDirectBridge` at the injected second send. The line was restored before the final GREEN run; no production file remains changed.

## Commit

- `119f3ae test(incident): cover rotation preparation integration`

## No-live statement and concerns

No real `systemctl`, Bridge, Gateway, QR, JID/message, auth, key/evidence, archive/memory, socket/port, or network path was opened or invoked. All files, crypto, observer transport, v3 events, and direct-send responses were disposable `tmp_path` fakes. This is deliberately synthetic coverage, not a live readiness or delivery claim.

Remaining concern: the proof exercises the injected test seams and not a real environment, by design; live Task 4 gates remain separately required.

## Task-review fix Round 1

This follow-up starts from `119f3ae test(incident): cover rotation preparation integration` and modifies only the synthetic integration test.

Commit: `4f8653e test(incident): harden rotation integration trace`.

- Observable orchestration: `_SyntheticOrchestrationTrace` has a method for each legal stage and rejects a skipped/out-of-order call. The returned order is its recorded invocation history. `_ForbiddenProductionAdapters` replaces public production wrappers, live quiescence/state functions, production observer/bridge factories, and public smoke/quarantine entrypoints with counted fail-fast spies; `live_calls` derives from that count.
- Baseline binding: the chain reads and verifies the state pre-receipt, extracts its exact `common_quiescence`, and passes that same commitment through revocation and quarantine. A distinct, otherwise-valid quiescence receipt is rejected before any owner record.
- Observer lifecycle: one `asyncio.run` owns the whole chain. The authenticated fake transport starts a retained reader task and queue; it sends the actual `server.ts` message envelope shape as text converted to UTF-8 before `BridgeEventObserver.feed`. The unsolicited group event and the direct-self-chat causal quote both retain raw and normalized protected evidence and compare as one unsolicited plus one expected reply.
- Cleanup: `finally` shuts down the observer/transport, cancels and clears the reader, drains the queue, and closes the borrowed fake crypto exactly once. Captured output rejects clear self JID, content, auth bytes, and token text.

### Round-1 RED / GREEN and mutation evidence

- RED: the new trace and baseline tests first failed with `NameError: _SyntheticOrchestrationTrace is not defined` and `TypeError: ... unexpected keyword argument 'substitute_quiescence'`.
- An initial reader-driven GREEN attempt correctly failed `capture_failed` because a causal quote used a group `chatJid` while the direct acceptance was self-chat. The fake now retains the group event only for unsolicited traffic and uses the self-chat destination for the causal quote; the state comparator accepts the resulting graph.
- Output mutation: temporarily printed `synthetic-token` from the harness. `test_synthetic_rotation_chain_preserves_state_and_never_retries` failed because the captured-output guard found `synthetic-token` in stdout. The line was restored.
- GREEN before commit: the integration file passed `4 passed in 0.48s`; the exact five-file suite passed `337 passed in 16.89s`; the exact Ruff command reported `All checks passed!`; `git diff --check` had no output.

No real Bridge, Gateway, systemctl, QR, runtime auth/evidence, socket/port, message/JID, archive/memory, or network endpoint was called. The new spies fail on any production-wrapper bypass; all inputs and effects remain temporary fakes.

## Integration review-fix Round 2

This follow-up starts from `4f8653e test(incident): harden rotation integration trace` and changes only `tests/shared/test_whatsapp_rotation_integration.py`.

Commit: `995c3ac test(incident): bind rotation integration operations`.

- `_SyntheticOrchestrationTrace.perform(stage, predecessor, operation)` now owns every stage transition. It checks the exact immediately preceding token, invokes the real closure, and appends only after that closure returns. Baseline capture, owner records, quarantine/phone setup, observer startup, smoke plus ambiguity/restart proof, post capture/comparison, and the bound final synthetic Luna result each live inside their corresponding closures.
- `test_synthetic_rotation_refuses_moved_owner_operation_before_it_executes` attempts the quarantine-stage operation while the owner stage is expected; it fails before its closure runs and proves the real owner operation was not entered.
- Fake transport teardown has distinct `drain_calls`, `cancel_calls`, and `close_calls`. Operational event delivery uses `wait_for_events`; final cleanup drains once before observer shutdown, rejects a double close, cancels/clears the reader once, drains the queue, and verifies detached observer transport. Borrowed fake crypto closes exactly once.
- The captured stdout/stderr denylist now includes legacy/rotated auth, fixed smoke text, every self/group/sender/participant/mention JID, raw content and message labels, bridge-token fixture, and inventory label.
- The counted fail-fast fence includes evidence production configuration/public wrappers, state production runtime/call wrappers, quarantine production runtime/public entrypoints, and smoke production core/factories/public wrappers. The direct production-core-spy test proves invocation raises and increments, then resets its independent count before the chain derives `live_calls`.

### Round-2 RED / GREEN

- RED: operation-bound tests initially failed with missing `trace.perform` and absent `(smoke, "_production_core")` fence target.
- GREEN: `6 passed in 0.47s` for the integration file. The exact five-file synthetic suite then passed `339 passed in 17.03s`; the exact Ruff command reported `All checks passed!`; `git diff --check` had no output before commit.

All work remains synthetic/offline. No real service, Bridge, Gateway, QR, auth/key/evidence root, JID/message delivery, socket/port, archive/memory, or network resource was invoked.
