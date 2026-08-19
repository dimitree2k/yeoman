# Prep-03 Tasks 1-3 recovery report (partial implementation checkpoint)

## Scope and commits

- Preserved starting checkpoint: `fc86076352e8aaa417b7315688c02872bddf4826`.
- `15eb49a feat(incident): harden smoke controller interfaces`
  - Added the exact public `build_smoke_expectation(current_auth, inventory, observer_ready)` and async `run_smoke(expectation, observer_ready)` APIs with no public runtime/client injection.
  - Added pinned direct protocol-v3 client wiring; raw observer ingestion now derives provenance from the raw envelope only.
  - Added `CaptureWindow` snapshots and a protected disconnect `capture_failed` record.
- `426dbf7 fix(incident): bind smoke to held auth identity`
  - Reuses the canonical held-auth traversal, no-follow `creds.json` open/stat checks, `me.id` parsing/JID normalization, and tree-HMAC recheck path.
- `18f5a96 feat(incident): seal causal smoke observer receipts`
  - Raw malformed observer input now records `capture_failed` when possible without erasing prior events.
  - Captured inbound events are classified by the accepted message ID; only the causal one produces `inbound-reply-v2`, while unsolicited events remain in the sealed observer chain.
  - Successful observer closes are durably stored against the burned nonce, so a rerun returns the same close and cannot resend.
- `80ae755 test(incident): prove controller smoke close comparator graph`
  - Proves real `run_smoke` close commitments, not hand-built substitute graphs, pass `compare_v2` for the zero-event, retained-unsolicited, and causal-quote cases.
  - Corrected the expectation's account binding to the protocol-v3 account (`default`) rather than the self destination JID; the comparator requires observer and acceptance account provenance to agree.

Changed tracked files:

- `scripts/whatsapp_rotation_smoke.py`
- `tests/shared/test_whatsapp_rotation_smoke.py`

## TDD evidence

Each command was run from `/home/dm/Documents/yeoman-migration-toolkit`.

| Behavior | RED command and observed result | GREEN command and observed result |
| --- | --- | --- |
| Fixed public expectation API | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_public_smoke_build_api_has_only_the_fixed_production_inputs` -> `AttributeError: ... has no attribute 'build_smoke_expectation'` | Same command -> `1 passed in 0.26s` |
| Fixed public run API | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_public_smoke_run_api_has_no_caller_supplied_bridge` -> missing `_production_bridge_client` | Same command -> `1 passed in 0.17s` |
| Raw protocol-v3 observer input | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_observer_derives_provenance_only_from_authenticated_raw_v3_frame` -> `TypeError: ... feed() missing 1 required positional argument: 'event'` | Same command -> `1 passed in 0.15s` |
| Capture-window snapshot | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_capture_window_is_an_in_memory_ready_bound_snapshot` -> missing `capture_window` | Same command -> `1 passed in 0.15s` |
| Durable disconnect failure | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_observer_disconnect_durably_records_capture_failed_without_erasing_events` -> missing `terminal` | Same command -> `1 passed in 0.20s` |
| Comparator controller closes | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_controller_unsolicited_event_close_passes_real_state_comparator tests/shared/test_whatsapp_rotation_smoke.py::test_controller_causal_reply_close_passes_real_state_comparator` -> two `mismatch` results | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_controller_zero_event_close_passes_real_state_comparator tests/shared/test_whatsapp_rotation_smoke.py::test_controller_unsolicited_event_close_passes_real_state_comparator tests/shared/test_whatsapp_rotation_smoke.py::test_controller_causal_reply_close_passes_real_state_comparator` -> `3 passed in 0.57s` |

Focused smoke regression after the first checkpoint:

```text
uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py
15 passed in 0.59s
```

## Full focused verification

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
272 passed in 13.45s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)
```

## Task-review fix Round 1

- `c0d02e8 fix(incident): bind pre-qr smoke observer` is the atomic review-fix follow-up; prior recovery commits remain preserved.

