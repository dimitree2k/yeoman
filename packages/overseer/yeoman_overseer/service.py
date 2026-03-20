"""OverseerService — main loop, lifecycle, boot cleanup."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.agent.loop import AgentLoop, AgentResult, BudgetExhaustedError
from yeoman_overseer.agent.tools import ToolContext
from yeoman_overseer.audit.git import InternalGit
from yeoman_overseer.audit.logger import AuditLogger, AuditEntry
from yeoman_overseer.comms.cascading import CascadingComms
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
    llm_tokens_per_day: int = 500_000
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
    _comms: CascadingComms | None = None
    _agent_loop: AgentLoop | None = None
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

        # Comms — no channels yet; local_log=True ensures alerts are never silently lost
        self._comms = CascadingComms(channels=[], local_log=True)

        # Load config for model profiles
        import json as _json
        config_path = self.data_dir.parent / "config.json"
        raw_config = _json.loads(config_path.read_text()) if config_path.exists() else {}

        tool_ctx = ToolContext(
            yeoman_home=self.data_dir.parent,
            source_dir=Path.home() / "Documents" / "yeoman",
            audit=self._audit,
            comms=self._comms,
            data_dir=self.data_dir,
            sandbox=self._create_sandbox(),
            memory_db=self.data_dir / "memory" / "memory.db",
            git=self._git,
        )
        _budget = BudgetTracker(
            self._state,
            calls_per_day=self.config.llm_calls_per_day,
            tokens_per_day=self.config.llm_tokens_per_day,
        )
        self._agent_loop = AgentLoop(tool_ctx=tool_ctx, budget=_budget, config=raw_config)

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

    @staticmethod
    def _create_sandbox():
        """Try to create a Sandbox instance; return None if bwrap unavailable."""
        from yeoman_overseer.agent.sandbox import Sandbox, SandboxUnavailableError
        try:
            sandbox = Sandbox()
            sandbox._find_bwrap()
            return sandbox
        except SandboxUnavailableError:
            logger.warning("bwrap not found — sandbox-dependent tools will be unavailable")
            return None

    async def _on_runbook_triggered(self, runbook: Runbook, check_result: CheckResult) -> None:
        start = time.monotonic()
        logger.info("Runbook triggered: %s", runbook.meta.name)

        escalated = False
        result_str = "success"
        llm_tokens = llm_calls = reasoning = None
        llm_profile = None

        if runbook.meta.escalate_to_llm and self._agent_loop:
            # Set per-runbook context
            self._agent_loop._tool_ctx.runbook_name = runbook.meta.name
            self._agent_loop._tool_ctx.domain = runbook.meta.domain
            self._agent_loop._tool_ctx.shell_timeout_s = runbook.meta.safety.shell_timeout_s
            try:
                observations = {
                    "check": check_result.value,
                    "message": check_result.detail,
                }
                agent_result = await self._agent_loop.run(runbook, observations)
                escalated = True
                llm_tokens = agent_result.tokens_used
                llm_calls = agent_result.tool_calls_made
                reasoning = agent_result.summary[:500] if agent_result.summary else None
                llm_profile = agent_result.llm_profile
            except BudgetExhaustedError as exc:
                result_str = f"budget_exhausted: {exc}"

        duration_ms = int((time.monotonic() - start) * 1000)
        if self._audit:
            self._audit.append(AuditEntry(
                runbook=runbook.meta.name,
                trigger=runbook.meta.trigger.kind,
                action="triggered",
                target=runbook.meta.trigger.condition.target if runbook.meta.trigger.condition else "",
                result=result_str,
                duration_ms=duration_ms,
                escalated_to_llm=escalated,
                domain=runbook.meta.domain,
                llm_tokens_used=llm_tokens,
                llm_tool_calls=llm_calls,
                llm_profile=llm_profile,
                reasoning_summary=reasoning,
            ))

    def _get_stats(self) -> dict:
        return {
            "runbooks_loaded": len(self.runbooks),
            "heartbeat": self._state.heartbeat_ts,
            "budget": self._state.budget,
        }
