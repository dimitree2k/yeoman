# 2026-04-26 - Consciousness Safety Property

## Scope

Completed the remaining always-on safety checklist item after Phase 4.

Added `tests/gateway/test_consciousness_safety.py`, a deterministic property-style regression that runs generated adversarial tool-call scripts against the real `ConsciousnessTools` commit boundary.

The scripts include:

- Valid proposals.
- Invalid-chat proposals.
- Low-confidence proposals.
- Repeated commits of the same proposal.
- Fake proposal commits.
- Agent-driven runs.
- Concurrent commits of multiple valid proposals.

The property is checked for daily caps `0`, `1`, and `2`: after every operation, committed sent rows and outbound messages for the target chat must remain less than or equal to the configured daily cap.

## Verification

Targeted property check:

```bash
uv run python -m pytest tests/gateway/test_consciousness_safety.py -q
```

Result: `3 passed`.

Regression checks:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_consciousness_phase4.py tests/gateway/test_consciousness_safety.py tests/gateway/test_inbound_archive_range.py tests/gateway/test_event_bus.py tests/gateway/test_workflow_state.py tests/shared/test_config_ipc_webhooks.py -q
```

Result: `59 passed`.

Lint:

```bash
uv run ruff check tests/gateway/test_consciousness_safety.py tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_consciousness_phase4.py packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/app/bootstrap.py tests/shared/test_config_ipc_webhooks.py
```

Result: `All checks passed`.
