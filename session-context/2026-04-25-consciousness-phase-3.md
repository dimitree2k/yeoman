# 2026-04-25 - Consciousness Phase 3

## Scope

Implemented the first Phase 3 slice for post-speakup learning:

- `OutcomeEnricher` classifies sent speakups after a delay by reading the post-speakup inbound archive window.
- `TasteDistiller` writes compact chat-scope taste memory only after enough classified samples exist.
- `SpeakupLog` can query pending outcome rows, mark outcome labels, and fetch classified samples.
- Added model routes for `consciousness.agent`, `consciousness.outcome`, and `consciousness.taste`.

## Important Constraint

The second Phase 3 slice wires outcome and taste jobs into `ConsciousnessService.tick_once()`. When consciousness is enabled, `build_gateway_runtime()` now constructs:

- `OutcomeEnricher` using the explicit `consciousness.outcome` model route.
- `TasteDistiller` using the explicit `consciousness.taste` model route.
- The planner using the explicit `consciousness.agent` model route.

The service sequence is planner tick, outcome classification, then taste distillation for chats that have classified outcome samples. Outcome/taste jobs do not publish outbound messages.

## 2026-04-26 Runtime Wiring Update

Changed files:

- `packages/gateway/yeoman_gateway/app/bootstrap.py`
- `packages/gateway/yeoman_gateway/consciousness/service.py`
- `packages/gateway/yeoman_gateway/consciousness/log.py`
- `tests/gateway/test_consciousness_phase3.py`

Also fixed date-sensitive Phase 1/2 tests that assumed April 25 while runtime code used the current date.

## Verification

Targeted checks:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase3.py tests/shared/test_config_ipc_webhooks.py::test_config_has_consciousness_model_routes -q
```

Result: `4 passed`.

Regression checks:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_inbound_archive_range.py tests/gateway/test_event_bus.py tests/gateway/test_workflow_state.py tests/shared/test_config_ipc_webhooks.py -q
```

Result: `49 passed`.

Lint:

```bash
uv run ruff check packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/storage/inbound_archive.py packages/shared/yeoman_shared/config/defaults.py tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/shared/test_config_ipc_webhooks.py
```

Result: `All checks passed`.

Runtime wiring checks on 2026-04-26:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_inbound_archive_range.py tests/gateway/test_event_bus.py tests/gateway/test_workflow_state.py tests/shared/test_config_ipc_webhooks.py -q
```

Result: `50 passed`.

```bash
uv run ruff check packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/app/bootstrap.py tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py
```

Result: `All checks passed`.
