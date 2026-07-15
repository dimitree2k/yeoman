"""OverseerService — main loop, lifecycle, boot cleanup."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sdnotify

from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.agent.loop import AgentLoop, BudgetExhaustedError
from yeoman_overseer.agent.tools import ToolContext
from yeoman_overseer.audit.git import InternalGit
from yeoman_overseer.audit.logger import AuditEntry, AuditLogger
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.deterministic import (
    DeterministicExecutor,
    parse_deterministic_actions,
)
from yeoman_overseer.maintenance import MaintenanceManager
from yeoman_overseer.runbook.parser import Runbook, parse_runbook, parse_runbook_dir
from yeoman_overseer.safety.causal import CausalChainDetector
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker
from yeoman_overseer.safety.rate_limiter import RateLimiter
from yeoman_overseer.socket.server import OverseerSocket
from yeoman_overseer.state import OverseerState
from yeoman_overseer.trigger.checks import CheckResult
from yeoman_overseer.trigger.evaluator import TriggerEvaluator
from yeoman_overseer.trigger.lock import LockManager

logger = logging.getLogger(__name__)


def _sync_starter_runbooks(
    starter_dir: Path,
    runbook_dir: Path,
    *,
    copy_missing: bool = False,
) -> int:
    """Copy missing starter runbooks and upgrade older generated copies."""
    if not starter_dir.is_dir():
        return 0

    synced = 0
    for src in starter_dir.glob("*.md"):
        dest = runbook_dir / src.name
        if not dest.exists():
            if copy_missing:
                shutil.copy2(src, dest)
                synced += 1
            continue

        try:
            source_runbook = parse_runbook(src)
            existing_runbook = parse_runbook(dest)
        except ValueError:
            continue

        if source_runbook.meta.version > existing_runbook.meta.version:
            shutil.copy2(src, dest)
            synced += 1
    return synced


@dataclass
class OverseerConfig:
    tick_interval_s: float = 1.0
    actions_per_hour: int = 30
    llm_calls_per_day: int = 80
    llm_tokens_per_day: int = 2_000_000
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
    _sd_notifier: sdnotify.SystemdNotifier = field(default_factory=sdnotify.SystemdNotifier)

    async def init(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        runbook_dir = self.data_dir / "runbooks"
        runbook_dir.mkdir(exist_ok=True)

        starter_dir = Path(__file__).parent / "starter_runbooks"
        synced = _sync_starter_runbooks(
            starter_dir,
            runbook_dir,
            copy_missing=not any(runbook_dir.glob("*.md")),
        )
        if synced:
            logger.info("Synced %d starter runbooks", synced)

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

        # Load .env, config, and policy
        # data_dir is ~/.yeoman/data/overseer — yeoman_home is two levels up
        import os as _os
        _yeoman_home_env = _os.environ.get("YEOMAN_HOME", "").strip()
        yeoman_home = Path(_yeoman_home_env) if _yeoman_home_env else Path.home() / ".yeoman"
        self._load_dotenv()
        import json as _json
        config_path = yeoman_home / "config.json"
        raw_config = _json.loads(config_path.read_text()) if config_path.exists() else {}
        policy_path = yeoman_home / "policy.json"
        raw_policy = _json.loads(policy_path.read_text()) if policy_path.exists() else {}

        # Build comms channels from config
        channels = self._build_comms_channels(raw_config, raw_policy)
        self._comms = CascadingComms(channels=channels, local_log=True)

        tool_ctx = ToolContext(
            yeoman_home=yeoman_home,
            source_dir=Path.home() / "Documents" / "yeoman",
            audit=self._audit,
            comms=self._comms,
            data_dir=self.data_dir,
            sandbox=self._create_sandbox(),
            memory_db=yeoman_home / "data" / "memory" / "memory.db",
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
        self._sd_notifier.notify("READY=1")
        last_hourly_reset = time.monotonic()
        last_daily_reset = time.monotonic()
        try:
            while self._running:
                now = time.monotonic()

                # Periodic rate-limiter resets
                if now - last_hourly_reset >= 3600:
                    last_hourly_reset = now
                    if self._evaluator:
                        self._evaluator.rate_limiter.reset_hourly()
                    self._state.reset_hourly_budget()
                if now - last_daily_reset >= 86400:
                    last_daily_reset = now
                    if self._evaluator:
                        self._evaluator.rate_limiter.reset_daily()
                    self._prune_action_log()

                try:
                    if self._evaluator:
                        await self._evaluator.tick()
                except Exception:
                    logger.exception("Error during evaluator tick")

                self._state.heartbeat_ts = datetime.now(timezone.utc).isoformat()
                self._state.save(self.data_dir / "state.json")
                self._sd_notifier.notify("WATCHDOG=1")

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
        self._sd_notifier.notify("STOPPING=1")
        if self._socket:
            await self._socket.stop()
        if self._evaluator:
            self._state.circuit_breakers = self._evaluator.circuit_breaker.export_state()
            self._state.maintenance = self._evaluator.maintenance.export_state()
        self._state.save(self.data_dir / "state.json")
        logger.info("Overseer service stopped")

    @staticmethod
    def _load_dotenv() -> None:
        """Load ~/.yeoman/.env into os.environ (same logic as gateway config loader)."""
        import os
        yeoman_home = os.environ.get("YEOMAN_HOME", "").strip()
        base = Path(yeoman_home) if yeoman_home else Path.home() / ".yeoman"
        env_file = base / ".env"
        if not env_file.is_file():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value

    @staticmethod
    def _build_comms_channels(
        config: dict, policy: dict
    ) -> list:
        """Build notification channels from gateway config + policy."""
        import os

        from yeoman_overseer.comms.cascading import CommsChannel

        channels: list[CommsChannel] = []

        # Telegram: resolve bot token from config or env, chat ID from policy owners
        tg_config = config.get("channels", {}).get("telegram", {})
        bot_token = (tg_config.get("token") or "").strip() or os.environ.get(
            "TELEGRAM_BOT_TOKEN", ""
        ).strip()
        owner_tg_ids = policy.get("owners", {}).get("telegram", [])

        if bot_token and owner_tg_ids:
            from yeoman_overseer.comms.telegram import TelegramDirectChannel
            channels.append(TelegramDirectChannel(
                bot_token=bot_token,
                chat_id=str(owner_tg_ids[0]),
            ))
            logger.info("Comms: Telegram channel configured (chat_id=%s)", owner_tg_ids[0])
        else:
            logger.warning(
                "Comms: Telegram not configured (token=%s, owners=%d)",
                "present" if bot_token else "missing",
                len(owner_tg_ids),
            )

        return channels

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
            except Exception as exc:
                logger.error("Runbook %s agent error: %s", runbook.meta.name, exc)
                result_str = f"error: {exc}"
        elif self._comms:
            actions = parse_deterministic_actions(runbook.body)
            if actions:
                executor = DeterministicExecutor(comms=self._comms)
                failed_results: list[str] = []
                for action in actions:
                    action_start = time.monotonic()
                    result = await executor.execute(
                        action.action,
                        target=action.target,
                        **action.kwargs,
                    )
                    if not result.success:
                        result_str = result.detail
                        failed_results.append(result.detail)
                    if self._audit:
                        self._audit.append(AuditEntry(
                            runbook=runbook.meta.name,
                            trigger=runbook.meta.trigger.kind,
                            action=action.action,
                            target=action.target,
                            result=result.detail,
                            duration_ms=int((time.monotonic() - action_start) * 1000),
                            escalated_to_llm=False,
                            domain=runbook.meta.domain,
                        ))
                if failed_results:
                    raise RuntimeError("; ".join(failed_results))
                return

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

    def _prune_action_log(self, keep: int = 500) -> None:
        """Trim the persisted action_log to the most recent *keep* entries."""
        if len(self._state.action_log) > keep:
            self._state.action_log = self._state.action_log[-keep:]

    def _get_stats(self) -> dict:
        return {
            "runbooks_loaded": len(self.runbooks),
            "heartbeat": self._state.heartbeat_ts,
            "budget": self._state.budget,
        }
