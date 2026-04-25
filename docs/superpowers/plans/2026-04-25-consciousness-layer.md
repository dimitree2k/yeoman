# Consciousness Layer Implementation Plan

> For agentic workers: implement this plan phase-by-phase. Keep the checkbox
> state updated as tasks land so progress survives session changes.

Status: in_progress
Spec: `../specs/2026-04-25-consciousness-layer-design.md`
Superseded draft: `../specs/2026-04-25-consciousness-layer-design-superseded.md`

## Phase 0 - Integration Fixes

Goal: add the codebase primitives needed by later phases without enabling any
proactive bot behavior.

- [x] Policy schema: add `SpontaneityPolicy` and `SpontaneityPolicyOverride` in `packages/gateway/yeoman_gateway/policy/schema.py`.
- [x] Policy schema: add `spontaneity` to `ChatPolicy` and `ChatPolicyOverride`.
- [x] Policy engine: expose resolved spontaneity policy for a `(channel, chat_id)` target.
- [x] Global config: add `ConsciousnessConfig` in `packages/shared/yeoman_shared/config/schema.py`.
- [x] Global config: add `Config.consciousness`.
- [x] Bus events: add `InboundObservedEvent` in `packages/gateway/yeoman_gateway/bus/events.py`.
- [x] Bus publish: emit `InboundObservedEvent` from `MessageBus.publish_inbound()` without blocking normal inbound delivery.
- [x] Approval primitives: add `PendingSpeakupApproval` and `SpeakupApprovalStore`.
- [x] Tests: policy schema accepts and rejects expected spontaneity fields.
- [x] Tests: `MessageBus.publish_inbound()` emits observation events and still enqueues inbound messages.
- [x] Tests: event queue overflow cannot block inbound delivery.
- [x] Tests: `SpeakupApprovalStore` persists, reloads, expires, approves, and denies proposals.

Exit criteria:

- [x] Phase 0 tests pass.
- [x] No service task starts by default.
- [x] No proactive message can be sent yet.

## Phase 1 - Owner DM Helpful Cron

Goal: prove one low-blast-radius proactive path with owner DMs only.

- [x] Add `consciousness/service.py` with cron-loop orchestration.
- [x] Add `consciousness/log.py` with SQLite speakup log and daily counters.
- [x] Add `consciousness/tools.py` with the hard tool boundary.
- [x] Add `consciousness/agent.py` with one-proposal-or-silence behavior.
- [x] Wire `ConsciousnessService` in `build_gateway_runtime()` behind `config.consciousness.enabled`.
- [x] Enforce owner-DM-only eligibility.
- [x] Enforce global kill switch, daily cap, quiet hours, action allowlist, length cap, and confidence threshold.
- [x] Publish `OutboundMessage(metadata={"spontaneous": true, ...})` on successful commit.
- [x] Tests: global kill switch prevents service start and commit.
- [x] Tests: daily cap cannot be exceeded.
- [x] Tests: quiet hours prevent commits.
- [x] Tests: non-eligible chat ids are rejected by tools.
- [x] Tests: fake agent can propose exactly one outbound message.
- [x] Tests: fake agent can stay silent and silent pass is logged.
- [x] Tests: security classifier rejection prevents commit.

Exit criteria:

- [ ] One week of owner-DM runs has no cap violations.
- [ ] Owner reports messages are useful often enough to continue.

## Phase 2 - Opt-In Groups With Preview

Goal: add explicit group opt-in with owner-DM preview before group delivery.

- [x] Add `pipeline/speakup_approval.py`.
- [x] Wire `SpeakupApprovalMiddleware` into the pipeline after policy resolution.
- [x] Queue group proposals in `SpeakupApprovalStore` when preview is `owner_dm`.
- [x] Send owner preview messages with `spk-approve-*` and `spk-deny-*` codes.
- [x] On approve, publish to the target chat.
- [x] On deny, expire, or timeout, do not send and do not consume daily cap.
- [x] Tests: group opt-in is required.
- [x] Tests: group preview queues owner approval instead of sending directly.
- [x] Tests: approve code sends to target chat.
- [x] Tests: deny code prevents send.
- [x] Tests: expired approval does not send and does not consume daily cap.

Exit criteria:

- [x] Preview flow works end-to-end for at least one opt-in group.
- [x] No group message is sent without explicit `preview: "none"` or owner approval.

## Phase 3 - Outcome And Taste

Goal: add self-improvement from logged behavior after enough data exists.

- [x] Add `consciousness/outcomes.py` delayed outcome classifier.
- [x] Add provider route `consciousness.outcome`.
- [x] Update `SpeakupLog` records with outcome labels.
- [x] Add `consciousness/taste.py` distiller.
- [x] Add provider route `consciousness.taste`.
- [x] Write compact chat-scope taste memory only after enough samples.
- [x] Tests: outcome enricher classifies scripted post-speakup windows.
- [x] Tests: taste distiller writes patterns, not raw speakup messages.

Implementation note:

- Phase 3 outcome/taste primitives and model routes are implemented and tested.
- Runtime scheduling/wiring is intentionally not enabled yet; no autonomous outcome/taste loop runs until wired into the gateway runtime.

Exit criteria:

- [ ] Distilled chat taste records improve proposal quality without polluting memory retrieval.

## Phase 4 - Burst Trigger

Goal: allow opportunistic wakeups from observed chat activity without bypassing rails.

- [ ] Add `consciousness/burst.py`.
- [ ] Subscribe `BurstObserver` to `InboundObservedEvent`.
- [ ] Maintain rolling counts per `(channel, chat_id)`.
- [ ] Persist burst debounce state across restarts.
- [ ] Enforce at-most-one burst between daily cron firings per chat.
- [ ] Tests: burst trigger fires only when threshold/window are met.
- [ ] Tests: burst trigger never bypasses eligibility, daily cap, preview, quiet hours, or profile rails.
- [ ] Tests: debounce state survives restart.

Exit criteria:

- [ ] Burst fires only within configured limits over two weeks of normal traffic.

## Always-On Safety Checklist

- [x] `consciousness.enabled = false` disables all proactive behavior.
- [x] No proactive group message without explicit group opt-in.
- [x] Daily cap is enforced atomically at commit time.
- [x] Tool boundary refuses non-eligible chat ids.
- [x] Logs and transcripts stay under private runtime paths, not source.
- [ ] Property test passes: adversarial agent sequences cannot exceed daily cap.
