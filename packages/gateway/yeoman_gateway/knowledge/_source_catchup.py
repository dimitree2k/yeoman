"""Retry observed-source proof using the live path's selected history lease."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from yeoman_gateway.history.context import history_knowledge_scope, history_turn
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge._capture import ObservedSourceRegistrar
from yeoman_gateway.knowledge._history_identity import current_history_scope
from yeoman_gateway.knowledge.models import KnowledgeError


@dataclass
class SourceCatchupReport:
    examined: int = 0
    registered: int = 0
    already_proven: int = 0
    still_unknown: Counter[str] = field(default_factory=Counter)


class ObservedSourceCatchup:
    def __init__(
        self, *, processing: Any, knowledge: Any, registrar: ObservedSourceRegistrar,
        projector: Any, lookback_days: int | None = 7, interval_seconds: float = 600,
        batch_size: int = 500, clock: Callable[[], int] | None = None,
    ) -> None:
        if lookback_days is not None and lookback_days < 0:
            raise ValueError("lookback_days must be nonnegative or None")
        if batch_size < 1 or interval_seconds <= 0:
            raise ValueError("batch_size and interval_seconds must be positive")
        self.processing, self.knowledge = processing, knowledge
        self.registrar, self.projector = registrar, projector
        self.lookback_days, self.interval_seconds = lookback_days, interval_seconds
        self.batch_size = batch_size
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> SourceCatchupReport:
        report = SourceCatchupReport()
        since_ms = (None if self.lookback_days is None
                    else self.clock() - self.lookback_days * 86_400_000)
        after = None
        try:
            while True:
                rows = self.processing.unproven_event_sources(
                    since_ms=since_ms, after=after, inbound_only=True, limit=self.batch_size,
                )
                if not rows:
                    break
                for row in rows:
                    report.examined += 1
                    if row["revoked_at_ms"] is not None:
                        report.still_unknown["revoked"] += 1
                        continue
                    try:
                        async with history_turn(self.projector) as snapshot:
                            with history_knowledge_scope(snapshot, self.knowledge):
                                # Revocation can arrive while read_turn awaits its lease.
                                authority = self.processing.get_event_source_authority(
                                    row["event_id"], row["revision"],
                                )
                                if authority is not None:
                                    if authority["revoked_at_ms"] is not None:
                                        report.still_unknown["revoked"] += 1
                                        continue
                                    if authority["audience_status"] != "unknown":
                                        report.already_proven += 1
                                        continue
                                event = self.processing.get_event(row["event_id"])
                                scope = current_history_scope(self.knowledge._store)
                                if event.channel == "whatsapp":
                                    assert scope is not None
                                    message = scope.queries.native_message(
                                        chat_id=event.chat_id, native_id=event.source_message_id,
                                    )
                                    if message is None:
                                        report.still_unknown["missing_history_message"] += 1
                                        continue
                                    previous = scope.sources.ledger.current(message["message_id"])
                                    if previous is not None:
                                        if previous.revoked:
                                            report.still_unknown["revoked"] += 1
                                            continue
                                        if (previous.audience.status in ("known", "author_only")
                                                and previous.content_fingerprint == scope.queries.content_fingerprint(message["message_id"])):
                                            report.already_proven += 1
                                            continue
                                if self.registrar.register(row["event_id"]):
                                    report.registered += 1
                                else:
                                    report.still_unknown["audience_unproven"] += 1
                    except HistoryPaused:
                        report.still_unknown["history_paused"] += 1
                        return report
                    except KnowledgeError:
                        report.still_unknown["source_proof_unavailable"] += 1
                    except Exception:
                        report.still_unknown["registration_failed"] += 1
                last = rows[-1]
                after = (last["created_ms"], last["event_id"])
                await asyncio.sleep(0)
        finally:
            logger.info(
                "observed source catch-up examined={} registered={} already_proven={} still_unknown={}",
                report.examined, report.registered, report.already_proven,
                dict(sorted(report.still_unknown.items())),
            )
        return report

    async def _run(self) -> None:
        while True:
            if self.projector.health()["status"] == "starting":
                await asyncio.sleep(1)
                continue
            try:
                await self.run_once()
            except Exception:
                logger.warning("observed source catch-up pass failed")
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
