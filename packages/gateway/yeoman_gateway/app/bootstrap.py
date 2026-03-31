"""Application bootstrap and runtime wiring for the vNext orchestrator."""

from __future__ import annotations

import asyncio
import os
import random
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from loguru import logger
from yeoman_shared.telemetry import InMemoryTelemetry, tracing

from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.adapters.typing_channel_manager import ChannelManagerTypingAdapter
from yeoman_gateway.agent.tools.file_access import build_file_access_resolver
from yeoman_gateway.bus.events import InboundMessage, OutboundMessage, ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.manager import ChannelManager
from yeoman_gateway.contacts.service import ContactsService
from yeoman_gateway.core.intents import (
    OrchestratorIntent,
    PersistSessionIntent,
    QueueMemoryNotesCaptureIntent,
    RecordManualMemoryIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SendReactionIntent,
    SetTypingIntent,
)
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_gateway.cron.service import CronService
from yeoman_gateway.cron.types import CronJob
from yeoman_gateway.heartbeat.service import HeartbeatService
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.media.tts import TTSSynthesizer
from yeoman_gateway.memory import MemoryService
from yeoman_gateway.providers.factory import ProviderFactory
from yeoman_gateway.providers.openai_compatible import resolve_openai_compatible_credentials
from yeoman_gateway.security import NoopSecurity, SecurityEngine
from yeoman_gateway.session.manager import SessionManager
from yeoman_gateway.storage.inbound_archive import InboundArchive

