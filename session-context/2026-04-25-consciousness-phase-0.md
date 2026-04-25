# Consciousness Phase 0

Date: 2026-04-25

Short description: Phase 0 integration primitives for the consciousness layer.

## Implemented

- Added gateway policy-level `spontaneity` schema and resolved policy fields.
- Added shared global `ConsciousnessConfig` and `Config.consciousness`.
- Added `InboundObservedEvent` and best-effort emission from `MessageBus.publish_inbound()`.
- Added `PendingSpeakupApproval` and `SpeakupApprovalStore` under `yeoman_gateway.consciousness`.
- Updated `docs/superpowers/plans/2026-04-25-consciousness-layer.md` Phase 0 checkboxes.

## Verification

- `uv run python -m pytest tests/gateway/test_contacts_policy.py tests/gateway/test_event_types.py tests/gateway/test_event_bus.py tests/shared/test_config_ipc_webhooks.py tests/gateway/test_workflow_state.py`
- `uv run ruff check packages/gateway/yeoman_gateway/policy/schema.py packages/gateway/yeoman_gateway/policy/engine.py packages/shared/yeoman_shared/config/schema.py packages/gateway/yeoman_gateway/bus/events.py packages/gateway/yeoman_gateway/bus/queue.py packages/gateway/yeoman_gateway/consciousness tests/gateway/test_contacts_policy.py tests/gateway/test_event_types.py tests/gateway/test_event_bus.py tests/shared/test_config_ipc_webhooks.py tests/gateway/test_workflow_state.py`
- `npm run build` in `packages/bridge`
- `npm test` in `packages/bridge`

## Next Step

Start Phase 1 from the durable plan. Do not add proactive send behavior outside
the service gate: `Config.consciousness.enabled` must remain the hard kill
switch.
