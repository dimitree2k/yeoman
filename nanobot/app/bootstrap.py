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
from nanobot.bus.events import InboundMessage, OutboundMessage, ReactionMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.core.intents import (
    OrchestratorIntent,
    PersistSessionIntent,
    QueueMemoryNotesCaptureIntent,
    RecordManualMemoryIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SendReactionIntent,
    SetTypingIntent,
)
from nanobot.core.models import InboundEvent
from nanobot.core.orchestrator import Orchestrator
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.heartbeat.service import HeartbeatService
from nanobot.media.router import ModelRouter
from nanobot.media.storage import MediaStorage
from nanobot.media.tts import TTSSynthesizer
from nanobot.memory import MemoryService
from nanobot.providers.factory import ProviderFactory
from nanobot.providers.openai_compatible import resolve_openai_compatible_credentials
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
        exec_config.allow_host_execution = False
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
        memory: MemoryService,
    ) -> None:
        self._bus = bus
        self._orchestrator = orchestrator
        self._typing_adapter = typing_adapter
        self._telemetry = telemetry
        self._memory = memory
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
                logger.error(
                    "vnext orchestrator failure channel={} chat={}: {}",
                    event.channel,
                    event.chat_id,
                    e,
                )
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
                            metadata=dict(intent.event.metadata or {}),
                        )
                    )
                case SendReactionIntent():
                    await self._bus.publish_reaction(
                        ReactionMessage(
                            channel=intent.channel,
                            chat_id=intent.chat_id,
                            message_id=intent.message_id,
                            emoji=intent.emoji,
                            participant_jid=intent.participant_jid,
                        )
                    )
                case PersistSessionIntent():
                    # Sessions are persisted by the responder implementation.
                    continue
                case QueueMemoryNotesCaptureIntent():
                    self._memory.enqueue_background_note(
                        channel=intent.channel,
                        chat_id=intent.chat_id,
                        sender_id=intent.sender_id,
                        message_id=intent.message_id,
                        content=intent.content,
                        is_group=intent.is_group,
                        mode=intent.mode,
                        batch_interval_seconds=intent.batch_interval_seconds,
                        batch_max_messages=intent.batch_max_messages,
                    )
                case RecordManualMemoryIntent():
                    mapped_kind = "decision" if intent.entry_kind == "backlog" else "episodic"
                    salience = 0.9 if intent.entry_kind == "backlog" else 0.8
                    self._memory.record_manual(
                        channel=intent.channel,
                        chat_id=intent.chat_id,
                        sender_id=intent.sender_id,
                        scope_type="chat",
                        kind=mapped_kind,
                        text=intent.content,
                        importance=salience,
                        confidence=1.0,
                    )
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

    memory_service = MemoryService(workspace=workspace, config=config.memory, root_config=config)
    memory_state_dir = config.memory.wal.state_dir
    try:
        imported = memory_service.backfill_from_workspace_files(force=False)
        if imported > 0:
            logger.info("memory backfill imported {} entries", imported)
    except Exception as e:
        logger.warning("memory backfill failed: {}", e)

    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create policy adapter first so we can use it for owner_alert_resolver
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=set(),  # Will be updated after responder is created
        policy_path=policy_path,
        session_manager=session_manager,
        workspace=workspace,
        memory_state_dir=memory_state_dir,
    )

    responder = LLMResponder(
        provider=provider,
        workspace=workspace,
        bus=bus,
        model=assistant_model,
        subagent_model=config.agents.defaults.subagent_model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        session_manager=session_manager,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        cron_service=cron,
        owner_alert_resolver=policy_adapter.owner_recipients,
    )
    if policy_engine is not None:
        policy_engine.validate(set(responder.tool_names))

    # Update policy adapter with actual tool names
    policy_adapter._known_tools = set(responder.tool_names)
    admin_command_handler = getattr(policy_adapter, "route_admin_command", None)
    if admin_command_handler is None:
        admin_command_handler = getattr(policy_adapter, "maybe_handle_admin_command", None)

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
    openai_compat = resolve_openai_compatible_credentials(config)
    elevenlabs = config.providers.elevenlabs
    tts = TTSSynthesizer(
        openai_api_key=openai_compat.api_key if openai_compat else None,
        openai_api_base=openai_compat.api_base if openai_compat else None,
        openai_extra_headers=openai_compat.extra_headers if openai_compat else None,
        elevenlabs_api_key=elevenlabs.api_key or None,
        elevenlabs_api_base=elevenlabs.api_base,
        elevenlabs_extra_headers=elevenlabs.extra_headers,
        elevenlabs_default_voice_id=elevenlabs.voice_id,
        elevenlabs_default_model_id=elevenlabs.model_id,
        max_concurrency=config.channels.whatsapp.media.max_tts_concurrency,
    )
    orchestrator = Orchestrator(
        policy=policy_adapter,
        responder=responder,
        reply_archive=archive_adapter,
        reply_context_window_limit=config.channels.whatsapp.reply_context_window_limit,
        reply_context_line_max_chars=config.channels.whatsapp.reply_context_line_max_chars,
        typing_notifier=typing_adapter,
        security=security,
        security_block_message=config.security.block_user_message,
        policy_admin_handler=admin_command_handler,
        model_router=model_router,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        owner_alert_resolver=policy_adapter.owner_recipients,
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
        memory=memory_service,
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
