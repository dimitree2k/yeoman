"""Periodic sweep over the decisions table: post due decisions once, drop expired rows.

A due decision is sent as plain text to its own chat through the service effect path.
Nothing here runs an agent turn, so a group-written text never reaches a tool.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from loguru import logger

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.group_decisions.store import Decision, GroupDecisionStore

#: Shown in front of a due decision, so the chat sees it is not a new reply.
NUDGE_PREFIX = "⏰ "
MAX_NUDGE_ATTEMPTS = 5
NUDGE_INTERVAL_S = 60.0
RETENTION_INTERVAL_S = 3600.0


class DecisionSweeper:
    """Runs the nudge pass every minute and the retention pass every hour."""

    def __init__(
        self,
        store: GroupDecisionStore,
        *,
        service_effects: Any | None,
        bus: Any,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        interval_s: float = NUDGE_INTERVAL_S,
    ) -> None:
        self._store = store
        self._service_effects = service_effects
        self._bus = bus
        self._clock_ms = clock_ms
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._last_retention_ms = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="group-decision-sweeper")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._store.close()

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("group decision sweep failed: {}", type(exc).__name__)
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> None:
        now_ms = self._clock_ms()
        for decision in self._store.due_for_nudge(now_ms):
            await self._nudge(decision, now_ms)
        if now_ms - self._last_retention_ms >= RETENTION_INTERVAL_S * 1000:
            self._last_retention_ms = now_ms
            removed = self._store.purge_expired(now_ms)
            if removed:
                logger.info("group decisions: removed {} expired rows", removed)

    async def _nudge(self, decision: Decision, now_ms: int) -> None:
        # Re-read the row: it may have been closed since the due query ran.
        current = self._store.get(decision.chat_id, decision.id)
        if current is None or current.status != "open" or current.nudged_at_ms is not None:
            return
        try:
            delivered = await self._send(current)
        except Exception as exc:
            logger.warning("group decision {} nudge failed: {}", current.id,
                           type(exc).__name__)
            delivered = False
        if delivered:
            self._store.mark_nudged(current.chat_id, current.id, now_ms)
            return
        attempts = self._store.record_nudge_failure(current.chat_id, current.id)
        if attempts >= MAX_NUDGE_ATTEMPTS:
            # Give up rather than retry forever; the decision stays open in 'list'.
            self._store.mark_nudged(current.chat_id, current.id, now_ms)
            logger.warning("group decision {} nudge abandoned after {} attempts",
                           current.id, attempts)

    async def _send(self, decision: Decision) -> bool:
        content = NUDGE_PREFIX + decision.text
        if self._service_effects is None:
            await self._bus.publish_outbound(
                OutboundMessage(channel="whatsapp", chat_id=decision.chat_id, content=content)
            )
            return True
        receipt = await self._service_effects.send(
            source="cron",
            # Stable per decision and due time, so a retry after a crash is the same effect.
            operation_ref=f"decision:{decision.id}:{decision.due_at_ms}",
            channel="whatsapp",
            chat_id=decision.chat_id,
            content=content,
        )
        # None means the chat is on the legacy queue path, which gives no receipt.
        state = getattr(receipt, "state", "sent") if receipt is not None else "sent"
        return bool(state == "sent")
