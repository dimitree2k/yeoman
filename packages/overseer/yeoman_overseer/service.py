"""OverseerService — main loop, lifecycle, boot cleanup."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from yeoman_overseer.audit.git import InternalGit
from yeoman_overseer.audit.logger import AuditLogger, AuditEntry
from yeoman_overseer.maintenance import MaintenanceManager
from yeoman_overseer.runbook.parser import Runbook, parse_runbook_dir
from yeoman_overseer.safety.causal import CausalChainDetector
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker
from yeoman_overseer.safety.rate_limiter import RateLimiter
from yeoman_overseer.socket.server import OverseerSocket
from yeoman_overseer.state import OverseerState
from yeoman_overseer.trigger.checks import CheckResult
from yeoman_overseer.trigger.evaluator import TriggerEvaluator
from yeoman_overseer.trigger.lock import LockManager

logger = logging.getLogger(__name__)


@dataclass
class OverseerConfig:
    tick_interval_s: float = 1.0
    actions_per_hour: int = 30
    llm_calls_per_day: int = 20
    failure_threshold: int = 3
    max_quarantines: int = 3


@dataclass
class OverseerService:
    data_dir: Path
    socket_path: Path
    config: OverseerConfig = field(default_factory=OverseerConfig)

    runbooks: list[Runbook] = field(default_factory=list)
    _state: OverseerState = field(default_factory=OverseerState)
    _git: InternalGit | None = None
    _audit: AuditLogger | None = None
    _evaluator: TriggerEvaluator | None = None
    _socket: OverseerSocket | None = None
    _running: bool = False
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def init(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        runbook_dir = self.data_dir / "runbooks"
        runbook_dir.mkdir(exist_ok=True)

        # Copy starter runbooks if directory is empty
        if not any(runbook_dir.glob("*.md")):
            starter_dir = Path(__file__).parent / "starter_runbooks"
            if starter_dir.is_dir():
                for src in starter_dir.glob("*.md"):
                    shutil.copy2(src, runbook_dir / src.name)
                logger.info("Copied %d starter runbooks", len(list(starter_dir.glob("*.md"))))

        self._git = InternalGit(self.data_dir)
        self._git.init()

        self._audit = AuditLogger(self.data_dir / "audit")
        self._state = OverseerState.load(self.data_dir / "state.json")
        self.runbooks = parse_runbook_dir(runbook_dir)
        logger.info("Loaded %d runbooks", len(self.runbooks))

        lock_manager = LockManager()
        circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.failure_threshold,
            max_quarantines=self.config.max_quarantines,
        )
        if self._state.circuit_breakers:
            circuit_breaker.import_state(self._state.circuit_breakers)

        rate_limiter = RateLimiter(
            actions_per_hour=self.config.actions_per_hour,
            llm_calls_per_day=self.config.llm_calls_per_day,
        )
        causal_detector = CausalChainDetector()
        maintenance = MaintenanceManager()
        if self._state.maintenance:
            maintenance.import_state(self._state.maintenance)

        self._evaluator = TriggerEvaluator(
            runbooks=self.runbooks,
            on_triggered=self._on_runbook_triggered,
            lock_manager=lock_manager,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            causal_detector=causal_detector,
            maintenance=maintenance,
            state=self._state,
        )

        self._socket = OverseerSocket(self.socket_path, stats_callback=self._get_stats)

    async def run(self) -> None:
        self._running = True
        self._stop_event.clear()

        if self._socket:
            await self._socket.start()

        logger.info("Overseer service started")
        try:
            while self._running:
                try:
                    if self._evaluator:
                        await self._evaluator.tick()
                except Exception:
                    logger.exception("Error during evaluator tick")

                self._state.heartbeat_ts = datetime.now(timezone.utc).isoformat()
                self._state.save(self.data_dir / "state.json")

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.config.tick_interval_s,
                    )
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def stop(self) -> None:
        self._running = False
        if self._socket:
            await self._socket.stop()
        if self._evaluator:
            self._state.circuit_breakers = self._evaluator.circuit_breaker.export_state()
            self._state.maintenance = self._evaluator.maintenance.export_state()
        self._state.save(self.data_dir / "state.json")
        logger.info("Overseer service stopped")

    async def _on_runbook_triggered(self, runbook: Runbook, check_result: CheckResult) -> None:
        start = time.monotonic()
        logger.info("Runbook triggered: %s", runbook.meta.name)
        duration_ms = int((time.monotonic() - start) * 1000)
        if self._audit:
            self._audit.append(AuditEntry(
                runbook=runbook.meta.name,
                trigger=runbook.meta.trigger.kind,
                action="triggered",
                target=runbook.meta.trigger.condition.target if runbook.meta.trigger.condition else "",
                result="success",
                duration_ms=duration_ms,
                escalated_to_llm=False,
                domain=runbook.meta.domain,
            ))

    def _get_stats(self) -> dict:
        return {
            "runbooks_loaded": len(self.runbooks),
            "heartbeat": self._state.heartbeat_ts,
            "budget": self._state.budget,
        }