if TYPE_CHECKING:
    from pathlib import Path

    from yeoman_shared.config.schema import Config, ExecToolConfig

    from yeoman_gateway.ipc.gateway_socket import GatewaySocket
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.providers.base import LLMProvider


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
                tb = traceback.format_exc()
                logger.error(
                    "vnext orchestrator failure channel={} chat={}: {} (type={})\n{}",
                    event.channel,
                    event.chat_id,
                    e,
                    type(e).__name__,
                    tb,
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
                    self._memory.record_idea_backlog_capture(
                        entry_kind=intent.entry_kind,
                        content=intent.content,
                        source="orchestrator_manual_capture",
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
    contacts: ContactsService
    bus: MessageBus | None = None
    gateway_socket: "GatewaySocket | None" = None

    async def run(self) -> None:
        tracing.init()
        try:
            await self.cron.start()
            await self.heartbeat.start()
            if self.gateway_socket:
                await self.gateway_socket.start()
            tasks = [
                self.orchestrator.run(),
                self.channels.start_all(),
            ]
            if self.bus:
                tasks.append(self.bus.dispatch_events())
            await asyncio.gather(*tasks)
        finally:
            if self.gateway_socket:
                await self.gateway_socket.stop()
            self.heartbeat.stop()
            self.cron.stop()
            self.orchestrator.stop()
            await self.channels.stop_all()
            await self.responder.aclose()
            self.inbound_archive.close()
            self.contacts.close()
            self.memory.close()
            await tracing.shutdown()


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

    from yeoman_shared.utils.helpers import get_operational_data_path

    session_manager = SessionManager(workspace)
    inbound_archive = InboundArchive(
        db_path=get_operational_data_path() / "inbound" / "reply_context.db",
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

    # Optional LLM-based input classifier (second defence layer).
    security_classifier = None
    if config.security.enabled and config.security.stages.input:
        try:
            from yeoman_gateway.security.classifier import InputClassifier

            security_classifier = InputClassifier(config=config)
        except Exception as exc:
            logger.warning("security classifier disabled: {}", exc)

    memory_service = MemoryService(workspace=workspace, config=config.memory, root_config=config)
    memory_state_dir = config.memory.wal.state_dir
    try:
        imported = memory_service.backfill_from_workspace_files(force=False)
        if imported > 0:
            logger.info("memory backfill imported {} entries", imported)
    except Exception as e:
        logger.warning("memory backfill failed: {}", e)

    contacts_service = ContactsService(
        db_path=get_operational_data_path() / "contacts" / "contacts.db",
    )
    contacts_service.mark_owner_from_policy(
        policy_engine.policy.owners if policy_engine else {},
    )
    memory_service.set_contacts(contacts_service)
    try:
        linked = contacts_service.backfill_memory(memory_service.store)
        if linked > 0:
            logger.info("contacts: backfilled {} memory nodes with contact_id", linked)
    except Exception as e:
        logger.warning("contacts: memory backfill failed: {}", e)

    cron_store_path = get_operational_data_path() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, sessions_dir=get_operational_data_path() / "inbound")

    # Create policy adapter first so we can use it for owner_alert_resolver
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=set(),  # Will be updated after responder is created
        policy_path=policy_path,
        session_manager=session_manager,
        workspace=workspace,
        memory_state_dir=memory_state_dir,
    )
    policy_adapter.set_memory_service(memory_service)

    file_access_resolver = build_file_access_resolver(
        workspace=workspace,
        policy=policy_engine.policy if policy_engine is not None else None,
    )

    openai_compat = resolve_openai_compatible_credentials(config)
    elevenlabs = config.providers.elevenlabs
    openrouter = config.providers.openrouter
    fish_api_key = os.environ.get("FISH_API_KEY") or ""
    tts = TTSSynthesizer(
        openai_api_key=openai_compat.api_key if openai_compat else None,
        openai_api_base=openai_compat.api_base if openai_compat else None,
        openai_extra_headers=openai_compat.extra_headers if openai_compat else None,
        elevenlabs_api_key=elevenlabs.api_key or None,
        elevenlabs_api_base=elevenlabs.api_base,
        elevenlabs_extra_headers=elevenlabs.extra_headers,
        elevenlabs_default_voice_id=elevenlabs.voice_id,
        elevenlabs_default_model_id=elevenlabs.model_id,
        openrouter_api_key=openrouter.api_key or None,
        openrouter_api_base=openrouter.api_base,
        openrouter_extra_headers=openrouter.extra_headers,
        fish_api_key=fish_api_key or None,
        fish_default_voice_id="71c095ed4c03459fb98500db63b88fbe",
        max_concurrency=config.channels.whatsapp.media.max_tts_concurrency,
    )

    # CalDAV service — enabled when iCloud credentials are in env
    _caldav_service = None
    _caldav_user = os.environ.get("ICLOUD_CALDAV_USERNAME")
    _caldav_pass = os.environ.get("ICLOUD_CALDAV_APP_PASSWORD")
    if _caldav_user and _caldav_pass:
        from yeoman_gateway.caldav.service import CalDAVService

        _caldav_service = CalDAVService(_caldav_user, _caldav_pass)
        logger.info("CalDAV service enabled for {}", _caldav_user)

    responder = LLMResponder(
        provider=provider,
        workspace=workspace,
        bus=bus,
        model=assistant_model,
        subagent_model=config.agents.defaults.subagent_model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        web_config=config.tools.web,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        session_manager=session_manager,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        cron_service=cron,
        contacts_service=contacts_service,
        caldav_service=_caldav_service,
        owner_alert_resolver=policy_adapter.owner_recipients,
        file_access_resolver=file_access_resolver,
        group_resolver=policy_adapter.resolve_whatsapp_group,
        model_router=model_router,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        inbound_archive=inbound_archive,
    )
    if policy_engine is not None:
        policy_engine.validate(set(responder.tool_names))

    # Wire /voice command callback: reuses the send_voice tool.
    async def _voice_send_callback(content: str, chat_id: str) -> str:
        return await responder.tools.execute(
            "send_voice",
            {
                "content": content,
                "channel": "whatsapp",
                "chat_id": chat_id,
                "voice": "71c095ed4c03459fb98500db63b88fbe",
                "verbatim": True,
            },
        )

    policy_adapter.set_voice_send_callback(_voice_send_callback)

    # Wire admin notify callback: sends text to a given channel+chat.
    async def _admin_notify(channel: str, chat_id: str, text: str) -> None:
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=text))

    policy_adapter.set_admin_notify_callback(_admin_notify)

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

    # Wire recording indicator: responder switches presence to mic icon during TTS
    async def _recording_notifier(channel: str, chat_id: str) -> None:
        await channels.set_recording(channel, chat_id)

    responder._recording_notifier = _recording_notifier

    from yeoman_gateway.cron.workflow_chain import build_chained_prompt, is_chain_failure
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState

    workflow_state = WorkflowState(
        store_path=Path(workspace) / "data" / "cron" / "pending_approvals.json"
    )

    async def _handle_approved_job(approval: PendingApproval) -> None:
        next_job = cron.get_job(approval.next_job_id)
        if not next_job:
            logger.warning("Approved job {} not found", approval.next_job_id)
            return
        prompt = build_chained_prompt(
            approval.previous_output, next_job.payload.message,
            input_from_previous=next_job.payload.input_from_previous,
        )
        next_job.payload.max_chain_depth = approval.remaining_depth
        response = await responder.process_direct(
            prompt, session_key=f"cron:{next_job.id}",
            channel=next_job.payload.channel or "cli",
            chat_id=next_job.payload.to or "direct",
            model_profile=next_job.payload.model_profile,
        )
        if next_job.payload.deliver and next_job.payload.to:
            await bus.publish_outbound(OutboundMessage(
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to, content=response or "",
            ))
        if next_job.payload.next_job_id and response and not is_chain_failure(response):
            await _handle_chain(next_job, response)

    archive_adapter = SqliteReplyArchiveAdapter(inbound_archive)
    orchestrator = Orchestrator(
        policy=policy_adapter,
        responder=responder,
        reply_archive=archive_adapter,
        contacts=contacts_service,
        reply_context_window_limit=config.channels.whatsapp.reply_context_window_limit,
        reply_context_line_max_chars=config.channels.whatsapp.reply_context_line_max_chars,
        ambient_window_limit=config.channels.whatsapp.ambient_window_limit,
        typing_notifier=typing_adapter,
        security=security,
        security_classifier=security_classifier,
        security_block_message=config.security.block_user_message,
        policy_admin_handler=admin_command_handler,
        model_router=model_router,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        owner_alert_resolver=policy_adapter.owner_recipients,
        workflow_state=workflow_state,
        approval_trigger=_handle_approved_job,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        if job.payload.kind == "voice_broadcast":
            phrases = [str(v).strip() for v in list(job.payload.voice_messages) if str(v).strip()]
            if not phrases and str(job.payload.message or "").strip():
                phrases = [str(job.payload.message).strip()]
            if not phrases:
                raise ValueError("voice_broadcast job has no message candidates")

            content = random.choice(phrases) if job.payload.voice_random else phrases[0]
            args: dict[str, object] = {"content": content}
            if job.payload.voice_verbatim:
                args["verbatim"] = True
            if job.payload.voice_name:
                args["voice"] = job.payload.voice_name
            if job.payload.voice_tts_route:
                args["tts_route"] = job.payload.voice_tts_route
            if job.payload.voice_max_sentences is not None:
                args["max_sentences"] = int(job.payload.voice_max_sentences)
            if job.payload.voice_max_chars is not None:
                args["max_chars"] = int(job.payload.voice_max_chars)

            voice_channel = str(job.payload.voice_channel or "").strip() or "whatsapp"
            args["channel"] = voice_channel
            if str(job.payload.voice_group or "").strip():
                args["group"] = str(job.payload.voice_group).strip()
            else:
                chat_target = (
                    str(job.payload.voice_chat_id or "").strip()
                    or str(job.payload.to or "").strip()
                )
                if not chat_target:
                    raise ValueError("voice_broadcast job has no target chat")
                args["chat_id"] = chat_target

            result = await responder.tools.execute("send_voice", args)
            if str(result).startswith("Error:"):
                raise RuntimeError(str(result))
            return str(result)

        response = await responder.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            model_profile=job.payload.model_profile,
        )
        if job.payload.deliver and job.payload.to:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )

        # Workflow chaining
        if job.payload.next_job_id and response is not None:
            if is_chain_failure(response):
                fail_channel = job.payload.approval_channel or job.payload.channel or "cli"
                fail_chat = job.payload.to or "direct"
                wf_name = job.payload.workflow_id or job.id
                await bus.publish_outbound(OutboundMessage(
                    channel=fail_channel, chat_id=fail_chat,
                    content=f"Workflow '{wf_name}' failed at step {job.payload.workflow_step}: {response[:200]}. Use /cron workflow_list to review.",
                ))
            else:
                await _handle_chain(job, response)

        return response

    async def _handle_chain(job: CronJob, output: str) -> None:
        next_job = cron.get_job(job.payload.next_job_id) if job.payload.next_job_id else None
        if not next_job:
            logger.warning("Chained job {} not found", job.payload.next_job_id)
            return

        remaining = job.payload.max_chain_depth - 1
        if remaining <= 0:
            wf_name = job.payload.workflow_id or job.id
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.approval_channel or job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                content=f"Workflow '{wf_name}' stopped: max chain depth reached.",
            ))
            return

        if job.payload.requires_approval:
            from uuid import uuid4
            approval_id = f"wf-approve-{job.id}-{uuid4().hex[:8]}"
            approval_channel = job.payload.approval_channel or job.payload.channel or "cli"
            approval_chat = job.payload.to or "direct"

            await workflow_state.add(PendingApproval(
                approval_id=approval_id,
                next_job_id=next_job.id,
                previous_output=output,
                channel=approval_channel,
                chat_id=approval_chat,
                created_at=time.time(),
                expires_at=time.time() + 86400,
                workflow_id=job.payload.workflow_id,
                remaining_depth=remaining,
            ))

            await bus.publish_outbound(OutboundMessage(
                channel=approval_channel, chat_id=approval_chat,
                content=(
                    f"{output}\n\n---\n"
                    f"Workflow step {job.payload.workflow_step} complete.\n"
                    f"Next: {next_job.name}\n"
                    f"Reply with this code to approve: {approval_id}"
                ),
            ))
        else:
            prompt = build_chained_prompt(output, next_job.payload.message, input_from_previous=next_job.payload.input_from_previous)
            next_job.payload.max_chain_depth = remaining
            chain_response = await responder.process_direct(
                prompt,
                session_key=f"cron:{next_job.id}",
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to or "direct",
                model_profile=next_job.payload.model_profile,
            )
            if next_job.payload.deliver and next_job.payload.to:
                await bus.publish_outbound(OutboundMessage(
                    channel=next_job.payload.channel or "cli",
                    chat_id=next_job.payload.to,
                    content=chain_response or "",
                ))
            if next_job.payload.next_job_id and chain_response is not None and not is_chain_failure(chain_response):
                await _handle_chain(next_job, chain_response)

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

    # IPC socket for overseer commands
    from yeoman_gateway.ipc.gateway_socket import GatewaySocket

    ipc_config = config.ipc
    socket_path = Path(ipc_config.gateway_socket_path).expanduser()

    async def ipc_send_message(channel: str, chat_id: str, content: str) -> dict:
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=content)
        )
        return {"delivered": True}

    async def ipc_trigger_agent_turn(
        prompt: str,
        session_key: str,
        channel: str,
        chat_id: str,
        model_profile: str | None = None,
    ) -> dict:
        response = await responder.process_direct(
            prompt,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            model_profile=model_profile,
        )
        return {"response": response}

    async def ipc_publish_event(kind: str, detail: dict) -> dict:
        from yeoman_gateway.bus.events import SystemEvent

        await bus.publish_event(SystemEvent(kind=kind, detail=detail, timestamp=time.time()))
        return {"published": True}

    gateway_socket = GatewaySocket(
        path=socket_path,
        send_message_handler=ipc_send_message,
        trigger_agent_turn_handler=ipc_trigger_agent_turn,
        publish_event_handler=ipc_publish_event,
        rate_limit=ipc_config.command_rate_limit,
    )

    return GatewayRuntime(
        orchestrator=orchestrator_service,
        channels=channels,
        cron=cron,
        heartbeat=heartbeat,
        inbound_archive=inbound_archive,
        responder=responder,
        memory=memory_service,
        contacts=contacts_service,
        bus=bus,
        gateway_socket=gateway_socket,
    )
