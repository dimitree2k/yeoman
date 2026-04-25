"""Owner-DM-only cron orchestration for Phase 1 consciousness."""

from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger
from yeoman_shared.config.schema import Config

from yeoman_gateway.consciousness.agent import ConsciousnessAgent


class ConsciousnessService:
    """Run the Phase 1 consciousness agent at the configured local time."""

    def __init__(self, *, config: Config, agent: ConsciousnessAgent) -> None:
        self._config = config
        self._agent = agent
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_run_day: str | None = None

    @property
    def started(self) -> bool:
        return self._running

    async def start(self) -> None:
        if not self._config.consciousness.enabled:
            logger.info("Consciousness service disabled")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Consciousness service started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def tick_once(self) -> dict[str, object]:
        if not self._config.consciousness.enabled:
            return {"status": "disabled"}
        return await self._agent.run_once(trigger="cron")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                now = datetime.now().astimezone()
                day = now.date().isoformat()
                if (
                    self._last_run_day != day
                    and now.hour == self._config.consciousness.cron_hour
                    and now.minute == self._config.consciousness.cron_minute
                ):
                    self._last_run_day = day
                    await self.tick_once()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Consciousness tick failed: {}", exc)
                await asyncio.sleep(30)

