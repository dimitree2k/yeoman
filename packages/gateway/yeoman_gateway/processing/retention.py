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
        self._stopping = False
        self._sweeps = 0
        self._failures = 0

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stats(self) -> Mapping[str, int]:
        return {"sweeps": self._sweeps, "failures": self._failures}

    # -- work --------------------------------------------------------------------------

    async def sweep_once(self) -> PurgeReport:
        """Run one retention pass off the event loop."""
        report = await asyncio.to_thread(self._store.purge, now_ms=self._clock())
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
