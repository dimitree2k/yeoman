"""Plan 01 / R06, R10: the journal retention schedule actually runs."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from yeoman_gateway.app.bootstrap import build_retention_service
from yeoman_gateway.processing.models import CANONICAL_WHATSAPP_ORIGIN, DAY_MS, PurgeReport
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


class _SlowStore:
    def __init__(self, delay_seconds: float = 0.2) -> None:
        self.delay_seconds = delay_seconds
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def purge(self, *, now_ms: int) -> PurgeReport:
        del now_ms
        self.calls += 1
        self.started.set()
        time.sleep(self.delay_seconds)
        self.finished.set()
        return PurgeReport()


class _SerializedSlowStore:
    """Blocks each purge until the test releases that specific call."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = [threading.Event(), threading.Event()]
        self.release = [threading.Event(), threading.Event()]
        self.finished = [threading.Event(), threading.Event()]
        self._lock = threading.Lock()

    def purge(self, *, now_ms: int) -> PurgeReport:
        del now_ms
        with self._lock:
            index = self.calls
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started[index].set()
        self.release[index].wait(timeout=2)
        with self._lock:
            self.active -= 1
        self.finished[index].set()
        return PurgeReport()


class _CancellableErrorStore:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def purge(self, *, now_ms: int) -> PurgeReport:
        del now_ms
        self.started.set()
        self.release.wait(timeout=2)
        self.finished.set()
        raise RuntimeError("purge failed")


class _CloseAwareStore:
    def __init__(self) -> None:
        self.calls = 0
        self.after_close = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def purge(self, *, now_ms: int) -> PurgeReport:
        del now_ms
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        if self.closed:
            self.after_close += 1
        return PurgeReport()

    def close(self) -> None:
        self.closed = True


async def _wait_for_thread_event(event: threading.Event) -> None:
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for worker event")


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


def test_sweep_exempts_canonical_whatsapp_events_but_not_operational_events(tmp_path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    store.append_event(
        event_key="wa:old",
        event_id="wa-old",
        trace_id="trace-wa",
        payload={
            "kind": "message",
            "origin": CANONICAL_WHATSAPP_ORIGIN,
            "channel": "whatsapp",
            "text": "canonical",
        },
        now_ms=NOW,
    )
    store.append_event(
        event_key="ops:old",
        event_id="ops-old",
        trace_id="trace-ops",
        payload={"kind": "message", "text": "operational"},
        now_ms=NOW,
    )
    service = ProcessingRetentionService(store, clock=_Clock(NOW + 31 * DAY_MS))

    asyncio.run(service.sweep_once())

    canonical = store.get_event("wa-old")
    assert canonical is not None and canonical.payload is not None
    assert store.get_event("ops-old") is None
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


def test_sweep_runs_off_event_loop() -> None:
    store = _SlowStore()
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        ticks: list[float] = []

        async def ticker() -> None:
            while not store.finished.is_set():
                await asyncio.sleep(0.02)
                ticks.append(asyncio.get_running_loop().time())

        tick_task = asyncio.create_task(ticker())
        await service.sweep_once()
        await tick_task

        assert store.calls == 1
        assert ticks, "a slow purge must not block the event loop"
        await service.stop()

    asyncio.run(scenario())


def test_stop_waits_for_an_inflight_sweep_before_shutdown() -> None:
    store = _SlowStore(delay_seconds=0.05)
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        sweep = asyncio.create_task(service.sweep_once())
        while not store.started.is_set():
            await asyncio.sleep(0.005)
        await service.stop()
        assert store.finished.is_set()
        await sweep
        assert service.running is False

    asyncio.run(scenario())


def test_concurrent_sweeps_are_serialized_and_stop_waits_for_both() -> None:
    store = _SerializedSlowStore()
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        first = asyncio.create_task(service.sweep_once())
        await _wait_for_thread_event(store.started[0])
        second = asyncio.create_task(service.sweep_once())
        await asyncio.sleep(0)
        stop = asyncio.create_task(service.stop())
        await asyncio.sleep(0.02)

        assert not stop.done()
        assert store.calls == 1
        assert store.max_active == 1

        store.release[0].set()
        await _wait_for_thread_event(store.started[1])
        assert not stop.done()
        assert store.max_active == 1

        store.release[1].set()
        await asyncio.gather(first, second, stop)
        assert store.calls == 2
        assert store.max_active == 1
        assert service.running is False

    asyncio.run(scenario())


def test_stop_rejects_new_sweeps_without_starting_a_worker() -> None:
    store = _SlowStore(delay_seconds=0.01)
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        await service.stop()
        with pytest.raises(RuntimeError, match="stopping"):
            await service.sweep_once()
        assert store.calls == 0

    asyncio.run(scenario())


def test_cancelled_sweep_waits_for_worker_and_cleans_up() -> None:
    store = _SlowStore(delay_seconds=0.05)
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        sweep = asyncio.create_task(service.sweep_once())
        await _wait_for_thread_event(store.started)
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        assert store.finished.is_set()
        await service.stop()
        assert store.calls == 1

    asyncio.run(scenario())


def test_worker_error_propagates_and_stop_is_clean() -> None:
    store = _CancellableErrorStore()
    service = ProcessingRetentionService(store, clock=_Clock(), startup_delay_seconds=0.0)

    async def scenario() -> None:
        sweep = asyncio.create_task(service.sweep_once())
        await _wait_for_thread_event(store.started)
        store.release.set()
        with pytest.raises(RuntimeError, match="purge failed"):
            await sweep
        assert store.finished.is_set()
        await service.stop()

    asyncio.run(scenario())


def test_concurrent_start_is_rejected_until_stop_fence_and_restart_is_safe() -> None:
    store = _CloseAwareStore()
    service = ProcessingRetentionService(
        store,
        clock=_Clock(),
        startup_delay_seconds=0.05,
    )

    async def scenario() -> None:
        sweep = asyncio.create_task(service.sweep_once())
        await _wait_for_thread_event(store.started)
        stop = asyncio.create_task(service.stop())
        for _ in range(200):
            if service._stopping:
                break
            await asyncio.sleep(0)
        assert service._stopping is True

        try:
            with pytest.raises(RuntimeError, match="stop is in progress"):
                await service.start()
        finally:
            store.release.set()
            await asyncio.gather(sweep, stop)

        await service.start()
        assert service.running is True
        await service.stop()
        store.close()
        await asyncio.sleep(0.06)
        assert store.after_close == 0

    asyncio.run(scenario())


def test_repeated_start_stop_leaves_no_retention_tasks_or_pending_sweeps() -> None:
    service = ProcessingRetentionService(
        _SlowStore(delay_seconds=0.01),
        clock=_Clock(),
        interval_seconds=0.05,
        startup_delay_seconds=0.01,
    )

    async def scenario() -> None:
        for _ in range(30):
            await service.start()
            await service.start()
            await service.stop()
            assert service.running is False
            assert service._task is None
            assert service._pending_sweeps == 0
            assert service._inflight_done is None

        retention_tasks = [
            task
            for task in asyncio.all_tasks()
            if "ProcessingRetentionService._run_loop" in repr(task.get_coro())
        ]
        assert retention_tasks == []

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