### Review findings fixed

| Finding | Synthetic regression coverage | Implementation |
| --- | --- | --- |
| C1: public lifecycle used a fresh/unbound observer | `test_public_run_refuses_before_reservation_without_the_same_live_observer`; `test_public_run_uses_same_ready_observer_and_retains_raw_event_through_close`; `test_production_owned_observer_is_started_before_qr_and_is_singleton` | Added a private production-owned pre-QR observer registry. Only `_start_production_observer_before_qr` starts the fixed observer; public `run_smoke` consumes the exact registered ready commitment and otherwise refuses before reservation/network. |
| C2: immediate close and fixture times | `test_observer_waits_for_a_causal_reply_arriving_during_bounded_window`; `test_nonfixture_clock_chronology_passes_the_real_close_comparator` | Added private UTC/monotonic/wait test seams; production defaults use UTC and monotonic clocks. The observer drains until its bounded deadline or causal reply, then seals. |
| I1: observer transport optional | `test_production_observer_requires_mandatory_authenticated_transport` | Production-configured observers now require the fixed authenticated transport seam, and refuse absent/wrong protocol/auth before ready publication. |
| I2: first-run close not fully verified | `test_first_run_close_dag_verification_failure_becomes_capture_failed` | `run_smoke` runs the full observer graph verifier before returning/persisting a successful close; verifier failure writes a capture-failure terminal when possible. |
| I3: inventory accepted insufficient current-auth proof | `test_inventory_reverifies_current_auth_before_recording` (serialization/artifact/HMAC matrix) | Reused a single strict current-auth verifier for inventory and expectation construction; it rechecks schema, tree HMAC, quarantine receipt/artifact, serialization, and phone-ready ancestry. |
| I4: sealed reservation nonce was not compared to its keyed filename | `test_hmac_valid_wrong_nonce_reservation_is_unknown_without_send` | Reservation reference verification now receives and compares the expected keyed nonce. |

### Round-1 RED/GREEN evidence

The initial focused RED command covered the six direct reviewer regressions and produced five expected failures: public run entered the bridge without an observer, production readiness accepted absent transport, no-reply completed immediately, a first-run close returned success despite a failing graph verifier, and inventory accepted a wrong current-auth HMAC/artifact. The original wrong-nonce test already returned unknown through a separate invalid-reference route; an explicit nonce equality check was nevertheless implemented and retained.

```text
uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py
49 passed in 2.68s

uv run ruff check scripts/whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_smoke.py
All checks passed!
```

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
303 passed in 15.45s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)
```

No real Bridge, Gateway, QR, service, auth/key/evidence, socket/port, message, archive, memory, or network path was touched; the observer transport was never invoked outside disposable fakes.

## Task-review fix Round 2

- `082c14a fix(incident): retain authenticated smoke observer` is the atomic Round-2 follow-up.
- `d41b4cc test(incident): cover observer transport handshake` adds the isolated websocket contract integration coverage.
- C2 timeout: `wait_for_quote_or_timeout` now catches only a normal waiter timeout, rechecks the monotonic deadline, and seals one complete `accepted_no_reply` close at expiry. `test_nonzero_timeout_seals_accepted_no_reply_without_capture_failure_or_busy_loop` proves comparator acceptance and one wait.
- Real transport/protocol: `_ProductionObserverTransport` uses the same pinned loopback URL, token-bound v3 `health` request, matching authenticated `response`, and `protocolVersion == 3` validation as the runtime source. It retains that websocket, starts one reader task, and passes subsequent raw frames to the observer. `test_actual_bridge_v3_message_envelope_is_retained_and_normalized` covers the actual `server.ts` message envelope and sender/chat/mentions provenance.
- `test_production_transport_health_handshake_reader_and_cleanup_are_synthetic` supplies only an in-memory websocket/module fake and proves exact token-bound health request, v3 response validation, retained-reader raw delivery, and deterministic cancellation/close with no clear token/raw/JID/content output.
- Lifecycle: a ready observer is a same-process `_ProductionObserverLease` with `ready/claimed/closed/failed` state, expiry, per-lease claim lock, and start lock. Expired/claimed entries refuse before reservation; run cleanup cancels reader and closes the socket before crypto. `test_expired_registered_observer_refuses_before_reservation` covers stale refusal.
- RED/GREEN: before the C2 change, a normal `asyncio.wait_for` timeout escaped the local wait loop and became a capture failure; the new controllable-clock regression passes. Before the parser change, actual `server.ts` frames were rejected by the invented synthetic shape; the actual-envelope regression passes.

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
306 passed in 16.23s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)
```

