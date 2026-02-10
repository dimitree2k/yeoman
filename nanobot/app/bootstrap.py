"""Application bootstrap and runtime wiring for the vNext orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, assert_never

from loguru import logger

from nanobot.adapters.policy_engine import EnginePolicyAdapter
from nanobot.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
from nanobot.adapters.responder_llm import LLMResponder
from nanobot.adapters.telemetry import InMemoryTelemetry
from nanobot.adapters.typing_channel_manager import ChannelManagerTypingAdapter
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.core.intents import (
    OrchestratorIntent,
    PersistSessionIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SetTypingIntent,
)
from nanobot.core.models import InboundEvent
from nanobot.core.orchestrator import Orchestrator
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.heartbeat.service import HeartbeatService
from nanobot.media.router import ModelRouter
from nanobot.media.storage import MediaStorage
from nanobot.memory import MemoryService
from nanobot.providers.factory import ProviderFactory
from nanobot.security import NoopSecurity, SecurityEngine
from nanobot.session.manager import SessionManager
from nanobot.storage.inbound_archive import InboundArchive

if TYPE_CHECKING:
    from pathlib import Path

    from nanobot.config.schema import Config, ExecToolConfig
    from nanobot.policy.engine import PolicyEngine
    from nanobot.providers.base import LLMProvider


def _normalize_timestamp(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _resolve_security_tool_settings(config: "Config") -> tuple[bool, "ExecToolConfig"]:
    """Apply strict-profile hardening overrides for tool runtime settings."""
    restrict_to_workspace = bool(config.tools.restrict_to_workspace)
    exec_config = config.tools.exec.model_copy(deep=True)
    if config.security.strict_profile:
        restrict_to_workspace = True
        exec_config.isolation.enabled = True
        exec_config.isolation.fail_closed = True
    return restrict_to_workspace, exec_config


def _inbound_message_to_event(msg: InboundMessage) -> InboundEvent:
    meta = msg.metadata
    return InboundEvent(
        channel=msg.channel,
        chat_id=msg.chat_id,
        sender_id=msg.sender_id,
        content=msg.content,
        message_id=str(meta.get("message_id") or "").strip() or None,
        timestamp=_normalize_timestamp(msg.timestamp),
        participant=str(meta.get("participant") or "").strip() or None,
        is_group=bool(meta.get("is_group", False)),
        mentioned_bot=bool(meta.get("mentioned_bot", False)),
        reply_to_bot=bool(meta.get("reply_to_bot", False)),
        reply_to_message_id=str(meta.get("reply_to_message_id") or "").strip() or None,
        reply_to_participant=str(meta.get("reply_to_participant") or "").strip() or None,
        reply_to_text=str(meta.get("reply_to_text") or "").strip() or None,
        media=tuple(msg.media),
        raw_metadata=dict(meta),
    )


class OrchestratorService:
    """Consumes inbound messages and executes typed orchestrator intents."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        orchestrator: Orchestrator,
        typing_adapter: ChannelManagerTypingAdapter,
        telemetry: InMemoryTelemetry,
    ) -> None:
        self._bus = bus
        self._orchestrator = orchestrator
        self._typing_adapter = typing_adapter
        self._telemetry = telemetry
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            event = _inbound_message_to_event(msg)
            try:
                intents = await self._orchestrator.handle(event)
                await self._dispatch_intents(intents)
            except Exception as e:
                logger.error("vnext orchestrator failure channel={} chat={}: {}", event.channel, event.chat_id, e)
                await self._bus.publish_outbound(
                    OutboundMessage(
                        channel=event.channel,
                        chat_id=event.chat_id,
                        content=f"Sorry, I encountered an error: {e}",
                    )
                )

    def stop(self) -> None:
        self._running = False

    async def _dispatch_intents(self, intents: list[OrchestratorIntent]) -> None:
        for intent in intents:
            match intent:
                case SetTypingIntent():
                    await self._typing_adapter(intent.channel, intent.chat_id, intent.enabled)
                case SendOutboundIntent():
                    await self._bus.publish_outbound(
                        OutboundMessage(
                            channel=intent.event.channel,
                            chat_id=intent.event.chat_id,
                            content=intent.event.content,
                            reply_to=intent.event.reply_to,
                            media=list(intent.event.media),
                        )
                    )
                case PersistSessionIntent():
                    # Sessions are persisted by the responder implementation.
                    continue
                case RecordMetricIntent():
                    self._telemetry.incr(intent.name, intent.value, intent.labels)
                case _:
                    assert_never(intent)


