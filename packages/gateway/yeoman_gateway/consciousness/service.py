"""Owner-DM-only cron orchestration for Phase 1 consciousness."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from yeoman_shared.config.schema import Config

from yeoman_gateway.consciousness.agent import ConsciousnessAgent

if TYPE_CHECKING:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.outcomes import OutcomeEnricher
    from yeoman_gateway.consciousness.taste import TasteDistiller


class ConsciousnessService:
    """Run proactive consciousness jobs at the configured local time."""

    def __init__(
        self,
        *,
        config: Config,
        agent: ConsciousnessAgent,
        outcome_enricher: "OutcomeEnricher | None" = None,
        taste_distiller: "TasteDistiller | None" = None,
        speakup_log: "SpeakupLog | None" = None,
    ) -> None:
        self._config = config
        self._agent = agent
        self._outcome_enricher = outcome_enricher
        self._taste_distiller = taste_distiller
        self._speakup_log = speakup_log
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

    async def tick_once(
        self,
        *,
        trigger: str = "cron",
        target_channel: str | None = None,
        target_chat_id: str | None = None,
    ) -> dict[str, object]:
        if not self._config.consciousness.enabled:
            return {"status": "disabled"}
        result = dict(
            await self._agent.run_once(
                trigger=trigger,
                target_channel=target_channel,
                target_chat_id=target_chat_id,
            )
        )
        if self._outcome_enricher is not None:
            result["outcomes"] = await self._outcome_enricher.run_once()
        if self._taste_distiller is not None and self._speakup_log is not None:
            result["taste"] = await self._run_taste_distillation()
        return result

    async def _run_taste_distillation(self) -> list[dict[str, Any]]:
        assert self._speakup_log is not None
        assert self._taste_distiller is not None
        results: list[dict[str, Any]] = []
        for target in await self._speakup_log.outcome_sample_chats():
            channel = str(target.get("channel") or "")
            chat_id = str(target.get("chat_id") or "")
            if not channel or not chat_id:
                continue
            distilled = await self._taste_distiller.run_once(channel=channel, chat_id=chat_id)
            results.append({"channel": channel, "chat_id": chat_id, **distilled})
        return results

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