Final Round-2 checkpoint after the transport test commit: all four focused files passed with `307 passed in 15.79s`; the exact four-script/four-test Ruff command and `git diff --check` were clean, and the worktree was clean.

## Task-review fix Round 3

- `5f53c1a fix(incident): clean abandoned observer leases` is the atomic lifecycle cleanup follow-up.
- `8455fc3 fix(incident): harden observer transport frames` converts websocket text to exact UTF-8 bytes before protected raw retention, fails closed on any reader exception, uses validated sender precedence, routes health responses privately, and requires unchanged dropped-queue attestation before close.
- The legacy test shape remains unavailable to production-configured observers. `test_transport_converts_real_websocket_text_frame_to_protected_raw_evidence` proves actual string-frame capture by the real observer; `test_actual_group_sender_prefers_participant_before_valid_sender_id` proves LID group provenance.
- The wait race is closed by clearing the signal before each event-chain scan. Timeout regression remains covered by `test_nonzero_timeout_seals_accepted_no_reply_without_capture_failure_or_busy_loop`.
- Round-3 lifecycle cleanup uses the single identity-checked `_cleanup_production_lease`; it handles abandoned/failed/closed leases, shutdown, and exact registry/active removal. `test_abandoned_lease_cleanup_is_identity_checked_and_idempotent` covers repeated abandonment cleanup.

Final Round-3 focused verification: `310 passed in 15.96s`; exact four-script/four-test Ruff and `git diff --check` were clean, with a clean worktree.

## Self-review and remaining requirements

The new public signatures, raw-envelope-only provenance, no-follow held-auth read, causal inbound receipt, successful one-shot close replay, and controller-produced comparator closes are implemented and covered by the focused suite. The temporary test mutation requirements were **not yet executed**. The recovery batch remains incomplete: it still needs the complete observer publication-failure matrix and genuine bounded wait, crash/concurrency/corruption/substitution one-shot matrix, required mutations, removal of unused scaffolding, and final requirement-by-requirement review.

No real Bridge, Gateway, service, QR, JID, message, owner turn, auth, key/evidence, archive, memory, socket/port, or network path was touched. All executions used only source files and disposable pytest temporary fakes; the production paths added here were not invoked.

## Round-4 recovery follow-up (68fa819)

### Implemented recovery requirements

- The keyed `.smoke-one-shot` record is now a minimal durable state machine: `reserved`, `intent_durable`, `attempt_durable`, `terminal`, or `closed`.  It persists and HMAC-seals the nonce, expectation, client-id binding, intent, attempt, and final reference as each transition happens under the short registry lock.
- Any existing nonce is irreversibly burned.  Before returning an earlier terminal or a successful close, every stored reference is structurally re-read and cryptographically revalidated: expectation/client-id binding, intent predecessor, attempt predecessor, close acceptance chain, or failure terminal's exact expectation/attempt binding.  A corrupt, incomplete, substituted, or mismatched record returns `external_effect_unknown` without a second send.
- The registry lock is released by `_with_reservation` before the direct send and observer wait; it never spans network I/O.  Existing reservations at every crash point therefore remain no-send unknowns.
- A failed raw-artifact write, normalized-event write, malformed raw frame, disconnect, overflow, inbound-receipt path, or observer-close publication burns the capture as `capture_failed`; preserved events remain referenced by the failure terminal.  A close is marked successful only after its protected publication returns.
- A direct response followed by acceptance publication failure is terminal `external_effect_unknown` (with a durable terminal when that publication is available) and cannot retry.  Ambiguous send behavior remains one attempt only.

