# Consciousness Phase 2

Date: 2026-04-25

Short description: Opt-in group speakups now queue owner-DM preview approvals before delivery.

## Implemented

- Added `SpeakupApprovalMiddleware` to consume `spk-approve-*` and `spk-deny-*` owner replies in the inbound pipeline after policy resolution.
- Wired one shared `SpeakupLog` and one shared `SpeakupApprovalStore` through `build_gateway_runtime()` so cron proposals and inbound approvals use the same state.
- Extended `ConsciousnessTools` eligibility from owner DMs to explicitly opted-in groups with `balanced` and `permissive` profiles.
- Defaulted group preview mode to `owner_dm`, queued pending approvals instead of direct group sends, and sent preview messages to the owner DM with explicit approve and deny codes.
- Enforced final-send rails on approval: owner-DM matching, output security, and daily-cap recheck at approval time.
- Added Phase 2 regression coverage in `tests/gateway/test_consciousness_phase2.py`.

## Runtime State

- `~/.yeoman/config.json` currently has `consciousness.enabled = true`.
- `~/.yeoman/config.json` currently keeps `consciousness.ownerDmDefaultEnabled = false`.
- `~/.yeoman/policy.json` currently enables spontaneity only for the explicit owner WhatsApp DM `491757070305@s.whatsapp.net`.
- No group chat has been opted in yet, so the new Phase 2 path is present in code but idle until a group receives explicit spontaneity policy.

## Verification

- `uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_contacts_policy.py tests/gateway/test_event_types.py tests/gateway/test_event_bus.py tests/shared/test_config_ipc_webhooks.py tests/gateway/test_workflow_state.py`
- `uv run ruff check packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/pipeline/speakup_approval.py packages/gateway/yeoman_gateway/core/orchestrator.py packages/gateway/yeoman_gateway/pipeline/__init__.py packages/gateway/yeoman_gateway/app/bootstrap.py tests/gateway/test_consciousness_phase2.py`

## Not Done

- Phase 2 live exit criteria remain unchecked because no group has been opted in and exercised end-to-end yet.
- Phase 3 outcome and taste loops are still not started.