@dataclass(slots=True)
class GatewayRuntime:
    """Lifecycle holder for the composed gateway runtime."""

    orchestrator: OrchestratorService
    channels: ChannelManager
    cron: CronService
    heartbeat: HeartbeatService
    inbound_archive: InboundArchive
    responder: LLMResponder
    memory: MemoryService

    async def run(self) -> None:
        try:
            await self.cron.start()
            await self.heartbeat.start()
            await asyncio.gather(
                self.orchestrator.run(),
                self.channels.start_all(),
            )
        finally:
            self.heartbeat.stop()
            self.cron.stop()
            self.orchestrator.stop()
            await self.channels.stop_all()
            await self.responder.aclose()
            self.inbound_archive.close()
            self.memory.close()


def build_gateway_runtime(
    *,
    config: "Config",
    provider: "LLMProvider",
    policy_engine: "PolicyEngine | None",
    policy_path: "Path | None",
    workspace: "Path",
    bus: MessageBus,
) -> GatewayRuntime:
    """Compose full gateway runtime around vNext orchestrator."""
    from nanobot.config.loader import get_data_dir

    session_manager = SessionManager(workspace)
    inbound_archive = InboundArchive(
        db_path=get_data_dir() / "inbound" / "reply_context.db",
        retention_days=30,
    )
    inbound_archive.purge_older_than(days=30)
    model_router = ModelRouter(config.models)
    media_storage = MediaStorage(
        incoming_dir=config.channels.whatsapp.media.incoming_path,
        outgoing_dir=config.channels.whatsapp.media.outgoing_path,
    )
    provider_factory = ProviderFactory(config=config)

    assistant_model = config.agents.defaults.model
    try:
        assistant_profile = model_router.resolve("assistant.reply")
        if assistant_profile.model:
            assistant_model = assistant_profile.model
    except KeyError:
        pass

    telemetry = InMemoryTelemetry()
    restrict_to_workspace, exec_config = _resolve_security_tool_settings(config)
    security = SecurityEngine(config.security) if config.security.enabled else NoopSecurity()

    memory_service = MemoryService(
        workspace=workspace,
        config=config.memory,
    )
    try:
        imported = memory_service.backfill_from_workspace_files(force=False)
        if imported > 0:
            logger.info("memory backfill imported {} entries", imported)
    except Exception as e:
        logger.warning("memory backfill failed: {}", e)

    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    responder = LLMResponder(
        provider=provider,
        workspace=workspace,
        bus=bus,
        model=assistant_model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        session_manager=session_manager,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        cron_service=cron,
    )
    if policy_engine is not None:
        policy_engine.validate(set(responder.tool_names))
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=set(responder.tool_names),
        policy_path=policy_path,
    )

    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        inbound_archive=inbound_archive,
        model_router=model_router,
        media_storage=media_storage,
        provider_factory=provider_factory,
    )

    typing_adapter = ChannelManagerTypingAdapter(channels)
    archive_adapter = SqliteReplyArchiveAdapter(inbound_archive)
    orchestrator = Orchestrator(
        policy=policy_adapter,
        responder=responder,
        reply_archive=archive_adapter,
        reply_context_window_limit=config.channels.whatsapp.reply_context_window_limit,
        reply_context_line_max_chars=config.channels.whatsapp.reply_context_line_max_chars,
        typing_notifier=typing_adapter,
        security=security,
        security_block_message=config.security.block_user_message,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        response = await responder.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )
        return response

    cron.on_job = on_cron_job

    async def on_heartbeat(prompt: str) -> str:
        return await responder.process_direct(
            prompt,
            session_key="heartbeat",
            channel="heartbeat",
            chat_id="direct",
        )

    heartbeat = HeartbeatService(
        workspace=workspace,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,
        enabled=True,
    )

    orchestrator_service = OrchestratorService(
        bus=bus,
        orchestrator=orchestrator,
        typing_adapter=typing_adapter,
        telemetry=telemetry,
    )

    return GatewayRuntime(
        orchestrator=orchestrator_service,
        channels=channels,
        cron=cron,
        heartbeat=heartbeat,
        inbound_archive=inbound_archive,
        responder=responder,
        memory=memory_service,
    )