### Round-4 TDD evidence

All commands ran from `/home/dm/Documents/yeoman-migration-toolkit` using disposable pytest directories and fakes only.

| Behavior | RED command and observed result | GREEN command and observed result |
| --- | --- | --- |
| Substituted HMAC-valid successful journal expectation | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_existing_successful_nonce_with_substituted_expectation_is_unknown_without_send` before implementation -> expected `external_effect_unknown`, got `accepted_no_reply` | Same focused group after implementation -> `4 passed in 0.41s` (initial group run had one test-fixture fault, corrected before the green rerun) |
| Raw artifact publication fault preserves prior evidence and burns capture | Same four-test command before implementation -> uncaught `EvidenceError` and no terminal | Same focused group after implementation -> `4 passed in 0.41s` |
| Observer close publication fault is not a successful close | Same four-test command before implementation -> terminal state remained `accepted_no_reply` | Same focused group after implementation -> `4 passed in 0.41s` |
| Acceptance receipt publication fault has no retry | Same four-test command before implementation -> uncaught `SmokeError` | Same focused group after implementation -> `4 passed in 0.41s` |

The focused smoke suite after the follow-up was:

```text
uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py
25 passed in 1.34s

uv run ruff check scripts/whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_smoke.py
All checks passed!
```

### Required temporary mutations (each restored immediately)

| Mutation | Command | Expected RED evidence | Restored GREEN evidence |
| --- | --- | --- | --- |
| Generic owner approval accepted | Temporarily changed `source.exact_content != ALL_DEVICES_REVOKED_TEXT` to `False`, then ran `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_pre_action_gate_refuses_generic_combined_and_reused_owner_turn` | `Failed: DID NOT RAISE SmokeError` | Same command after exact restoration: `1 passed in 0.17s` |
| One unsolicited event discarded | Temporarily returned before persisting `message_id == "unsolicited"`, then ran `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_controller_unsolicited_event_close_passes_real_state_comparator` | expected classified additions, got `preserved` | Same command after exact restoration: `1 passed in 0.24s` |
| Retry after ambiguous send | Temporarily added a second `bridge_send_text_once` in the `AmbiguousSend` handler, then ran `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_ambiguous_response_is_terminal_and_never_retried` | second synthetic `AmbiguousSend` escaped after the retry | Same command after exact restoration: `1 passed in 0.20s` |
| Accepted timeout mapped to unknown | Temporarily constructed `SmokeResult("external_effect_unknown", close)`, then ran `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_accepted_protocol_response_seals_complete_observer_close` | expected `accepted_no_reply`, got `external_effect_unknown` | Restored exact construction; final focused suite recorded below |

### Commit and self-review

- Preserved all prior recovery commits through `80ae755`; no reset, amend, squash, or WIP rewrite was used.
- `68fa819 fix(incident): seal smoke one-shot publication failures` adds the state-machine/reference validation and publication-failure regressions.
- No obsolete production scaffolding was removed: the remaining underscore seams are actively used only by the existing synthetic tests, while public APIs retain fixed production signatures.
- Outstanding concern before this report's final verification block: no production adapter was invoked by design, so the only remaining validation is static/synthetic.  No runtime services, Bridge, Gateway, QR, socket, port, JID, message, owner turn, auth, keys, evidence, archive, memory, or network path was touched.

### Final Round-4 verification

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
279 passed in 14.35s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)

git status --short
(no output; clean linked worktree)
```

## Round-8 reviewer cancellation follow-up

`attest_complete` now cleans the exact request-id mapping only when it still
points at its own future, and cancels that future in `finally` only when it is
unfinished. Thus cancellation during websocket `send` propagates unchanged but
cannot leak a pending waiter; a completed reader-routed response remains intact
and is not consumed or cancelled by cleanup. The existing exception path still
maps non-cancellation transport/protocol faults to `SmokeError`.

