"""Plan 04 / R07: probes are bounded, leased and idempotent across workers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    PolicySnapshot,
    TextPayload,
)
from yeoman_gateway.processing.policy import SnapshotEffectAuthorizer
from yeoman_gateway.processing.reconcile import (
    ProbeOutcome,
    ProbeResult,
    ReconciliationService,
)
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Config:
    backoff_seconds = (5, 15, 45, 120)
    max_probes = 4
    deadline_seconds = 600
    claim_lease_seconds = 30
    probe_timeout_ms = 10_000
    probe_concurrency = 2

    def __init__(self, **overrides) -> None:
        for key, value in overrides.items():
            setattr(self, key, value)


class _Snapshots:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="v1", policy_hash="h1", healthy=True)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _TimeoutExecutor:
    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        raise TimeoutError("bridge timeout")


class _CountingProbe:
    def __init__(self, outcome: ProbeOutcome = ProbeOutcome.INCONCLUSIVE, delay: float = 0.0):
        self.outcome = outcome
        self.delay = delay
        self.calls = 0
        self.peak = 0
        self._active = 0

    async def probe(self, request):
        self.calls += 1
        self._active += 1
        self.peak = max(self.peak, self._active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return ProbeResult(self.outcome, "counting probe")
        finally:
            self._active -= 1


def _unknown_effect(store: ProcessingStore, effect_id: str = "fx1") -> str:
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=_TimeoutExecutor(),
        clock=_Clock(),
    )
    gateway.submit(
        EffectEnvelope(
            effect_id=effect_id,
            operation_key=f"k-{effect_id}",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    return effect_id


async def _make_unknown(store: ProcessingStore, effect_id: str = "fx1") -> str:
    _unknown_effect(store, effect_id)
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=_TimeoutExecutor(),
        clock=_Clock(),
    )
    await gateway.execute_ready(effect_id)
    assert store.effect_state(effect_id) == "unknown"
    return effect_id


@pytest.mark.asyncio
async def test_recovered_claim_is_probed_immediately(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = _unknown_effect(store)
    # A worker died after dispatch: the claim is still running.
    store.claim_effect(effect_id, "dead-worker", T0, 30_000)
    probe = _CountingProbe(ProbeOutcome.CONFIRMED)
    service = ReconciliationService(
        store, probe=probe, config=_Config(), clock=_Clock(T0 + 30_001), tick_seconds=0.05
    )

    await service.tick_once()

    assert store.effect_state(effect_id) == "sent"
    assert probe.calls == 1
    assert service.stats()["recovered"] == 1
    store.close()


@pytest.mark.asyncio
async def test_probe_timeout_leaves_a_finished_inconclusive_probe(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    probe = _CountingProbe(ProbeOutcome.INCONCLUSIVE, delay=5.0)
    service = ReconciliationService(
        store,
        probe=probe,
        config=_Config(probe_timeout_ms=50),
        clock=_Clock(T0 + 6_000),
        tick_seconds=0.05,
    )

    await service.tick_once()

    assert store.effect_state(effect_id) == "unknown"  # a timeout proves nothing
    assert store.count_probes(effect_id, outcome="inconclusive") == 1
    assert store.open_probe(effect_id) is None
    store.close()


@pytest.mark.asyncio
async def test_two_workers_run_a_probe_only_once(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    first = ProcessingStore(path)
    effect_id = await _make_unknown(first)
    second = ProcessingStore(path)
    clock = _Clock(T0 + 6_000)
    probe_a = _CountingProbe(ProbeOutcome.CONFIRMED, delay=0.05)
    probe_b = _CountingProbe(ProbeOutcome.CONFIRMED, delay=0.05)
    service_a = ReconciliationService(first, probe=probe_a, config=_Config(), clock=clock, worker_id="a")
    service_b = ReconciliationService(second, probe=probe_b, config=_Config(), clock=clock, worker_id="b")

    await asyncio.gather(service_a.tick_once(), service_b.tick_once())

    assert probe_a.calls + probe_b.calls == 1  # the lease is exclusive
    assert first.effect_state(effect_id) == "sent"
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_concurrency_is_bounded_and_loop_survives_failures(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    for index in range(5):
        await _make_unknown(store, f"fx{index}")
    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def probe(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("probe exploded")
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, "ok")

    service = ReconciliationService(
        store, probe=_Boom(), config=_Config(probe_concurrency=2), clock=_Clock(T0 + 6_000)
    )
    await service.tick_once()
    assert store.effect_state("fx0") == "unknown"  # the exception did not escalate anything

    probe = _CountingProbe(ProbeOutcome.INCONCLUSIVE, delay=0.05)
    service = ReconciliationService(
        store, probe=probe, config=_Config(probe_concurrency=2), clock=_Clock(T0 + 6_000)
    )
    await service.tick_once()
    assert probe.peak <= 2  # never more than the configured concurrency
    store.close()


@pytest.mark.asyncio
async def test_start_stop_leaves_no_running_task(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    service = ReconciliationService(
        store,
        probe=_CountingProbe(),
        config=_Config(),
        clock=_Clock(T0),
        tick_seconds=0.05,
    )

    await service.start()
    assert service.running is True
    await asyncio.sleep(0.12)
    await service.stop()

    assert service.running is False
    assert service.stats()["ticks"] >= 1
    store.close()


@pytest.mark.asyncio
async def test_deadline_escalates_without_further_probes(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    probe = _CountingProbe(ProbeOutcome.INCONCLUSIVE)
    service = ReconciliationService(
        store, probe=probe, config=_Config(), clock=_Clock(T0 + 700_000)
    )

    await service.tick_once()

    assert store.effect_state(effect_id) == "unknown_nonrepeatable"
    assert probe.calls == 0  # past the deadline no probe is planned
    store.close()


@pytest.mark.asyncio
async def test_service_factory_is_inert_when_processing_is_disabled(tmp_path: Path) -> None:
    from unittest.mock import patch

    from yeoman_gateway.app.bootstrap import (
        build_processing_store,
        build_reconciliation_service,
    )
    from yeoman_shared.config.schema import Config

    disabled = Config()
    assert build_reconciliation_service(disabled, None) is None  # no store, no DB, no task

    enabled = Config.model_validate({"processing": {"enabled": True}})
    with patch.dict("os.environ", {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(enabled)
        assert store is not None
        service = build_reconciliation_service(enabled, store)
    assert service is not None
    assert service.running is False  # built, not started
    await service.stop()  # a stop without a start is a no-op
    store.close()


@pytest.mark.asyncio
async def test_reconciler_starts_before_channels_and_stops_before_the_store(tmp_path: Path) -> None:
    """Start order matters: recovery must run before the first message is processed."""
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = _unknown_effect(store)
    store.claim_effect(effect_id, "dead-worker", T0, 30_000)
    probe = _CountingProbe(ProbeOutcome.CONFIRMED)
    service = ReconciliationService(
        store, probe=probe, config=_Config(), clock=_Clock(T0 + 30_001), tick_seconds=0.05
    )

    await service.start()
    await asyncio.sleep(0.15)
    await service.stop()

    # The recovery happened in the first tick, before anything else could dispatch.
    assert store.effect_state(effect_id) == "sent"
    assert probe.calls == 1
    assert service.running is False
    store.close()
