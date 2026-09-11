"""Plan 01 / R06, R10: the journal retention schedule actually runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from yeoman_gateway.app.bootstrap import build_retention_service
from yeoman_gateway.processing.models import DAY_MS, PurgeReport
from yeoman_gateway.processing.retention import ProcessingRetentionService
from yeoman_gateway.processing.store import ProcessingStore

NOW = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = NOW) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _FailingStore:
    """Fails the first sweep, then succeeds; records every call."""

    def __init__(self) -> None:
        self.calls = 0

    def purge(self, *, now_ms: int) -> PurgeReport:
        del now_ms
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database is locked")
        return PurgeReport()


def _append_event(store: ProcessingStore) -> None:
    store.append_event(
        event_key="wa:old",
        event_id="e1",
        trace_id="tr1",
        payload={"kind": "message", "text": "secret"},
        now_ms=NOW,
    )


def test_sweep_once_applies_the_configured_retention(tmp_path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    _append_event(store)
    service = ProcessingRetentionService(store, clock=_Clock(NOW + 8 * DAY_MS))

    report = asyncio.run(service.sweep_once())

    stored = store.get_event("e1")
    assert stored is not None
    assert stored.payload is None, "the payload must be stripped at its retention window"
    assert report.event_payloads_purged == 1
    assert service.stats()["sweeps"] == 1
    store.close()


def test_sweep_keeps_metadata_until_its_own_window(tmp_path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    _append_event(store)
    service = ProcessingRetentionService(store, clock=_Clock(NOW + 8 * DAY_MS))

    asyncio.run(service.sweep_once())

    assert store.get_event("e1") is not None, "metadata outlives the payload window"
    store.close()


def test_the_loop_survives_a_failing_sweep() -> None:
    store = _FailingStore()
    service = ProcessingRetentionService(
        store,
        clock=_Clock(),
        interval_seconds=0.05,
        startup_delay_seconds=0.0,
    )

    async def scenario() -> None:
        await service.start()
        for _ in range(200):
            if store.calls >= 3:
                break
            await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(scenario())

    assert store.calls >= 3, "the sweep must repeat after a failure"
    assert service.stats()["failures"] == 1
    assert service.running is False


def test_start_is_idempotent_and_stop_without_start_is_safe() -> None:
    service = ProcessingRetentionService(
        _FailingStore(),
        clock=_Clock(),
        interval_seconds=0.05,
        startup_delay_seconds=0.0,
    )

    async def scenario() -> None:
        await service.stop()
        assert service.running is False
        await service.start()
        first = service._task
        await service.start()
        assert service._task is first, "a second start must not spawn a second loop"
        await service.stop()
        assert service.running is False

    asyncio.run(scenario())


def test_builder_stays_inert_without_processing(tmp_path) -> None:
    enabled = SimpleNamespace(processing=SimpleNamespace(enabled=True))
    disabled = SimpleNamespace(processing=SimpleNamespace(enabled=False))

    assert build_retention_service(enabled, None) is None

    store = ProcessingStore(tmp_path / "processing.db")
    try:
        assert build_retention_service(disabled, store) is None
        assert isinstance(build_retention_service(enabled, store), ProcessingRetentionService)
    finally:
        store.close()