| Behavior | RED | GREEN |
| --- | --- | --- |
| Cancellation during final health send | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_final_health_send_cancellation_clears_and_cancels_its_pending_future tests/shared/test_whatsapp_rotation_smoke.py::test_final_health_completed_response_is_not_cancelled_by_cleanup` -> `1 failed, 1 passed`: pending map cleared but captured future remained pending | Same command -> `2 passed in 0.25s`: cancellation propagates, map is empty, created future is cancelled; completed response future remains completed and not cancelled. |

Only in-memory fake websocket/future objects were used; no live transport or
runtime resource was opened.

Final Round-8 verification after commit `b788608`:

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
333 passed in 16.81s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)

git status --short
(no output; clean linked worktree)
```

## Round-7 reviewer re-review fixes

### Final-health send failure and close ownership

`_ProductionObserverTransport.attest_complete` now owns its pending future from
creation through a single `finally`: any non-cancellation send, receive, parse,
or protocol exception cancels an unresolved future, removes its request-id
entry, and raises only the public-safe `SmokeError`. `BridgeEventObserver.close`
therefore burns capture; the smoke controller stores its durable
`capture_failed` terminal when publication remains available, so the nonce
cannot send again. Cancellation is deliberately re-raised.

`BridgeEventObserver.shutdown` atomically swaps `self.transport` to `None`
before awaiting `close`. Consequently startup's local failure cleanup and the
public wrapper may both call shutdown without a second transport close; the
same holds for concurrent shutdown callers.

### Round-7 RED/GREEN evidence

| Behavior | RED | GREEN |
| --- | --- | --- |
| Final health send raises a ConnectionClosed-like transport exception | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_final_health_send_loss_is_capture_failed_and_burns_the_nonce tests/shared/test_whatsapp_rotation_smoke.py::test_public_start_failure_closes_nonidempotent_transport_and_crypto_once tests/shared/test_whatsapp_rotation_smoke.py::test_concurrent_shutdown_detaches_nonidempotent_transport_once` -> uncaught test `ConnectionClosedError` from `attest_complete` | Same command -> `3 passed in 0.78s`; first run returns `capture_failed`, no observer-close success, and rerun uses the terminal with no second fake send. |
| Public start readiness failure followed by wrapper cleanup | Same RED command -> public-safe `SmokeError` was masked by `RuntimeError: second close` from a non-idempotent fake transport | Same GREEN command -> exactly one close and one crypto close, with empty registry/active state. |
| Concurrent shutdown | The finalized direct regression invokes two shutdown callers against a non-idempotent transport; it verifies one close and detached ownership on the GREEN implementation. |

These tests remain entirely synthetic: an actual `BridgeEventObserver`, fake
in-memory websocket/transport, disposable evidence root, and fake direct Bridge
client only. No live path was invoked.

Final Round-7 verification after commit `9277f31`:

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
331 passed in 16.71s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)

git status --short
(no output; clean linked worktree)
```

The completed synthetic requirements are public fixed APIs; exact owner-source and protected-artifact checks; held/no-follow auth identity and immediate recheck; authenticated raw v3 observer capture; causal/unsolicited retention; durable no-retry keyed state; failure terminals; successful close replay; and controller-produced comparator closes.  The only operational boundary is intentional: all proof remains synthetic, and no real endpoint or credential was exercised.

## Round-6 reviewer-closure follow-up

### Architectural contract

The public pre-QR entry point owns one same-process lease. A closable observer
transport becomes owned before its connection/readiness awaits, so cancellation
or readiness-publication failure closes the acquired fake socket. A ready lease
is claimed exactly once; every terminal path (expiry, cancellation, failed
observer, or normal finish) uses the same identity-checked cleanup: remove only
the matching registry entry and active core, cancel a non-self watchdog, stop
the reader/socket, and close crypto once. A failed final v3 health attestation
never becomes a close success: the actual `BridgeEventObserver` records
`capture_failed` when terminal publication is possible, otherwise the caller
gets the truthful unknown/failure path. The final command is the retained
socket's token-bound v3 `health` request, and its reader alone routes the exact
request-id response to the attestation future.

