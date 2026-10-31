"""Quiet-moment trigger for dead groups: no message for hours, no lull before it.

The lull observer only fires after a burst of chat goes quiet. A group that stays
silent all day never has one, so this watcher offers it the same lull trigger once
it has been silent for ``idle_ms``. Posting hours, the daily cap and the gap between
offers are checked by ``TrendSeeds.seed``; the judge still decides.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from loguru import logger


class TrendIdleWatcher:
    def __init__(
        self,
        *,
        chats: Callable[[], Sequence[tuple[str, str, Any]]],
        last_activity_ms: Callable[[str, str], Awaitable[int | None]],
        may_post: Callable[[str, str], bool],
        trigger: Callable[[str, str], object],
        idle_ms: int,
        interval_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._chats = chats
        self._last_activity_ms = last_activity_ms
        self._may_post = may_post
        self._trigger = trigger
        self._idle_ms = int(idle_ms)
        self._interval = float(interval_seconds)
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def tick(self) -> list[str]:
        """Trigger every enabled chat that has been silent long enough; return their ids."""
        now_ms = int(self._clock() * 1000)
        fired: list[str] = []
        for channel, chat_id, _policy in list(self._chats()):
            if not self._may_post(channel, chat_id):
                continue
            last = await self._last_activity_ms(channel, chat_id)
            if last is None or now_ms - int(last) < self._idle_ms:
                continue
            raw = self._trigger(channel, chat_id)
            if inspect.isawaitable(raw):
                raw = await raw
            if raw is not None:  # None: no fresh cards, nothing was offered
                fired.append(chat_id)
        return fired

    async def _run(self) -> None:
        while True:
            try:
                fired = await self.tick()
                if fired:
                    logger.info("trend_idle_triggered chats={}", len(fired))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watcher outlives one bad tick
                logger.warning("trend idle tick failed error_type={}", type(exc).__name__)
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
