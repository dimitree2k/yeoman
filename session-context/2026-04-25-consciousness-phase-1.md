# Consciousness Phase 1

Date: 2026-04-25

Short description: Owner-DM-only helpful cron speakups behind the global kill switch.

## Implemented

- Added `ConsciousnessService` with a local-time cron loop and manual `tick_once()`.
- Added `SpeakupLog` SQLite storage for proposals, sent rows, silent passes, and daily sent counts.
- Added `ConsciousnessTools` hard rails for owner-DM eligibility, global kill switch, daily cap, quiet hours, action allowlist, length cap, confidence threshold, and output security.
- Added `ConsciousnessAgent` that returns one proposal or records silence using a planner callable.
- Wired `ConsciousnessService` in `build_gateway_runtime()` only when `config.consciousness.enabled` is true.

## Verification

- `uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_contacts_policy.py tests/gateway/test_event_types.py tests/gateway/test_event_bus.py tests/shared/test_config_ipc_webhooks.py tests/gateway/test_workflow_state.py`
- `uv run ruff check packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/app/bootstrap.py tests/gateway/test_consciousness_phase1.py`
- `uv run python -m py_compile packages/gateway/yeoman_gateway/app/bootstrap.py packages/gateway/yeoman_gateway/consciousness/agent.py packages/gateway/yeoman_gateway/consciousness/log.py packages/gateway/yeoman_gateway/consciousness/service.py packages/gateway/yeoman_gateway/consciousness/tools.py`

## Not Done

- Phase 1 live exit criteria remain unchecked: one week of owner-DM runs and owner usefulness feedback.
- The planner is currently a JSON-response wrapper over `responder.process_direct()`, not a model-tool-calling loop.