### Round-6 RED/GREEN evidence

| Behavior | RED | GREEN |
| --- | --- | --- |
| Ready publication after a connected transport; cleanup watchdog ownership | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_ready_publication_failure_closes_the_connected_real_observer_transport tests/shared/test_whatsapp_rotation_smoke.py::test_cleanup_cancels_watchdog_and_closes_crypto_once` -> `2 failed`: acquired transport close count was `0`; watchdog was pending | Same command -> `2 passed in 0.25s` after observer ownership begins before the async connect/publication boundary and shared cleanup cancels the watchdog. |
| Expiry race/refusal cleanup | `uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py::test_expired_public_lease_uses_identity_safe_cleanup_before_refusal` -> `1 failed`: `lease.cleaned` was false | Same command -> `1 passed in 0.29s` after expiry delegates to the shared identity-safe cleanup path. |

The remaining required clauses were added as direct synthetic coverage against
the existing corrected implementation: `test_real_observer_final_health_rejects_incomplete_capture_without_success` covers dropped-baseline growth, malformed envelope, payload false, wrong account, and wrong protocol version with the real observer and in-memory websocket; `test_real_observer_final_health_timeout_burns_capture` covers no response; and `test_final_health_rejects_wrong_request_account_or_version` covers the strict final response binding. `test_real_observer_reader_close_burns_capture_without_a_successful_close` uses a ConnectionClosed-like fake. `test_cancelled_public_start_closes_acquired_transport_and_never_registers_it`, `test_cancelled_public_run_cleans_claimed_lease_without_a_second_fake_send`, `test_concurrent_public_starts_publish_one_ready_lease`, and `test_concurrent_public_run_claims_send_at_most_once` cover the public lifecycle; `test_clear_before_scan_keeps_boundary_reply_and_real_comparator_closes_it` injects an actual retained reply at the clear/scan boundary and verifies the real comparator closes `inbound_reply_observed`.

```text
uv run pytest -q tests/shared/test_whatsapp_rotation_smoke.py
74 passed in 3.56s
```

All new runs use only disposable pytest evidence roots, fake clocks, and
in-memory fake websocket objects. No real Bridge, Gateway, service, QR, JID,
message, auth, key, evidence, archive, memory, socket, port, or network path
was invoked.

Final Round-6 full verification after commit `5207405`:

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
328 passed in 16.55s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)

git status --short
(no output; clean linked worktree)
```

## Round-5 final recovery follow-up

### Requirement-by-requirement coverage

