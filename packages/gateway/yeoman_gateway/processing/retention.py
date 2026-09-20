"""Plan 01 / R06, R10: apply journal retention on a schedule.

``ProcessingStore.purge`` implements the retention contract, but nothing ever
called it: ``processing.retention.*`` was a dead switch and payloads, decisions
and lineage metadata grew without bound. This service owns the missing
schedule.

The sweep runs in a worker thread, so a large delete never blocks the message
path, and it starts after a short delay instead of during startup. A failing
sweep is logged and retried on the next interval; it never ends the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger

from yeoman_gateway.processing.models import PurgeReport

# Retention windows are measured in days, so an hourly sweep is plenty and keeps
# the delete work far away from the hot path. Mirrors the inbound archive sweep.
SWEEP_INTERVAL_SECONDS = 3600.0
# Let startup finish (store migration, channels) before the first sweep.
STARTUP_DELAY_SECONDS = 30.0


class ProcessingRetentionService:
    """Applies ``ProcessingStore.purge`` on a slow, self-contained schedule."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], int] | None = None,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        startup_delay_seconds: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._store = store
        self._clock = clock or _now_ms
        self._interval_seconds = max(0.05, float(interval_seconds))
        self._startup_delay_seconds = max(0.0, float(startup_delay_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_failure: BaseException | None = None
        self._sweep_lock = asyncio.Lock()
        self._pending_zero = asyncio.Event()
        self._pending_zero.set()
        self._pending_sweeps = 0
        self._inflight_done: threading.Event | None = None
        self._stopping = False
        self._sweeps = 0
        self._failures = 0

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._stop_task is not None:
            if not self._stop_task.done():
                raise RuntimeError("processing retention stop is in progress")
            if self._stop_failure is not None:
                raise RuntimeError("processing retention stop failed; recovery required") from (
                    self._stop_failure
                )
            self._stop_task = None
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stopping = True
            task = self._task
            self._task = None
            self._stop_failure = None
            self._stop_task = asyncio.create_task(self._drain_stop(task))
        await asyncio.shield(self._stop_task)
        if self._stop_failure is not None:
            raise self._stop_failure

    async def _drain_stop(self, task: asyncio.Task[None] | None) -> None:
        failure: BaseException | None = None
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    failure = exc
        finally:
            await self._pending_zero.wait()
            self._stop_failure = failure

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stats(self) -> Mapping[str, int]:
        return {"sweeps": self._sweeps, "failures": self._failures}

    # -- work --------------------------------------------------------------------------

    async def sweep_once(self) -> PurgeReport:
        """Run one retention pass off the event loop.

        A single admission gate keeps SQLite work off the message loop while ensuring
        that only one purge runs at a time. Cancellation never abandons an in-flight
        purge: the operation is awaited before the call exits.
        """
        if self._stopping:
            raise RuntimeError("processing retention service is stopping")
        self._pending_sweeps += 1
        self._pending_zero.clear()
        acquired = False
        try:
            await self._sweep_lock.acquire()
            acquired = True
            return await self._run_sweep()
        finally:
            if acquired:
                self._sweep_lock.release()
            self._pending_sweeps -= 1
            if self._pending_sweeps == 0:
                self._pending_zero.set()

    async def _run_sweep(self) -> PurgeReport:
        done = threading.Event()
        result: list[PurgeReport] = []
        error: list[BaseException] = []

        def _run_purge() -> None:
            try:
                report = self._store.purge(now_ms=self._clock())
            except BaseException as exc:
                error.append(exc)
            else:
                result.append(report)
            finally:
                done.set()

        self._inflight_done = done
        threading.Thread(
            target=_run_purge,
            name="yeoman-processing-retention",
            daemon=True,
        ).start()
        cancelled = False
        try:
            while not done.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled = True
            while not done.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
        finally:
            if self._inflight_done is done:
                self._inflight_done = None
        if cancelled:
            raise asyncio.CancelledError
        if error:
            raise error[0]
        assert result
        report = result[0]
        self._sweeps += 1
        _log_report(report)
        return report

    async def _run_loop(self) -> None:
        if self._startup_delay_seconds:
            await asyncio.sleep(self._startup_delay_seconds)
        while not self._stopping:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad sweep must not end the loop
                self._failures += 1
                logger.warning(
                    "processing retention sweep failed error_type={}", type(exc).__name__
                )
            await asyncio.sleep(self._interval_seconds)


def _log_report(report: PurgeReport) -> None:
    counts = {
        "payloads": report.event_payloads_purged + report.effect_payloads_purged,
        "events": report.events_deleted,
        "relations": report.relations_deleted,
        "decisions": report.decisions_deleted,
        "attempts": report.attempts_deleted,
        "evidence": report.evidence_deleted,
        "probes": report.probes_deleted,
        "receipts": report.receipts_deleted,
    }
    if not any(counts.values()):
        logger.debug("processing retention sweep idle")
        return
    logger.info("processing retention sweep {}", " ".join(f"{k}={v}" for k, v in counts.items()))


def _now_ms() -> int:
    return int(time.time() * 1000)
