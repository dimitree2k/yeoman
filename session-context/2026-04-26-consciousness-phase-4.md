# 2026-04-26 - Consciousness Phase 4

## Scope

Implemented burst-triggered consciousness wakeups from `InboundObservedEvent`.

- Added `BurstObserver` in `packages/gateway/yeoman_gateway/consciousness/burst.py`.
- Wired `BurstObserver` in `build_gateway_runtime()` when consciousness is enabled.
- Subscribed the observer to `InboundObservedEvent`.
- Maintains rolling per-chat message windows.
- Persists same-day burst debounce state in private runtime data at `~/.yeoman/data/consciousness/burst_state.json`.
- Added targeted burst ticks with `ConsciousnessService.tick_once(trigger="burst", target_channel=..., target_chat_id=...)`.
- Added targeted planner filtering in `ConsciousnessAgent` so burst wakeups cannot choose a different eligible chat.

## Safety

Burst is disabled by default through `consciousness.burstEnabled = false`.

When enabled, the observer checks `ConsciousnessTools.is_chat_eligible()` before waking the service. The actual proposal path still uses the same hard tool boundary as cron/manual ticks:

- Global consciousness enable switch.
- Explicit group opt-in.
- Daily cap.
- Owner-DM preview for groups.
- Quiet hours.
- Action allowlist.
- Confidence threshold.
- Output security.

## Verification

Targeted Phase 4 checks:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase4.py -q
```

Result: `6 passed`.

Regression checks:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_consciousness_phase4.py tests/gateway/test_inbound_archive_range.py tests/gateway/test_event_bus.py tests/gateway/test_workflow_state.py tests/shared/test_config_ipc_webhooks.py -q
```

Result: `55 passed`.

Lint:

```bash
uv run ruff check packages/gateway/yeoman_gateway/consciousness packages/gateway/yeoman_gateway/app/bootstrap.py tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_consciousness_phase3.py tests/gateway/test_consciousness_phase4.py tests/shared/test_config_ipc_webhooks.py
```

Result: `All checks passed`.