| Named acceptance case | Exact focused test(s) |
| --- | --- |
| Reservation, intent, and durable-attempt crash reruns; no resend | `test_crash_after_each_durable_pre_network_boundary_burns_nonce` |
| Network-before-response and response-before-acceptance reruns; no resend | `test_ambiguous_response_is_terminal_and_never_retried`; `test_acceptance_publication_failure_burns_attempt_as_unknown_without_retry` |
| Raw, normalized, inbound, terminal, and close publication boundaries retain evidence and block success | `test_raw_publication_failure_burns_capture_and_keeps_prior_event`; `test_normalized_publication_failure_preserves_raw_evidence_and_burns_capture`; `test_inbound_publication_failure_is_terminal_and_never_resends`; `test_terminal_publication_failure_still_blocks_close_and_preserves_capture`; `test_close_publication_failure_becomes_capture_failed_terminal` |
| Corrupt/partial/substituted journal and stored close revalidation | `test_corrupt_existing_nonce_is_burned_as_unknown_without_a_second_send`; `test_existing_successful_nonce_with_substituted_expectation_is_unknown_without_send`; `test_existing_close_is_reverified_as_a_complete_dag_before_replay` |
| Existing nonce returns only validated prior result | `test_successful_nonce_is_burned_and_returns_its_durable_close_without_resend` |
| Concurrent nonce and lock release before fake network/observer wait | `test_concurrent_same_nonce_reaches_fake_network_once_and_observer_wait_is_unlocked` |
| Protocol/request-id/destination/message-id rejection and one attempt | `test_direct_acceptance_requires_matching_protocol_v3_destination_and_message_id`; `test_malformed_authenticated_response_is_one_attempt_unknown` |
| Held credential modes/types/ownership, identity drift, normalization and unsupported domain | `test_smoke_expectation_binds_rotation_auth_inventory_and_identity` and its matrix in `tests/shared/test_whatsapp_rotation_state.py`; `test_public_smoke_build_api_has_only_the_fixed_production_inputs` |
| Direct raw-v3 provenance, observer readiness, disconnect/drop/overflow and raw retention | `test_observer_derives_provenance_only_from_authenticated_raw_v3_frame`; `test_observer_refuses_unready_or_unauthenticated_protocol_before_capture`; `test_observer_drop_disconnect_and_overflow_are_capture_failed`; `test_observer_retains_raw_event_and_normalized_provenance` |
| Complete DAG order, heads/count/contiguity/timestamps/inbound binding/no forward predecessor | `test_controller_zero_event_close_passes_real_state_comparator`; `test_controller_unsolicited_event_close_passes_real_state_comparator`; `test_controller_causal_reply_close_passes_real_state_comparator`; `test_observer_growth_requires_typed_protected_external_graph` matrix |
| No clear JID/content/auth stdout/stderr leak; no public bypass | `test_synthetic_smoke_never_leaks_raw_jid_content_or_auth_to_output`; public API signature tests |

### Round-5 RED/GREEN evidence

| Behavior group | RED evidence | GREEN evidence |
| --- | --- | --- |
| Corrupt nonce, incomplete close, and inbound receipt-publication recovery | `uv run pytest -q ...::test_corrupt_existing_nonce_is_burned_as_unknown_without_a_second_send ...::test_existing_close_is_reverified_as_a_complete_dag_before_replay ...::test_inbound_publication_failure_is_terminal_and_never_resends` -> `3 failed`: corrupt journal raised; incomplete close replayed success; inbound fault escaped | Same command -> `3 passed in 0.33s` after the minimal journal sentinel, DAG verification, and protected capture-failure result changes |
| Existing corrupt-journal contract migration | Full smoke run initially -> prior test expected `SmokeError`, but the required terminal outcome is `external_effect_unknown` | Updated contract plus full smoke suite -> `38 passed` (recorded below) |
| Remaining crash, concurrency, malformed-response, leakage, and publication matrix | Existing implementations were audited before the tests were added; their new synthetic tests passed without production changes | `8 passed in 0.71s` for crash/concurrency/response/leakage, then `2 passed in 0.36s` for normalized/terminal publication failure |

### Final Round-5 implementation and verification

- `27148dc fix(incident): harden smoke recovery replay` is the atomic follow-up commit; no prior commit was amended, reset, squashed, or dropped.
- `scripts/whatsapp_rotation_smoke.py`: corrupt journal reads are treated as an irreversibly burned unknown; successful stored closes now pass the complete state DAG verifier before replay; an observer receipt-publication fault records a protected capture-failure terminal against the durable attempt where possible.
- Removed the unused `_SmokeRuntime.reservations` field. No other scaffolding was unused.
- The four earlier mutation checks remain restored exactly as recorded in Round 4; this follow-up did not alter their covered branches, so they were not rerun.
- No real Bridge, Gateway, service, QR, JID, message, owner turn, auth, key/evidence, archive, memory, socket/port, or network path was touched. All runs used source code, disposable pytest directories, and fakes only.

```text
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
292 passed in 14.70s

uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
All checks passed!

git diff --check
(no output)
```
