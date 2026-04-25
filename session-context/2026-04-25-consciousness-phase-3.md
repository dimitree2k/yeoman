# 2026-04-25 - Consciousness Phase 3

## Scope

Implemented the first Phase 3 slice for post-speakup learning:

- `OutcomeEnricher` classifies sent speakups after a delay by reading the post-speakup inbound archive window.
- `TasteDistiller` writes compact chat-scope taste memory only after enough classified samples exist.
- `SpeakupLog` can query pending outcome rows, mark outcome labels, and fetch classified samples.
- Added model routes for `consciousness.agent`, `consciousness.outcome`, and `consciousness.taste`.

## Important Constraint

This phase adds the tested primitives and provider routes, but it does not yet wire a scheduled runtime loop into the gateway. No autonomous outcome or taste distillation job runs until `build_gateway_runtime()` or an equivalent service loop schedules it.

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
