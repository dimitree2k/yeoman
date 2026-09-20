"""Application bootstrap and runtime wiring for the vNext orchestrator."""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_never
from uuid import uuid4 as _uuid4

from loguru import logger
from yeoman_shared.telemetry import InMemoryTelemetry, tracing

from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.adapters.typing_channel_manager import ChannelManagerTypingAdapter
from yeoman_gateway.agent.tools.file_access import build_file_access_resolver
from yeoman_gateway.bus.events import InboundMessage, OutboundMessage, ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.manager import ChannelManager
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
from yeoman_gateway.cron.service import CronJobDeferredError, CronJobSkippedError, CronService
from yeoman_gateway.cron.types import CronJob
from yeoman_gateway.cron.voice import evaluate_voice_quiet_gate
from yeoman_gateway.heartbeat.service import HeartbeatService
from yeoman_gateway.knowledge._contacts.service import ContactsService
from yeoman_gateway.knowledge._memory import MemoryService
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.media.document_processing import DocumentProcessor
from yeoman_gateway.media.lazy_resolver import LazyMediaResolver
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.media.tts import TTSSynthesizer
from yeoman_gateway.media.vision import VisionDescriber
from yeoman_gateway.persona_evolution import (
    PersonaEvolutionLedger,
    build_persona_evolution_approval_message,
    persona_evolution_result_needs_notification,
    run_persona_evolution_cron,
)
from yeoman_gateway.pipeline.responder import requested_report_length
from yeoman_gateway.policy.capabilities import policy_known_tools
from yeoman_gateway.policy.persona import load_persona_text
from yeoman_gateway.processing.dispatch import (
    SERVICE_PRINCIPALS,
    ServiceEffectProducer,
    disable_non_migrated_tools,
)
from yeoman_gateway.processing.invalidation import SignalInvalidator
from yeoman_gateway.processing.models import MediaPayload, canonical_hash
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.providers.factory import ProviderFactory
from yeoman_gateway.providers.openai_compatible import resolve_openai_compatible_credentials
from yeoman_gateway.security import NoopSecurity, SecurityEngine
from yeoman_gateway.session.manager import SessionManager
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_gateway.storage.private_handoff import PrivateHandoffStore

if TYPE_CHECKING:
    from pathlib import Path

    from yeoman_shared.config.schema import Config, ExecToolConfig

    from yeoman_gateway.ipc.gateway_socket import GatewaySocket
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.processing.dispatch import IntentEffectRouter
    from yeoman_gateway.processing.store import ProcessingStore
    from yeoman_gateway.providers.base import LLMProvider


#: Character boundary for the cheap continuation-candidate heuristic (spec section 7.1).
#: It is an internal cost heuristic deciding which *quota* an opportunity may use - it
#: never decides whether a reply is allowed.
CONTINUATION_CANDIDATE_MAX_CHARS = 120


def _normalize_timestamp(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def policy_validation_tools(responder_tools: set[str]) -> set[str]:
    """Include service-only policy capabilities in startup validation."""
    return policy_known_tools(responder_tools)


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


def build_a2a_voice_artifact_store(
    *,
    root: Path | None,
    managed_outgoing_root: Path,
    tts: TTSSynthesizer,
    model_router: ModelRouter,
    max_bytes: int,
    ttl_seconds: int,
) -> Any | None:
    """Build the optional voice store only for a usable configured TTS route."""
    if (
        root is None
        or not root.is_absolute()
        or not managed_outgoing_root.expanduser().is_absolute()
        or max_bytes <= 0
        or ttl_seconds <= 0
    ):
        return None
    try:
        from yeoman_gateway.a2a.artifacts import VoiceArtifactStore

        managed = managed_outgoing_root.expanduser().resolve(strict=True)
        candidate = root.expanduser().resolve(strict=False)
        if candidate == managed or not candidate.is_relative_to(managed):
            return None
        profile = model_router.resolve("tts.speak", channel="whatsapp")
        store = VoiceArtifactStore(
            root,
            tts=tts,
            profile=profile,
            max_bytes=max_bytes,
            ttl_seconds=ttl_seconds,
            managed_outgoing_root=managed_outgoing_root,
        )
    except (KeyError, OSError, ValueError):
        return None
    return store if store.available else None


def resolve_a2a_artifact_root(
    root: Path | None, managed_outgoing_root: Path
) -> Path | None:
    """Return a configured A2A subdirectory only when it stays under managed media."""
    if root is None or not root.is_absolute() or not managed_outgoing_root.is_absolute():
        return None
    try:
        managed = managed_outgoing_root.expanduser().resolve(strict=True)
        candidate = root.expanduser().resolve(strict=False)
    except OSError:
        return None
    return candidate if candidate != managed and candidate.is_relative_to(managed) else None


def _inbound_message_to_event(msg: InboundMessage) -> InboundEvent:
    meta = msg.metadata
    # Thread assignment from the fast gate travels with the event so the pipeline can
    # keep the own thread out of the ambient block.
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
        effect_router: "IntentEffectRouter | None" = None,
        processing_store: object | None = None,
        release_participation_chat: Callable[[str, str], None] | None = None,
        max_concurrent_messages: int = 4,
    ) -> None:
        self._bus = bus
        self._orchestrator = orchestrator
        self._typing_adapter = typing_adapter
        self._telemetry = telemetry
        self._memory = memory
        self._effect_router = effect_router
        self._processing_store = processing_store
        self._release_participation_chat = release_participation_chat
        self._running = False
        # Review F03: ingest must not wait for a running generation. Generations stay
        # bounded by the actor registry (one by default), so a second message can be
        # admitted and parked in the postbox while the first answer is still in flight.
        self._ingest_slots = asyncio.Semaphore(max(1, int(max_concurrent_messages)))
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            event = _inbound_message_to_event(msg)
            task = asyncio.create_task(self._process_message(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # A stop is graceful: in-flight messages finish (bounded) instead of being cut in
        # half, so run() returning means the work it accepted is done.
        await self._drain()

    async def _drain(self, *, timeout: float = 20.0) -> None:
        while self._tasks:
            pending = set(self._tasks)
            _done, still_running = await asyncio.wait(pending, timeout=timeout)
            if not still_running:
                return
            for task in still_running:
                task.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)
            return

    async def _process_message(self, event: Any) -> None:
        """Run one message through pipeline and dispatch without blocking the loop."""
        async with self._ingest_slots:
            try:
                intents = await self._orchestrator.handle(event)
                await self._dispatch_intents(intents, principal=event.sender_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "vnext orchestrator failure stage=handle_dispatch channel={} chat={} "
                    "message_id={} error_type={} state={} detail={}",
                    event.channel,
                    event.chat_id,
                    event.message_id,
                    type(e).__name__,
                    getattr(e, "state", "-"),
                    getattr(e, "detail", None) or str(e)[:200],
                )
            finally:
                self._finish_direct_event(event)

    def _finish_direct_event(self, event: object) -> None:
        """Finish a direct binding after terminal intent dispatch, including admin commands."""
        store = self._processing_store
        lookup = getattr(store, "direct_admission_for_event", None)
        finish = getattr(store, "finish_direct_admission", None)
        active = getattr(store, "direct_work_active", None)
        if not callable(lookup) or not callable(finish) or not callable(active):
            return
        channel = str(getattr(event, "channel", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        event_id = str(getattr(event, "message_id", "") or "")
        try:
            binding = lookup(event_id, channel=channel, chat_id=chat_id)
            if not binding or len(binding) < 5 or str(binding[4]) != "active":
                return
            finish(str(binding[2]))
            if not active(channel, chat_id) and self._release_participation_chat is not None:
                self._release_participation_chat(channel, chat_id)
        except Exception as exc:  # noqa: BLE001 - terminal cleanup cannot fail dispatch
            logger.warning(
                "direct_terminal_cleanup_failed chat={} error_type={}",
                chat_id,
                type(exc).__name__,
            )

    def stop(self) -> None:
        self._running = False

    async def _dispatch_intents(
        self, intents: list[OrchestratorIntent], *, principal: str = ""
    ) -> None:
        for intent in intents:
            match intent:
                case SetTypingIntent():
                    await self._typing_adapter(intent.channel, intent.chat_id, intent.enabled)
                case SendOutboundIntent():
                    if self._effect_router is not None and await self._effect_router.submit_outbound(
                        intent, principal=principal
                    ):
                        continue
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
                    if self._effect_router is not None and await self._effect_router.submit_reaction(
                        intent, principal=principal
                    ):
                        continue
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
    chat_registry: object
    bus: MessageBus | None = None
    gateway_socket: "GatewaySocket | None" = None
    speakup_log: object | None = None
    lull_observer: object | None = None
    opportunity_scheduler: object | None = None
    participation_maintenance: object | None = None
    processing: "ProcessingStore | None" = None
    reconciliation: object | None = None
    retention: object | None = None
    shared_facts: object | None = None
    startup_hook: Callable[[], Awaitable[None]] | None = None

    async def _start_processing_services(self) -> None:
        """Plan 04 start order: recover and reconcile before any channel consumes input.

        The reconciler's first tick runs synchronously, so a claim left ``executing`` by
        a previous process is turned into ``unknown`` and probed before
        ``channels.start_all()``. Disabled mode has neither service and stays inert.
        """
        if self.reconciliation is not None:
            # Recovery is a startup fence: if its first deterministic pass fails,
            # no socket, channel or producer may begin new work on stale state.
            recover_once = getattr(self.reconciliation, "recover_once", None)
            if callable(recover_once):
                await recover_once()
            else:
                await self.reconciliation.tick_once()
            await self.reconciliation.start()
        if self.retention is not None:
            await self.retention.start()

    def _resume_a2a_research(self) -> None:
        """Resume durable Hermes polling after the runtime event loop exists."""

        a2a_tool = self.responder.tools.get("a2a_delegate")
        resume = getattr(a2a_tool, "resume_pending_research", None)
        if callable(resume):
            resume()

    async def run(self) -> None:
        tracing.init()
        try:
            await self._start_processing_services()
            self._resume_a2a_research()
            await self.cron.start()
            await self.heartbeat.start()
            if self.lull_observer is not None and hasattr(self.lull_observer, "start"):
                await self.lull_observer.start()
            if self.opportunity_scheduler is not None:
                await self.opportunity_scheduler.start()
            if self.participation_maintenance is not None:
                maintenance_start = getattr(self.participation_maintenance, "start", None)
                if maintenance_start is not None:
                    await maintenance_start()
            if self.gateway_socket:
                await self.gateway_socket.start()
            if self.shared_facts is not None and hasattr(self.shared_facts, "start"):
                self.shared_facts.start()
            tasks = [
                self.orchestrator.run(),
                self.channels.start_all(),
            ]
            if self.startup_hook is not None:
                async def _run_startup_hook() -> None:
                    await asyncio.sleep(2.0)
                    try:
                        await self.startup_hook()
                    except Exception:
                        logger.exception("Gateway startup hook failed")

                tasks.append(_run_startup_hook())
            if self.bus:
                tasks.append(self.bus.dispatch_events())
            await asyncio.gather(*tasks)
        finally:
            if self.gateway_socket:
                await self.gateway_socket.stop()
            self.heartbeat.stop()
            if self.opportunity_scheduler is not None:
                await self.opportunity_scheduler.stop()
            if self.participation_maintenance is not None:
                maintenance_stop = getattr(self.participation_maintenance, "stop", None)
                if maintenance_stop is not None:
                    await maintenance_stop()
            if self.lull_observer is not None and hasattr(self.lull_observer, "stop"):
                self.lull_observer.stop()
            self.cron.stop()
            self.orchestrator.stop()
            await self.channels.stop_all()
            if self.reconciliation is not None:
                await self.reconciliation.stop()
            if self.retention is not None:
                await self.retention.stop()
            await self.responder.aclose()
            self.inbound_archive.close()
            if self.speakup_log is not None and hasattr(self.speakup_log, "close"):
                self.speakup_log.close()
            if hasattr(self.chat_registry, "close"):
                self.chat_registry.close()
            if self.shared_facts is not None and hasattr(self.shared_facts, "stop"):
                self.shared_facts.stop()
            self.contacts.close()
            self.memory.close()
            if self.processing is not None:
                self.processing.close()
            await tracing.shutdown()


class ProcessingStoreUnavailableError(RuntimeError):
    """The new mode is enabled but its durable store cannot be opened (review F01)."""


def _processing_store_path(config: "Config") -> Path:
    from yeoman_shared.utils.helpers import get_data_path

    path = Path(config.processing.db_path).expanduser()
    return path if path.is_absolute() else get_data_path() / path


def _has_pending_participation_recovery(
    *, speakup_path: Path, processing_path: Path
) -> bool:
    """Inspect existing ledgers, allowing SQLite to recover a hot journal."""
    import sqlite3

    def _contains(path: Path, table: str, columns: set[str], query: str) -> bool:
        if not path.is_file():
            return False
        connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
        try:
            present = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns.issubset(present):
                return False
            return connection.execute(query).fetchone() is not None
        finally:
            connection.close()

    return _contains(
        speakup_path,
        "delivery_reservations",
        {"origin", "lane", "delivery_state"},
        "SELECT 1 FROM delivery_reservations "
        "WHERE origin = 'participation' AND lane = 'production' AND delivery_state IN "
        "('reserved','submitted','transport_accepted','delivery_unknown') LIMIT 1",
    ) or _contains(
        processing_path,
        "effects",
        {"origin", "state"},
        "SELECT 1 FROM effects WHERE origin = 'participation' AND state IN "
        "('queued','executing','sent','blocked','expired','failed','cancelled','unknown') "
        "LIMIT 1",
    )


def build_processing_store(
    config: "Config", *, recover_pending: bool = False
) -> "ProcessingStore | None":
    """Open the durable processing store, but only when the new mode is enabled.

    Disabled mode stays byte-for-byte inert: no second database appears next to the
    archives. A store that cannot be opened leaves the new mode offline (fail closed)
    instead of degrading into an unaudited path.
    """
    if not config.processing.enabled and not recover_pending:
        return None

    from yeoman_gateway.processing.models import DAY_MS, RetentionSettings
    from yeoman_gateway.processing.store import ProcessingStore

    retention_cfg = config.processing.retention
    retention = RetentionSettings(
        journal_payload_ms=retention_cfg.journal_payload_days * DAY_MS,
        metadata_ms=retention_cfg.lineage_metadata_days * DAY_MS,
        unresolved_ms=retention_cfg.unresolved_days * DAY_MS,
    )
    path = _processing_store_path(config)
    try:
        return ProcessingStore(path, retention=retention)
    except Exception as exc:
        # Review F01: returning None here did not keep the mode "offline". Every later
        # builder then returned None as well, so no fast gate, no effect router and no
        # managed-outbound guard were installed - and the legacy path published for chats
        # that are configured as managed. A store we cannot open is a startup failure.
        logger.critical("processing store unavailable path={} error={}", path, exc)
        raise ProcessingStoreUnavailableError(
            f"processing.enabled is set but the store at {path} cannot be opened: {exc}"
        ) from exc


@dataclass
class SharedFactRuntime:
    """Read gate plus (optionally) the extraction queue for one gateway process."""

    gate: object
    extraction: object | None
    memory: object
    processing: object | None = None
    chat_registry: object | None = None
    policy: object | None = None
    config: object | None = None

    @property
    def extraction_enabled(self) -> bool:
        return self.extraction is not None

    def start(self) -> None:
        if self.extraction is not None:
            self.extraction.start()

    def stop(self) -> None:
        if self.extraction is not None:
            self.extraction.stop()


def build_shared_fact_runtime(
    config: "Config",
    *,
    store: "ProcessingStore | None" = None,
    processing: object | None = None,
    chat_registry: object | None = None,
    policy: object | None = None,
    memory: "MemoryService | None" = None,
) -> SharedFactRuntime | None:
    """Shared-fact runtime (Plan 05), or ``None`` when any switch is off.

    Three switches must agree before anything exists: memory, the shared-fact opt-in and
    the new processing mode. Disabled mode therefore has no worker thread, no job row and
    no gate object - the same fail-closed shape as :func:`build_processing_store`.
    """
    if not getattr(getattr(config, "memory", None), "enabled", False):
        return None
    shared = getattr(config.memory, "shared", None)
    if shared is None or not bool(getattr(shared, "enabled", False)):
        return None
    if not getattr(getattr(config, "processing", None), "enabled", False):
        return None
    if memory is None or store is None:
        return None

    from yeoman_gateway.knowledge._memory.extraction_jobs import (
        EXTRACTOR_VERSION,
        SharedFactExtractionQueue,
    )
    from yeoman_gateway.knowledge._memory.read_gate import FactReadGate

    extraction_cfg = getattr(config.processing, "extraction", None)
    retention_cfg = getattr(config.processing, "retention", None)
    fact_ttl_ms = (
        int(retention_cfg.shared_fact_days) * 24 * 3600 * 1000 if retention_cfg else None
    )
    extractor = None
    if bool(getattr(shared, "extraction_enabled", False)):
        from yeoman_gateway.knowledge._memory.fact_extractor import SharedFactExtractor

        try:
            tz_name = str(getattr(extraction_cfg, "timezone", "UTC") or "UTC")
            extractor = SharedFactExtractor(
                config=config,
                timezone_name=tz_name,
                route_key=str(
                    getattr(
                        getattr(config.memory, "capture", None),
                        "extract_route",
                        "memory.capture.extract",
                    )
                ),
                member_provider=_shared_fact_members(chat_registry),
            )
        except Exception:
            logger.exception("shared fact extractor unavailable; extraction stays off")
            extractor = None
    queue = SharedFactExtractionQueue(
        store=memory.store,
        journal=store,
        extractor=extractor,
        embedder=getattr(memory, "embedding", None),
        idle_ms=int(getattr(extraction_cfg, "idle_seconds", 60)) * 1000,
        max_delay_ms=int(getattr(extraction_cfg, "max_delay_seconds", 300)) * 1000,
        max_waiting=int(getattr(shared, "max_jobs_waiting", 64)),
        fact_ttl_ms=fact_ttl_ms,
        extractor_version=str(getattr(shared, "extractor_version", EXTRACTOR_VERSION)),
    )
    runtime = SharedFactRuntime(
        gate=FactReadGate(memory.store),
        extraction=queue if bool(getattr(shared, "extraction_enabled", False)) else None,
        memory=memory,
        processing=processing,
        chat_registry=chat_registry,
        policy=policy,
        config=shared,
    )
    memory.extraction = runtime.extraction
    return runtime


def _build_reaction_action(
    config: "Config", effect_router: "IntentEffectRouter | None", store: object | None
):
    """The `react` reply action: one cheap emoji choice per message, no answer turn.

    Built only when the mode is on and at least one chat actually asks for reactions - an
    unused provider client is not worth the start-up cost. A misconfigured route disables
    the action loudly instead of answering text where a reaction was configured.
    """
    if effect_router is None or store is None or not config.processing.enabled:
        return None
    wanted = {
        str(value).strip().lower()
        for value in (config.processing.reply_actions or {}).values()
    }
    if "react" not in wanted:
        return None
    from yeoman_gateway.processing.reaction_action import ReactionAction, ReactionChooser

    route = str(getattr(config.processing, "reaction_route", "") or "") or (
        str(getattr(getattr(config.memory, "capture", None), "extract_route", "") or "")
        or "memory.capture.extract"
    )
    try:
        chooser = ReactionChooser(config=config, route_key=route)
    except Exception as exc:
        logger.error(
            "reaction action disabled: route={} error_type={} detail={}",
            route,
            type(exc).__name__,
            str(exc)[:160],
        )
        return None
    logger.info("reaction_action enabled route={} emojis={}", route, len(config.processing.reaction_emojis))
    return ReactionAction(
        chooser=chooser,
        router=effect_router,
        allowed_emojis=tuple(config.processing.reaction_emojis),
    )


def _build_ambient_judge(config: "Config"):
    """The verdict that decides whether an unaddressed message may be answered.

    Built only when the mode is on, the owner released at least one chat for ambient
    answers, and a route is available. A missing route disables ambient answering loudly:
    silence is the safe failure, an unguarded answer is not.
    """
    if not config.processing.enabled or not config.processing.ambient_chats:
        return None
    from yeoman_gateway.processing.ambient_judge import AmbientJudge
    from yeoman_gateway.processing.model_route import (
        RouteClient,
        RouteUnavailableError,
        resolve_route_key,
    )

    settings = config.processing.ambient
    route = resolve_route_key(
        config,
        getattr(settings, "judge_route", ""),
        getattr(config.processing, "reaction_route", ""),
        str(getattr(getattr(config.memory, "capture", None), "extract_route", "") or ""),
        "memory.capture.extract",
    )
    try:
        client = RouteClient(config=config, route_key=route)
    except RouteUnavailableError as exc:
        logger.error("ambient judge disabled: route={} detail={}", route, str(exc)[:160])
        return None
    logger.info(
        "ambient_judge enabled route={} emojis={} min_confidence={} min_seconds={} "
        "min_messages={}",
        route,
        len(config.processing.reaction_emojis),
        settings.judge_min_confidence,
        settings.min_seconds_between_answers,
        settings.min_messages_since_answer,
    )
    return AmbientJudge(
        client=client,
        allowed_emojis=tuple(config.processing.reaction_emojis),
        min_confidence=settings.judge_min_confidence,
        timeout_seconds=settings.judge_timeout_seconds,
    )


def _offer_participation_trigger(
    *,
    channel: str,
    chat_id: str,
    trigger: str,
    runtime: object | None,
    policy_adapter: object,
    material_provider: Callable[
        [str, str, tuple[str, ...] | None], tuple[tuple[str, ...], int]
    ]
    | None,
) -> dict[str, str] | None:
    """Offer to Participation and return ``None`` only when Legacy still owns."""
    current = getattr(policy_adapter, "current_activation", None)
    snapshot = current(channel, chat_id) if callable(current) else None
    live = bool(snapshot is not None and getattr(snapshot, "live", False))
    observing = bool(snapshot is not None and getattr(snapshot, "observing", False))
    if live and runtime is None:
        return {"status": "skipped"}
    if runtime is None or not (live or observing):
        return None

    pause_reason = getattr(policy_adapter, "participation_pause_reason", None)
    if callable(pause_reason) and pause_reason(channel, chat_id):
        return {"status": "skipped"} if live else None

    sources, revision = (
        material_provider(channel, chat_id, None)
        if callable(material_provider)
        else ((), 0)
    )
    offer = getattr(runtime, "offer_source", None)
    offered = bool(
        sources
        and callable(offer)
        and offer(
            channel=channel,
            chat_id=chat_id,
            source_event_ids=tuple(sources),
            observed_revision=int(revision),
            trigger=trigger,
        )
    )
    if live:
        return {"status": "offered" if offered else "skipped"}
    return None


def _build_participation_runtime(
    *,
    config: Config,
    source_owner: object,
    log: object,
    policy_engine: object | None,
    inbound_archive: object | None,
    processing_store: object | None,
    responder: object | None,
    policy_adapter: object | None,
    approval_tools: object | None = None,
) -> tuple[object | None, object | None, object | None]:
    """Build the disabled-by-default participation runtime and scheduler.

    Returns ``(offer_runtime, scheduler, decision_runtime)``. All three are ``None``
    unless autonomy is enabled globally: with the feature off, observers keep their
    legacy behaviour exactly and nothing new is constructed. A missing route or store
    leaves the new path absent rather than falling back to an unguarded one.
    """
    participation = getattr(config.processing, "participation", None)
    if participation is None or not bool(getattr(participation, "enabled", False)):
        return None, None, None
    from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader
    from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
    from yeoman_gateway.consciousness.participation_runtime import (
        ParticipationRuntime as OpportunityOfferRuntime,
    )
    from yeoman_gateway.processing.model_route import RouteClient, RouteUnavailableError
    from yeoman_gateway.processing.participation import ParticipationJudge
    from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
    from yeoman_gateway.processing.participation_runtime import (
        ParticipationRuntime as ParticipationDecisionRuntime,
    )

    route_key = str(getattr(participation, "judge_route", "") or "").strip()
    if not route_key:
        logger.error("participation disabled: judgeRoute is required when it is enabled")
        return None, None, None
    try:
        client = RouteClient(config=config, route_key=route_key)
    except RouteUnavailableError as exc:
        logger.error(
            "participation disabled: route={} detail={}", route_key, str(exc)[:160]
        )
        return None, None, None
    judge = ParticipationJudge(
        client=client,
        allowed_emojis=tuple(config.processing.reaction_emojis),
        timeout_seconds=float(getattr(participation, "judge_timeout_seconds", 12.0)),
        max_input_tokens=int(getattr(participation, "judge_max_input_tokens", 4000)),
        max_output_tokens=int(getattr(participation, "judge_max_output_tokens", 256)),
    )

    anchors = (
        DeliveryAnchorReader(log=log, store=processing_store)  # type: ignore[arg-type]
        if processing_store is not None
        else None
    )

    def _taste_hits(channel: str, chat_id: str) -> list[dict[str, object]]:
        taste_reader = getattr(responder, "memory", None)
        if taste_reader is None or not hasattr(taste_reader, "learned_chat_taste"):
            return []
        hits = taste_reader.learned_chat_taste(
            channel=channel,
            chat_id=chat_id,
            limit=5,
            require_meta={"provenance": "participation:v1"},
        )
        rendered: list[dict[str, object]] = []
        for hit in hits:
            entry = getattr(hit, "entry", None)
            rendered.append(
                {
                    "content": str(getattr(entry, "content", "")),
                    "provenance": "participation:v1",
                    "confidence": getattr(entry, "confidence", None),
                }
            )
        return rendered

    def _source_authorized(row: Mapping[str, object]) -> bool:
        return participant_is_allowed(
            engine=policy_engine,
            channel=str(row.get("channel") or ""),
            chat_id=str(row.get("chat_id") or ""),
            sender=str(row.get("sender_id") or row.get("participant") or ""),
        )

    context_builder = ParticipationContextBuilder(
        archive=inbound_archive,
        policy=policy_engine,
        anchors=anchors,
        taste=_taste_hits,
        source_authorizer=_source_authorized,
    )

    def _continuation_candidate(opportunity: object | None) -> bool:
        """Cheap source evidence for the protected continuation quota (spec 7.1).

        Qualification is deliberately evidence-based and cheap: a short unquoted
        message (the internal cost heuristic) is a possible social continuation, so it
        may use the reserved slots. This admits *judgment* only - relatedness, the
        delivered anchor and the social budgets are still decided by the judge and the
        ledger, and a comment claiming continuity without a delivered anchor is still
        refused.
        """
        if opportunity is None:
            return False
        sources = getattr(opportunity, "source_event_ids", ()) or ()
        if not sources:
            return False
        lookup = getattr(inbound_archive, "lookup_message", None)
        if lookup is None:
            return False
        for source_id in sources:
            token = str(source_id)
            if token.startswith("observed:"):
                continue
            try:
                row = lookup(
                    str(getattr(opportunity, "channel", "")),
                    str(getattr(opportunity, "chat_id", "")),
                    token,
                )
            except Exception:
                return False
            if row is None:
                continue
            text = str(row.get("text") or "").strip()
            if text and len(text) <= CONTINUATION_CANDIDATE_MAX_CHARS:
                return True
        return False

    def _snapshot(
        channel: str, chat_id: str, *, epoch: int, opportunity: object | None = None
    ) -> dict[str, object]:
        del epoch
        current_activation = getattr(policy_adapter, "current_activation", None)
        resolved = current_activation(channel, chat_id) if callable(current_activation) else None
        if resolved is None:
            raise RuntimeError("participation activation unavailable")
        reaction_limit = int(resolved.participation.max_reactions_per_window)
        comment_limit = int(resolved.participation.max_unsolicited_comments_per_window)
        window_ms = int(resolved.participation.comment_window_minutes) * 60_000
        try:
            effective = policy_engine.resolve_policy(channel, chat_id)  # type: ignore[union-attr]
        except Exception as exc:
            raise RuntimeError("participation policy unavailable") from exc
        daily_cap = (
            effective.spontaneity_daily_cap
            if effective.spontaneity_daily_cap is not None
            else int(config.consciousness.default_daily_cap)
        )
        allowed_contribution_types = effective.spontaneity_allowed_actions
        if allowed_contribution_types is None:
            from yeoman_gateway.consciousness.tools import (
                DEFAULT_BALANCED_ACTIONS,
                DEFAULT_HELPFUL_ACTIONS,
                DEFAULT_PERMISSIVE_ACTIONS,
            )

            allowed_contribution_types = list(
                DEFAULT_PERMISSIVE_ACTIONS
                if effective.spontaneity_profile == "permissive"
                else DEFAULT_BALANCED_ACTIONS
                if effective.spontaneity_profile == "balanced"
                else DEFAULT_HELPFUL_ACTIONS
            )
        reply_actions = config.processing.reply_actions or {}
        reply_action = str(
            reply_actions.get(f"{channel}:{chat_id}", "answer")
            if isinstance(reply_actions, Mapping)
            else "answer"
        ).strip().lower()
        policy_state = policy_adapter.policy_snapshot()
        current_source_ids = tuple(
            str(item)
            for item in (getattr(opportunity, "source_event_ids", ()) or ())
            if str(item)
        )
        context_revision = int(getattr(opportunity, "observed_revision", 0) or 0)
        material_reader = getattr(log, "material_for_opportunity", None)
        if callable(material_reader):
            try:
                pending_ids, latest_revision = material_reader(
                    channel,
                    chat_id,
                    None,
                    lane=str(resolved.lane),
                )
                current_source_ids = tuple(
                    dict.fromkeys((*current_source_ids, *(str(item) for item in pending_ids)))
                )
                context_revision = max(context_revision, int(latest_revision))
            except Exception as exc:
                raise RuntimeError("participation material unavailable") from exc
        return {
            "enabled": resolved.enabled,
            "opted_in": resolved.opted_in,
            "invalid_reason": resolved.invalid_reason,
            "activation_epoch": resolved.activation_epoch,
            "lane": resolved.lane,
            "policy_version": resolved.policy_version,
            "policy_hash": policy_state.policy_hash,
            "allow_initiation": bool(resolved.participation.allow_initiation),
            "allow_continuation": bool(resolved.participation.allow_continuation),
            "allow_reactions": bool(resolved.participation.allow_reactions),
            "spontaneity_enabled": bool(effective.spontaneity_enabled),
            "spontaneity_daily_cap": max(0, int(daily_cap or 0)),
            "spontaneity_allowed_actions": tuple(
                sorted(str(item) for item in allowed_contribution_types)
            ),
            "spontaneity_quiet_hours_start": effective.spontaneity_quiet_hours_start,
            "spontaneity_quiet_hours_end": effective.spontaneity_quiet_hours_end,
            "reply_action": reply_action,
            "direct_addressed": False,
            "approval_required": effective.spontaneity_preview == "owner_dm",
            "approval_revision": 0,
            "arbitration_revision": int(
                processing_store.arbitration_revision(channel, chat_id)
                if processing_store is not None
                and hasattr(processing_store, "arbitration_revision")
                else 0
            ),
            "context_window_minutes": int(resolved.context_window_minutes),
            "context_max_messages": int(resolved.context_max_messages),
            "context_revision": context_revision,
            "current_source_ids": current_source_ids,
            "max_reevaluations": int(resolved.max_reevaluations),
            "opportunity_ttl_seconds": int(resolved.opportunity_ttl_seconds),
            "judge_calls_per_hour": int(
                resolved.participation.max_unaddressed_judge_calls_per_hour
            ),
            "min_gap_seconds": int(resolved.participation.min_unaddressed_judge_gap_seconds),
            "continuation_reserve": int(resolved.participation.continuation_judge_reserve),
            "continuation_candidate": _continuation_candidate(opportunity),
            "reaction_limits": (("reaction", reaction_limit, window_ms),)
            if reaction_limit > 0
            else (),
            "comment_limits": (("comment", comment_limit, window_ms),)
            if comment_limit > 0
            else (),
        }

    def _is_paused(channel: str, chat_id: str) -> str | None:
        probe = getattr(policy_adapter, "participation_pause_reason", None)
        if probe is None:
            return None
        return probe(channel, chat_id)

    def _is_source_allowed(channel: str, chat_id: str, sources: object) -> bool:
        del sources
        if policy_engine is None:
            return False
        try:
            resolved = policy_engine.resolve_policy(channel, chat_id)  # type: ignore[attr-defined]
        except Exception:
            return False
        return str(getattr(resolved, "when_to_reply_mode", "")) != "off"

    def _source_principals(
        channel: str, chat_id: str, sources: object
    ) -> tuple[tuple[str, str], ...]:
        if inbound_archive is None or not hasattr(inbound_archive, "senders_for_messages"):
            return ()
        try:
            senders = inbound_archive.senders_for_messages(  # type: ignore[attr-defined]
                channel, chat_id, tuple(str(item) for item in sources)  # type: ignore[arg-type]
            )
        except Exception:
            return ()
        return tuple(
            sorted(
                (str(source_id), str(sender))
                for source_id, sender in senders.items()
                if str(source_id) and str(sender)
            )
        )

    def _is_participant_allowed(channel: str, chat_id: str, sender: str) -> bool:
        return participant_is_allowed(
            engine=policy_engine, channel=channel, chat_id=chat_id, sender=sender
        )

    writer_profile = ""
    try:
        resolved_writer = ModelRouter(config.models).resolve_primary("participation.writer")
        writer_model = str(resolved_writer.model or "").strip()
        if not writer_model:
            raise ValueError("writer model is empty")
        writer_provider = str(resolved_writer.provider or "").strip()
        if not writer_provider:
            raise ValueError("writer provider is empty")
        if config.get_provider(
            writer_model, provider_name=writer_provider
        ) is None:
            raise ValueError("writer provider is unavailable")
        ProviderFactory(config=config).create_chat_provider(
            writer_model, writer_provider
        )
        writer_profile = resolved_writer.profile_name
        logger.info(
            "participation writer ready route={} profile={} provider={} model={}",
            resolved_writer.route_key,
            resolved_writer.profile_name,
            writer_provider,
            writer_model,
        )
    except Exception:  # noqa: BLE001 - unavailable writer removes only comments
        logger.warning(
            "participation writer unavailable route=participation.writer category=writer_unavailable"
        )
    submission = (
        _ParticipationSubmission(
            responder=responder,
            approval_tools=approval_tools,
            writer_profile=writer_profile,
        )
        if writer_profile
        else None
    )
    bind_submission = getattr(
        approval_tools, "set_participation_submission", None
    )
    if callable(bind_submission) and submission is not None:
        bind_submission(submission)
    reactor = (
        _ParticipationReactor(responder=responder)
        if callable(getattr(responder, "react_to_participation", None))
        and bool(getattr(responder, "participation_reaction_available", False))
        else None
    )
    decision_runtime = ParticipationDecisionRuntime(
        judge=judge,
        context_builder=context_builder,
        ledger=log,
        snapshot_provider=_snapshot,
        is_paused=_is_paused,
        is_source_allowed=_is_source_allowed,
        source_principals=_source_principals,
        is_participant_allowed=_is_participant_allowed,
        submission=submission,
        reactor=reactor,
        writer_available=submission is not None,
        direct_work_active=(
            processing_store.direct_work_active
            if processing_store is not None
            and hasattr(processing_store, "direct_work_active")
            else None
        ),
    )

    async def _handle(opportunity: object) -> None:
        await decision_runtime.evaluate_participation(opportunity)  # type: ignore[arg-type]

    scheduler = OpportunityScheduler(
        handle=_handle,
        max_pending_chats=int(getattr(participation, "max_pending_chats", 64)),
        max_concurrent_decisions=int(getattr(participation, "max_concurrent_decisions", 2)),
        max_pending_source_refs=int(getattr(participation, "max_pending_source_refs", 64)),
        max_pending_source_bytes=int(getattr(participation, "max_pending_source_bytes", 16_384)),
        ttl_seconds=int(getattr(participation, "opportunity_ttl_seconds", 120)),
    )
    current_activation = getattr(policy_adapter, "current_activation", None)
    offer_runtime = OpportunityOfferRuntime(
        scheduler=scheduler,
        source_owner=source_owner,  # type: ignore[arg-type]
        activation_epoch=int(log.activation_epoch_sync("participation")),  # type: ignore[attr-defined]
        activation_provider=(current_activation if callable(current_activation) else None),
        is_enabled=lambda channel, chat_id: _is_paused(channel, chat_id) is None,
        considered_revision_provider=getattr(
            log, "highest_considered_revision_sync", None
        ),
        direct_work_active=(
            processing_store.direct_work_active
            if processing_store is not None
            and hasattr(processing_store, "direct_work_active")
            else None
        ),
    )
    logger.info(
        "participation runtime built route={} shadow={} pending_chats={} concurrency={}",
        route_key,
        bool(getattr(participation, "shadow", True)),
        int(getattr(participation, "max_pending_chats", 64)),
        int(getattr(participation, "max_concurrent_decisions", 2)),
    )
    return offer_runtime, scheduler, (decision_runtime, None)


class _ParticipationSubmission:
    """Adapter from a selected comment to the draft-only generator and effect path."""

    def __init__(
        self,
        *,
        responder: object | None,
        writer_profile: str,
        approval_tools: object | None = None,
    ) -> None:
        self._responder = responder
        self._writer_profile = str(writer_profile)
        self._approval_tools = approval_tools

    async def generate_draft(
        self, *, opportunity: object, decision: object, context: object
    ) -> str | None:
        generator = getattr(self._responder, "generate_participation_draft", None)
        if generator is None:
            return None
        event, policy_decision = _participation_event(opportunity, decision)
        return await generator(
            event,
            policy_decision,
            purpose=str(getattr(decision, "purpose", "") or ""),
            context=dict(context or {}),
            model_profile=self._writer_profile,
        )

    async def submit(
        self,
        *,
        admission: object,
        effect_id: str,
        content: str,
        payload_hash: str,
    ) -> object:
        del payload_hash
        submitter = getattr(self._responder, "submit_participation_comment", None)
        if submitter is None:
            return _ParticipationOutcome(status="no_submission_path")
        return await submitter(
            admission=admission, effect_id=effect_id, content=content
        )

    async def queue_approval(
        self,
        *,
        opportunity: object,
        decision: object,
        admission: object,
        effect_id: str,
        content: str,
        snapshot: object,
    ) -> object:
        queue = getattr(
            self._approval_tools, "stage_participation_approval", None
        )
        if not callable(queue):
            return _ParticipationOutcome(status="approval_path_unavailable")
        return await queue(
            opportunity=opportunity,
            decision=decision,
            admission=admission,
            effect_id=effect_id,
            content=content,
            snapshot=snapshot,
        )


class _ParticipationReactor:
    """Adapter from a selected reaction to the existing reaction effect path."""

    def __init__(self, *, responder: object | None) -> None:
        self._responder = responder

    async def __call__(
        self,
        *,
        target_message_id: str,
        emoji: str,
        channel: str,
        chat_id: str,
        effect_id: str,
        admission: object,
    ) -> object | None:
        reactor = getattr(self._responder, "react_to_participation", None)
        if reactor is None:
            return None
        return await reactor(
            target_message_id=target_message_id,
            emoji=emoji,
            channel=channel,
            chat_id=chat_id,
            effect_id=effect_id,
            admission=admission,
        )


@dataclass(frozen=True, slots=True)
class _ParticipationOutcome:
    status: str


def _participation_event(opportunity: object, decision: object) -> tuple[object, object]:
    """Build the synthetic inbound event and policy decision for one draft.

    The event carries the admitted target only: no task, thread or turn identity, and
    its allowed tool set is empty. Direct requests never come through here.
    """
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import InboundEvent, PolicyDecision

    channel = str(getattr(opportunity, "channel", ""))
    chat_id = str(getattr(opportunity, "chat_id", ""))
    event = InboundEvent(
        channel=channel,
        chat_id=chat_id,
        sender_id="",
        content=str(getattr(decision, "purpose", "") or ""),
        timestamp=datetime.now(UTC),
        is_group=str(chat_id).endswith("@g.us"),
        raw_metadata={"participation": True, "opportunity_id": getattr(opportunity, "opportunity_id", "")},
    )
    policy_decision = PolicyDecision(
        accept_message=False,
        should_respond=False,
        allowed_tools=frozenset(),
        reason="participation_draft_only",
        persona_text=None,
    )
    return event, policy_decision


def participant_is_allowed(
    *, engine: object | None, channel: str, chat_id: str, sender: str
) -> bool:
    """Whether one originating participant may be answered in this exact chat.

    A failure here is a *denial*: the check exists so that a permitted service
    principal can never stand in for the authorization of the person whose material
    is being answered. It therefore fails closed, but it also has to be correct - a
    call-signature mistake would otherwise masquerade as "the sender is not allowed"
    and silently disable participation for every chat.
    """
    if engine is None or not str(sender or "").strip():
        return False
    from yeoman_gateway.policy.engine import ActorContext

    try:
        decision = engine.evaluate(  # type: ignore[attr-defined]
            ActorContext(
                channel=str(channel),
                chat_id=str(chat_id),
                sender_primary=str(sender),
                sender_aliases=[str(sender)],
                is_group=str(chat_id).endswith("@g.us"),
                mentioned_bot=False,
                reply_to_bot=False,
            ),
            all_tools=set(),
        )
    except Exception as exc:  # noqa: BLE001 - a broken check must not authorise
        logger.warning(
            "participation participant check failed chat={} error_type={}",
            str(chat_id)[:24],
            type(exc).__name__,
        )
        return False
    return bool(getattr(decision, "accept_message", False))


def _archive_feedback_reader(archive: object | None, reconciler: object | None):
    """Exact quoted-reply/reaction lookup for one delivered provider message."""

    def _lookup(*, channel: str, chat_id: str, message_id: str) -> dict[str, object] | None:
        if archive is None or not message_id:
            return None
        try:
            rows = archive.lookup_messages_in_range(  # type: ignore[attr-defined]
                channel, chat_id, _far_past(), None, limit=50, latest=True
            )
        except Exception:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            quoted = str(row.get("reply_to_message_id") or row.get("quoted_message_id") or "")
            if quoted and quoted == str(message_id):
                return {"kind": "reply", "event_id": str(row.get("message_id") or "")}
        return None

    del reconciler
    return _lookup


def _far_past():
    from datetime import UTC, datetime

    return datetime(2000, 1, 1, tzinfo=UTC)


def _shared_fact_members(chat_registry: object | None):
    """Proven chat participants, or ``None`` when nothing is proven.

    The audience of a fact must come from a recorded participant list - never from the
    model and never from a guess. Unknown membership therefore yields no audience, and
    the fact degrades to ``author_only``.
    """
    if chat_registry is None:
        return None

    def _lookup(channel: str, chat_id: str) -> frozenset[str] | None:
        from yeoman_gateway.knowledge._memory.read_gate import registry_members

        members = registry_members(chat_registry, channel=channel, chat_id=chat_id)
        return frozenset(members) if members else None

    return _lookup


def build_reconciliation_service(
    config: "Config",
    store: "ProcessingStore | None",
    *,
    probe: object | None = None,
    project_participation_receipts: Callable[[], Awaitable[object]] | None = None,
):
    """Reconciler for the new mode; ``None`` while processing is off or no store is open.

    Disabled mode stays inert: no store, no database and no background task.
    """
    if store is None and project_participation_receipts is None:
        return None

    from yeoman_gateway.processing.reconcile import LocalEvidenceProbe, ReconciliationService

    reconciliation = config.processing.reconciliation
    evidence = probe
    if evidence is None and store is not None:
        evidence = LocalEvidenceProbe(
            store,
            provider_lookup_enabled=bool(reconciliation.provider_lookup_enabled),
        )
    maintenance = config.processing.participation_maintenance
    return ReconciliationService(
        store,
        probe=evidence,
        config=reconciliation,
        project_participation_receipts=project_participation_receipts,
        projection_interval_seconds=int(maintenance.interval_seconds),
    )


def _build_participation_receipt_projection(
    config: "Config",
    *,
    log: object | None,
    store: "ProcessingStore | None",
    inbound_archive: object,
) -> tuple[object | None, Callable[[], Awaitable[object]] | None]:
    if log is None:
        return None, None
    from yeoman_gateway.consciousness.delivery import ParticipationReceiptReconciler

    reconciler = ParticipationReceiptReconciler(
        log=log,  # type: ignore[arg-type]
        store=store,
        unsubmitted_ttl_ms=int(
            config.processing.participation.opportunity_ttl_seconds
        )
        * 1000,
    )
    batch_size = int(config.processing.participation_maintenance.batch_size)

    async def _project() -> object:
        return await reconciler.reconcile(limit=batch_size)

    feedback = getattr(log, "set_explicit_feedback_reader", None)
    if callable(feedback):
        feedback(_archive_feedback_reader(inbound_archive, reconciler))
    return reconciler, _project


def build_retention_service(
    config: "Config",
    store: "ProcessingStore | None",
):
    """Retention schedule for the new mode; ``None`` while processing is off.

    Disabled mode stays inert: no store, no database and no background task.
    """
    if store is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.retention import ProcessingRetentionService

    return ProcessingRetentionService(store)


def build_thread_registry(config: "Config", store: "ProcessingStore | None"):
    """Thread registry for the new mode; ``None`` while processing is off."""
    if store is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.threads import ThreadRegistry

    return ThreadRegistry(store=store, config=config.processing)


def build_processing_gate(
    config: "Config",
    policy_adapter: "EnginePolicyAdapter | None",
    store: "ProcessingStore | None",
    threads: object | None = None,
):
    """Fast gate for canonical ingest -> journal -> policy, before expensive work.

    Returns ``None`` when the new mode is off, so the legacy path is untouched.
    """
    if store is None or policy_adapter is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.policy import AdapterSnapshotProvider, IngestGate

    def _participation_owns(channel: str, chat_id: str) -> bool:
        """Whether the participation lane is this chat's production owner.

        Only a fully valid activation counts: the global switch on, the chat explicitly
        opted in, and a resolvable participant-safe policy. Anything else leaves the
        legacy ambient path in charge, which is the safe direction.
        """
        participation = getattr(config.processing, "participation", None)
        if participation is None or not bool(getattr(participation, "enabled", False)):
            return False
        try:
            resolved = policy_adapter.participation_policy(channel, chat_id)
        except Exception:  # noqa: BLE001 - an unreadable policy keeps legacy ownership
            return False
        return bool(resolved)

    return IngestGate(
        config=config.processing,
        store=store,
        snapshots=AdapterSnapshotProvider(policy_adapter),
        evaluate=lambda request: policy_adapter.evaluate(request.event),
        threads=threads if threads is not None else build_thread_registry(config, store),
        participation=_participation_owns,
    )


def build_thread_responder(
    config: "Config",
    store: "ProcessingStore | None",
    threads: object | None,
    responder: object,
    policy_adapter: "EnginePolicyAdapter | None" = None,
    router: object | None = None,
    release_participation_chat: Callable[[str, str], None] | None = None,
):
    """Responder wrapper that drives the thread actor. ``None`` keeps the legacy path."""
    if store is None or threads is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.policy import operator_check
    from yeoman_gateway.processing.responder import ThreadActorResponder
    from yeoman_gateway.processing.threads import TurnAuthority

    actor_registry = ThreadActorRegistry(
        store=store,
        config=config.processing,
        authority=TurnAuthority(
            is_operator=operator_check(
                (lambda: policy_adapter.policy_engine()) if policy_adapter is not None else (lambda: None)
            )
        ),
    )
    return ThreadActorResponder(
        inner=responder,
        actors=actor_registry,
        store=store,
        router=router,
        finish_direct_admission=store.finish_direct_admission,
        direct_work_active=store.direct_work_active,
        release_chat=release_participation_chat,
    )


def build_effect_router(
    config: "Config",
    policy_adapter: "EnginePolicyAdapter | None",
    store: "ProcessingStore | None",
    bus: MessageBus,
    security: object | None = None,
    threads: object | None = None,
    participation_ledger: object | None = None,
    inbound_archive: object | None = None,
):
    """Effect gateway plus transport executor for the new mode.

    Returns ``None`` while the new mode is disabled, so every producer keeps its legacy
    path and no second sender exists for the same turn.
    """
    if store is None or policy_adapter is None or not config.processing.enabled:
        return None

    from yeoman_gateway.consciousness.log import DELIVERY_RESERVATION_TTL_MS
    from yeoman_gateway.processing.dispatch import BusEffectExecutor, IntentEffectRouter
    from yeoman_gateway.processing.effects import EffectGateway
    from yeoman_gateway.processing.participation_runtime import (
        ParticipationAuthorizationRequest,
        ParticipationEffectAuthorizer,
    )
    from yeoman_gateway.processing.policy import (
        AdapterSnapshotProvider,
        PolicyCapabilityResolver,
        SnapshotEffectAuthorizer,
    )

    snapshots = AdapterSnapshotProvider(policy_adapter)
    participation_checker = ParticipationEffectAuthorizer()

    def _participation_request(envelope: object, admission: object):
        current = policy_adapter.current_activation(
            str(getattr(getattr(envelope, "target", None), "channel", "")),
            str(getattr(getattr(envelope, "target", None), "chat_id", "")),
        )
        channel = str(getattr(admission, "channel", "") or "")
        chat_id = str(getattr(admission, "chat_id", "") or "")
        effect_id = str(getattr(envelope, "effect_id", "") or "")
        row_reader = getattr(participation_ledger, "_delivery_row_for_effect", None)
        reservation = row_reader(effect_id) if callable(row_reader) else None
        reservation_matches = bool(
            reservation is not None
            and str(reservation.get("proposal_id") or "")
            == str(getattr(admission, "opportunity_id", "") or "")
        )

        source_ids = tuple(str(item) for item in getattr(admission, "source_event_ids", ()))
        pairs = getattr(admission, "source_principals", ())
        expected_senders = {
            str(item[0]): str(item[1])
            for item in pairs
            if isinstance(item, (tuple, list)) and len(item) == 2
        }
        current_senders: dict[str, str] = {}
        if inbound_archive is not None and hasattr(inbound_archive, "senders_for_messages"):
            try:
                current_senders = {
                    str(key): str(value)
                    for key, value in inbound_archive.senders_for_messages(
                        channel, chat_id, source_ids
                    ).items()
                }
            except Exception:
                current_senders = {}
        exact_sources = bool(
            source_ids
            and len(expected_senders) == len(source_ids)
            and set(expected_senders) == set(source_ids)
            and current_senders == expected_senders
        )

        engine = policy_adapter.policy_engine()
        try:
            effective = engine.resolve_policy(channel, chat_id) if engine is not None else None
        except Exception:
            effective = None
        principals_allowed = bool(
            exact_sources
            and engine is not None
            and all(
                participant_is_allowed(
                    engine=engine,
                    channel=channel,
                    chat_id=chat_id,
                    sender=sender,
                )
                for sender in expected_senders.values()
            )
        )
        current_policy = policy_adapter.policy_snapshot()
        policy_matches = bool(
            current is not None
            and str(getattr(admission, "policy_version", "") or "")
            == str(getattr(current, "policy_version", "") or "")
            and str(getattr(admission, "policy_hash", "") or "")
            == str(getattr(current_policy, "policy_hash", "") or "")
        )
        arbitration_matches = bool(
            hasattr(store, "arbitration_revision")
            and int(store.arbitration_revision(channel, chat_id))
            == int(getattr(admission, "arbitration_revision", -1))
        )

        action = str(getattr(admission, "action", "") or "")
        intent = str(getattr(admission, "intent", "") or "")
        contribution = str(getattr(admission, "contribution_type", "") or "")
        reply_actions = config.processing.reply_actions or {}
        reply_action = str(
            reply_actions.get(f"{channel}:{chat_id}", "answer")
            if isinstance(reply_actions, Mapping)
            else "answer"
        ).strip().lower()
        current_rights = False
        if current is not None and effective is not None:
            participation = current.participation
            current_daily_cap = (
                effective.spontaneity_daily_cap
                if effective.spontaneity_daily_cap is not None
                else int(config.consciousness.default_daily_cap)
            )
            if effective.spontaneity_allowed_actions is not None:
                permitted_contributions = set(effective.spontaneity_allowed_actions)
            else:
                from yeoman_gateway.consciousness.tools import (
                    DEFAULT_BALANCED_ACTIONS,
                    DEFAULT_HELPFUL_ACTIONS,
                    DEFAULT_PERMISSIVE_ACTIONS,
                )

                permitted_contributions = set(
                    DEFAULT_PERMISSIVE_ACTIONS
                    if effective.spontaneity_profile == "permissive"
                    else DEFAULT_BALANCED_ACTIONS
                    if effective.spontaneity_profile == "balanced"
                    else DEFAULT_HELPFUL_ACTIONS
                )
            quiet = False
            quiet_start = effective.spontaneity_quiet_hours_start
            quiet_end = effective.spontaneity_quiet_hours_end
            if quiet_start and quiet_end:
                try:
                    start_hour, start_minute = (int(part) for part in quiet_start.split(":"))
                    end_hour, end_minute = (int(part) for part in quiet_end.split(":"))
                    start = start_hour * 60 + start_minute
                    end = end_hour * 60 + end_minute
                    now = datetime.now(UTC)
                    minute = now.hour * 60 + now.minute
                    quiet = (
                        True
                        if start == end
                        else start <= minute < end
                        if start < end
                        else minute >= start or minute < end
                    )
                except (TypeError, ValueError):
                    quiet = True
            current_rights = bool(
                (
                    action == "react"
                    and reply_action in {"answer", "react"}
                    and participation.allow_reactions
                )
                or (
                    action == "comment"
                    and reply_action == "answer"
                    and (
                        (
                            intent == "continue"
                            and participation.allow_continuation
                            and contribution in permitted_contributions
                        )
                        or (
                            intent == "initiate"
                            and participation.allow_initiation
                            and effective.spontaneity_enabled
                            and int(current_daily_cap or 0) > 0
                            and contribution in permitted_contributions
                            and not quiet
                            and (
                                effective.spontaneity_preview != "owner_dm"
                                or int(getattr(admission, "approval_revision", 0) or 0) > 0
                            )
                        )
                    )
                )
            )
        stale = True
        material_reader = getattr(participation_ledger, "material_for_opportunity", None)
        if callable(material_reader) and current is not None:
            try:
                pending, revision = material_reader(
                    channel,
                    chat_id,
                    None,
                    lane=str(getattr(current, "lane", "production")),
                )
                stale = bool(
                    pending
                    and int(revision) > int(getattr(admission, "observed_revision", 0) or 0)
                )
            except Exception:
                stale = True

        return ParticipationAuthorizationRequest(
            admission=admission,
            lane=str(getattr(current, "lane", "shadow") if current is not None else "shadow"),
            is_paused=policy_adapter.participation_pause_reason(channel, chat_id),
            is_shadow=bool(getattr(current, "shadow", True)),
            feature_enabled=bool(
                getattr(current, "enabled", False)
                and getattr(current, "valid", False)
            ),
            opted_in=bool(getattr(current, "opted_in", False)),
            current_epoch=int(getattr(current, "activation_epoch", -1)),
            source_authorized=bool(
                exact_sources
                and policy_matches
                and arbitration_matches
                and reservation_matches
                and current_rights
                and not stale
                and effective is not None
                and str(effective.when_to_reply_mode) != "off"
            ),
            source_principals_authorized=principals_allowed,
            effect_id=effect_id,
            reservation_state=(
                str(reservation.get("attempt_state") or "") if reservation is not None else None
            ),
            payload_hash=str(getattr(envelope, "payload_hash", "") or ""),
            expected_payload_hash=str(getattr(admission, "payload_hash", "") or ""),
        )

    def _reservation_pre_dispatch(envelope: object) -> tuple[bool, str]:
        row_reader = getattr(participation_ledger, "_delivery_row_for_effect", None)
        reservation = (
            row_reader(str(getattr(envelope, "effect_id", "")))
            if callable(row_reader)
            else None
        )
        if reservation is not None and int(time.time() * 1000) >= (
            int(reservation["created_at_ms"]) + DELIVERY_RESERVATION_TTL_MS
        ):
            return False, "reservation_expired"
        return True, "allow"

    def _participation_pre_dispatch(envelope: object) -> tuple[bool, str]:
        admission_id = str(getattr(envelope, "admission_id", "") or "")
        admission = store.get_participation_admission(admission_id) if admission_id else None
        if admission is None:
            return False, "participation_admission_missing"
        try:
            return participation_checker.check(_participation_request(envelope, admission))
        except Exception as exc:
            return False, f"participation_check_failed:{type(exc).__name__}"

    authorizer = SnapshotEffectAuthorizer(
        snapshots=snapshots,
        capabilities=PolicyCapabilityResolver(
            engine_provider=policy_adapter.policy_engine,
            known_tools=lambda: set(policy_adapter.known_tools),
        ),
        turn_lookup=getattr(threads, "turn_lookup", None),
        admission_loader=store.get_participation_admission,
        participation_authorizer=participation_checker,
        participation_request_builder=_participation_request,
    )
    executor = BusEffectExecutor(
        bus=bus,
        mark_provenance=True,
        security=security,
        security_block_message=config.security.block_user_message,
        participation_pre_dispatch=_participation_pre_dispatch,
        reservation_pre_dispatch=_reservation_pre_dispatch,
    )
    gateway = EffectGateway(
        store,
        authorizer=authorizer,
        executor=executor,
    )
    return IntentEffectRouter(
        gateway=gateway,
        config=config,
        turn_provider=getattr(threads, "active_turn", None),
        participation_ledger=participation_ledger,
    )


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

    consciousness_data_dir = get_operational_data_path() / "consciousness"
    speakup_path = consciousness_data_dir / "speakups.db"
    processing_path = _processing_store_path(config)
    pending_participation_recovery = _has_pending_participation_recovery(
        speakup_path=speakup_path, processing_path=processing_path
    )
    processing_store = build_processing_store(
        config,
        recover_pending=(pending_participation_recovery and processing_path.is_file()),
    )
    participation_enabled = bool(
        config.processing.enabled
        and getattr(getattr(config.processing, "participation", None), "enabled", False)
    )
    social_runtime_enabled = policy_engine is not None and participation_enabled
    speakup_log = None
    speakup_approval_store = None
    activation_tracker = None
    if (
        social_runtime_enabled
        or config.persona_evolution.enabled
        or (pending_participation_recovery and speakup_path.is_file())
    ):
        from yeoman_gateway.consciousness.log import SpeakupLog
        from yeoman_gateway.consciousness.participation_runtime import (
            ActivationEpochTracker,
        )

        speakup_log = SpeakupLog(speakup_path)
        if participation_enabled:
            activation_tracker = ActivationEpochTracker(store=speakup_log)
        if participation_enabled:
            from yeoman_gateway.consciousness.approval import SpeakupApprovalStore

            speakup_approval_store = SpeakupApprovalStore(
                consciousness_data_dir / "pending_approvals.json"
            )

    session_manager = SessionManager(workspace)
    # The owner wants a complete inbound record: keep every message, purge nothing.
    inbound_archive = InboundArchive(
        db_path=get_operational_data_path() / "inbound" / "reply_context.db",
        retention_days=None,
    )
    model_router = ModelRouter(config.models)
    media_storage = MediaStorage(
        incoming_dir=config.channels.whatsapp.media.incoming_path,
        outgoing_dir=config.channels.whatsapp.media.outgoing_path,
    )
    provider_factory = ProviderFactory(config=config)
    document_cache = DocumentCache(get_operational_data_path() / "media" / "document_cache.db")
    lazy_vision = (
        VisionDescriber(provider_factory)
        if config.channels.whatsapp.media.ocr_images
        else None
    )
    document_processor = DocumentProcessor(
        cache=document_cache,
        model_router=model_router,
        vision_describer=lazy_vision,
        max_document_bytes=config.channels.whatsapp.media.max_document_bytes_mb * 1024 * 1024,
        max_image_bytes=config.channels.whatsapp.media.max_image_bytes_mb * 1024 * 1024,
        max_pdf_pages=config.channels.whatsapp.media.max_document_text_pages,
        max_prompt_chars=config.channels.whatsapp.media.max_document_prompt_chars,
    )
    lazy_media_resolver = LazyMediaResolver(
        cache=document_cache,
        processor=document_processor,
        max_prompt_chars=config.channels.whatsapp.media.max_document_prompt_chars,
    )

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

    from yeoman_gateway.storage.chat_registry import ChatRegistry

    chat_registry = ChatRegistry(
        db_path=get_operational_data_path() / "inbound" / "chat_registry.db",
    )

    # ── knowledge storage ownership ──────────────────────────────────────────
    # With knowledge.enabled the consolidated store is the *only* writer: the memory
    # and contacts services join its one connection instead of opening their own.
    # Without it the legacy layout is untouched (no partial cutover, no second writer).
    knowledge_service: object | None = None
    knowledge_sources: object | None = None
    if getattr(config.knowledge, "enabled", False):
        from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
        from yeoman_gateway.knowledge.runtime import (
            RuntimeKnowledgePolicy,
            RuntimeKnowledgeSources,
        )

        knowledge_policy = RuntimeKnowledgePolicy(
            engine=policy_engine.policy if policy_engine else None,
            chat_registry=chat_registry,
            admin_principals=frozenset(
                getattr(policy_engine.policy, "admin_principals", frozenset())
                if policy_engine
                else frozenset()
            ),
            capture_actors=frozenset(),
        )
        knowledge_sources = RuntimeKnowledgeSources()
        try:
            knowledge_service = open_knowledge_store(
                Path(config.knowledge.db_path).expanduser(),
                workspace_id=workspace_id_for(workspace),
                source_authority=knowledge_sources,
                policy_authority=knowledge_policy,
                legacy_sources=tuple(
                    Path(item).expanduser()
                    for item in (
                        list(config.knowledge.legacy_memory_paths)
                        + list(config.knowledge.legacy_contacts_paths)
                    )
                ),
            )
        except Exception as exc:
            # Fail closed: never silently fall back to a second, unprotected store.
            logger.error("person knowledge unavailable: {}", exc)
            raise

    if knowledge_service is not None:
        memory_service = MemoryService(
            workspace=workspace,
            config=config.memory,
            root_config=config,
            store=knowledge_service.memory_store(),
            owns_store=False,
        )
        contacts_service = ContactsService(store=knowledge_service.contacts_store())
        contacts_service.mark_owner_from_policy(
            policy_engine.policy.owners if policy_engine else {},
        )
        memory_service.set_contacts(contacts_service)
    else:
        memory_service = MemoryService(
            workspace=workspace, config=config.memory, root_config=config
        )
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

    try:
        imported = memory_service.backfill_from_workspace_files(force=False)
        if imported > 0:
            logger.info("memory backfill imported {} entries", imported)
    except Exception as e:
        logger.warning("memory backfill failed: {}", e)

    cron_store_path = get_operational_data_path() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, sessions_dir=get_operational_data_path() / "inbound")
    private_handoffs = PrivateHandoffStore(get_operational_data_path() / "policy" / "private_handoffs.json")

    # Create policy adapter first so we can use it for owner_alert_resolver
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=set(),  # Will be updated after responder is created
        policy_path=policy_path,
        session_manager=session_manager,
        processing_store=processing_store,
        private_handoff_store=private_handoffs,
        workspace=workspace,
        processing_config=config.processing,
        models_config=config.models,
        activation_tracker=activation_tracker,
    )
    if activation_tracker is not None:
        activation_tracker.refresh_activation_sync()
    from yeoman_gateway.processing.quota import CapabilityQuotaGovernance

    quota_governance = CapabilityQuotaGovernance(
        store=processing_store,
        policy_provider=policy_adapter,
    )
    file_access_resolver = build_file_access_resolver(
        workspace=workspace,
        policy=policy_engine.policy if policy_engine is not None else None,
    )
    a2a_registry = A2AWorkerRegistry.from_config(config.tools.a2a)
    if a2a_registry is not None:
        logger.info("A2A worker tools enabled: {}", ", ".join(a2a_registry.names))

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

    thread_registry = build_thread_registry(config, processing_store)
    processing_gate = build_processing_gate(
        config, policy_adapter, processing_store, thread_registry
    )
    effect_router = build_effect_router(
        config,
        policy_adapter,
        processing_store,
        bus,
        security=security,
        threads=thread_registry,
        participation_ledger=speakup_log,
        inbound_archive=inbound_archive,
    )
    if effect_router is not None:
        from yeoman_gateway.processing.dispatch import managed_outbound_guard

        bus.set_managed_outbound_guard(managed_outbound_guard(effect_router))

    # The reaction vocabulary is an owner decision, so the effective list is named once at
    # startup: an emoji missing from it is silently unsendable, and that must be visible.
    logger.info(
        "reaction_emojis count={} allowed={}",
        len(config.processing.reaction_emojis),
        " ".join(config.processing.reaction_emojis) or "-",
    )
    service_effects = (
        ServiceEffectProducer(router=effect_router, bus=bus)
        if effect_router is not None
        else None
    )
    consciousness_tools = None
    if social_runtime_enabled and speakup_log is not None:
        from yeoman_gateway.consciousness.tools import ConsciousnessTools

        assert policy_engine is not None
        consciousness_tools = ConsciousnessTools(
            config=config,
            policy_engine=policy_engine,
            bus=bus,
            log=speakup_log,
            inbound_archive=inbound_archive,
            memory=memory_service,
            security=security,
            approval_store=speakup_approval_store,
            service_effects=service_effects,
            activation_provider=getattr(policy_adapter, "current_activation", None),
        )

    participation_receipt_reconciler, project_participation_receipts = (
        _build_participation_receipt_projection(
            config,
            log=speakup_log,
            store=processing_store,
            inbound_archive=inbound_archive,
        )
    )

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
        effect_router=effect_router,
        a2a_delivery=service_effects,
        processing_store=processing_store,
        memory_service=memory_service,
        knowledge=knowledge_service,
        knowledge_sources=knowledge_sources,
        telemetry=telemetry,
        security=security,
        cron_service=cron,
        contacts_service=contacts_service,
        chat_registry=chat_registry,
        caldav_service=_caldav_service,
        owner_alert_resolver=policy_adapter.owner_recipients,
        file_access_resolver=file_access_resolver,
        group_resolver=policy_adapter.resolve_whatsapp_group,
        model_router=model_router,
        routed_provider_factory=provider_factory.create_chat_provider,
        service_effects=service_effects,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        inbound_archive=inbound_archive,
        private_handoff_store=private_handoffs,
        a2a_registry=a2a_registry,
        quota_governance=quota_governance,
        lazy_media_resolver=lazy_media_resolver,
        whatsapp_session_history_limit=config.channels.whatsapp.session_history_limit,
        whatsapp_session_history_limit_group=config.channels.whatsapp.session_history_limit_group,
    )
    if policy_engine is not None:
        policy_engine.validate(policy_validation_tools(set(responder.tool_names)))

    if effect_router is not None:
        # The new mode must not keep uncontained write capabilities as a bypass.
        disable_non_migrated_tools(responder.tools)

    # Wire /voice command callback: reuses the send_voice tool.
    async def _voice_send_callback(
        content: str,
        chat_id: str,
        source_chat_id: str,
        principal: str,
    ) -> str:
        return await responder.execute_delivery(
            tool_name="send_voice",
            channel="whatsapp",
            chat_id=chat_id,
            text=content,
            session_key=f"whatsapp:{source_chat_id}",
            principal=principal,
            is_owner=True,
            voice="71c095ed4c03459fb98500db63b88fbe",
        )

    policy_adapter.set_voice_send_callback(_voice_send_callback)

    # Wire admin notify callback: sends text to a given channel+chat.
    async def _admin_notify(channel: str, chat_id: str, text: str) -> None:
        if service_effects is not None:
            await service_effects.send(
                source="admin",
                operation_ref=f"admin:{channel}:{chat_id}:{canonical_hash(text)[:12]}",
                channel=channel,
                chat_id=chat_id,
                content=text,
                capability="send_text",
            )
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=text))

    policy_adapter.set_admin_notify_callback(_admin_notify)

    # Update policy adapter with actual tool names
    policy_adapter._known_tools = policy_validation_tools(set(responder.tool_names))
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
        document_cache=document_cache,
        processing_gate=processing_gate,
        processing_signals=(
            SignalJournalSink(
                processing_store,
                invalidator=(
                    SignalInvalidator(
                        store=processing_store,
                        actors=thread_registry,
                        memory=memory_service,
                    )
                    if processing_store is not None and config.processing.enabled
                    else None
                ),
            )
            if processing_store is not None
            else None
        ),
        reaction_action=_build_reaction_action(config, effect_router, processing_store),
        ambient_judge=_build_ambient_judge(config),
    )

    typing_adapter = ChannelManagerTypingAdapter(channels)

    if effect_router is not None:
        # Approved effects go straight to the channel transport, so a successful send is
        # a real receipt instead of a queue guess (spec R07).
        effect_router.set_direct_transport(channels.send_now, channels.send_reaction_now)

    # Wire recording indicator: responder switches presence to mic icon during TTS
    async def _recording_notifier(channel: str, chat_id: str) -> None:
        await channels.set_recording(channel, chat_id)

    responder._recording_notifier = _recording_notifier

    from yeoman_gateway.cron.workflow_chain import build_chained_prompt, is_chain_failure
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState

    workflow_state = WorkflowState(
        store_path=Path(workspace) / "data" / "cron" / "pending_approvals.json"
    )
    persona_evolution_state_db_path = Path(workspace) / "persona-evolution" / "persona-evolution.db"

    async def _on_approval_expired(approval: PendingApproval) -> None:
        content = (
            f"Workflow approval expired: {approval.approval_id}. "
            "Use /cron workflow_list to review."
        )
        if service_effects is not None:
            await service_effects.send(
                source="cron",
                operation_ref=f"approval-expired:{approval.approval_id}",
                channel=approval.channel,
                chat_id=approval.chat_id,
                content=content,
            )
            return
        await bus.publish_outbound(OutboundMessage(
            channel=approval.channel,
            chat_id=approval.chat_id,
            content=content,
        ))

    cron._workflow_state = workflow_state
    cron._on_approval_expired = _on_approval_expired

    async def _handle_approved_job(approval: PendingApproval) -> None:
        from uuid import uuid4
        next_job = cron.get_job(approval.next_job_id)
        if not next_job:
            logger.warning("Approved job {} not found", approval.next_job_id)
            return
        run_id = uuid4().hex[:8]
        prompt = build_chained_prompt(
            approval.previous_output, next_job.payload.message,
            input_from_previous=next_job.payload.input_from_previous,
        )
        next_job.payload.max_chain_depth = approval.remaining_depth
        response = await responder.process_direct(
            prompt, session_key=f"cron:{next_job.id}:{run_id}",
            channel=next_job.payload.channel or "cli",
            chat_id=next_job.payload.to or "direct",
            model_profile=next_job.payload.model_profile,
        )
        if next_job.payload.deliver and next_job.payload.to:
            delivery_channel = next_job.payload.channel or "cli"
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"cron:{next_job.id}:{run_id}",
                    channel=delivery_channel,
                    chat_id=next_job.payload.to,
                    content=response or "",
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=delivery_channel,
                    chat_id=next_job.payload.to, content=response or "",
                ))
        if next_job.payload.next_job_id and response and not is_chain_failure(response):
            await _handle_chain(next_job, response, run_id)

    archive_adapter = SqliteReplyArchiveAdapter(inbound_archive)
    opportunity_scheduler: object | None = None

    def _release_participation_chat(channel: str, chat_id: str) -> None:
        release = getattr(opportunity_scheduler, "release_chat", None)
        if callable(release):
            release(channel, chat_id)

    thread_responder = build_thread_responder(
        config,
        processing_store,
        thread_registry,
        responder,
        policy_adapter,
        effect_router,
        _release_participation_chat,
    )

    from yeoman_gateway.agent.tools.a2a_research import A2AResearchStore, sibling_path

    research_path = sibling_path(processing_store)
    research_reports = A2AResearchStore(research_path) if research_path else None

    def _lookup_quoted_report(event: InboundEvent) -> str | None:
        if research_reports is None:
            return None
        requested_length = requested_report_length(event.content)
        if requested_length and event.reply_to_bot and event.reply_to_message_id:
            return research_reports.report_for_quote(
                processing_store,
                channel=event.channel,
                chat_id=event.chat_id,
                provider_message_id=event.reply_to_message_id,
                quoted_text=event.reply_to_text or "",
                length=requested_length,
            )
        from yeoman_gateway.agent.tools.a2a import _ticker
        from yeoman_gateway.policy.identity import canonical_user_id

        symbol = _ticker(event.content)
        user_id = canonical_user_id(event.channel, event.sender_id, event.raw_metadata)
        card = research_reports.cached_card(user_id, symbol)
        if card:
            return f"Gespeicherte TradingGuru-Analyse für {symbol} (maximal 24 Stunden alt; keine neue Marktabfrage):\n\n{card}"
        if research_reports.has_recent_report(user_id, symbol):
            return f"Eine Analyse zu {symbol} liegt bereits vor, aber kein eindeutiges Buy/Hold/Sell-Signal. Bitte frage im ursprünglichen Chat nach der Langfassung; ich starte hier keinen zweiten Auftrag."
        return None

    orchestrator = Orchestrator(
        policy=policy_adapter,
        responder=thread_responder or responder,
        reply_archive=archive_adapter,
        contacts=contacts_service,
        knowledge=knowledge_service,
        reply_context_window_limit=config.channels.whatsapp.reply_context_window_limit,
        reply_context_line_max_chars=config.channels.whatsapp.reply_context_line_max_chars,
        ambient_window_limit=config.channels.whatsapp.ambient_window_limit,
        typing_notifier=typing_adapter,
        reply_admission=(
            getattr(processing_gate, "admit_reply", None)
            if processing_gate is not None
            else None
        ),
        report_lookup=_lookup_quoted_report,
        security=security,
        security_classifier=security_classifier,
        security_block_message=config.security.block_user_message,
        policy_admin_handler=admin_command_handler,
        model_router=model_router,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        owner_alert_resolver=policy_adapter.owner_recipients,
        allowed_reaction_emojis=config.processing.reaction_emojis,
        workflow_state=workflow_state,
        approval_trigger=_handle_approved_job,
        bus=bus,
        speakup_approval_store=speakup_approval_store,
        speakup_log=speakup_log,
        speakup_tools=consciousness_tools,
        persona_evolution_workspace=Path(workspace),
        persona_evolution_state_db_path=persona_evolution_state_db_path,
        session_manager=session_manager,
        service_effects=service_effects,
    )

    def _choose_voice_phrase(job: CronJob, phrases: list[str]) -> str:
        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        available = [phrase for phrase in phrases if phrase not in set(recent)]
        pool = available or phrases
        return random.choice(pool) if job.payload.voice_random else pool[0]

    def _remember_voice_phrase(job: CronJob, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        recent = [value for value in recent if value != text]
        recent.append(text)
        job.payload.voice_recent_messages = recent[-8:]

    def _clean_generated_voice(text: str) -> str:
        cleaned = " ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
        cleaned = cleaned.strip().strip('"').strip("'").strip()
        for prefix in ("Text:", "Voice:", "Nachricht:", "Sprachnachricht:"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix):].strip()
        if len(cleaned) > 360:
            return ""
        return cleaned

    async def _generate_voice_phrase(
        job: CronJob,
        *,
        channel: str,
        chat_id: str,
        fallback_phrases: list[str],
    ) -> str:
        fallback = _choose_voice_phrase(job, fallback_phrases)
        if not job.payload.voice_generate:
            return fallback

        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        recent_block = "\n".join(f"- {value}" for value in recent[-8:]) or "- none"
        default_prompt = (
            "Write one fresh German WhatsApp voice-note line for Arvid in Finanzgruppe. "
            "It is a quiet weekly morning ritual, not a reply to the current chat. "
            "Make it lightly funny, concise, and natural. Max two short sentences. "
            "Do not ask a question. Do not use a visible label. Return only the line."
        )
        prompt = (
            f"{job.payload.voice_prompt or default_prompt}\n\n"
            "Avoid repeating or closely paraphrasing these previous weekly voice lines:\n"
            f"{recent_block}"
        )
        persona_text = None
        model_profile = job.payload.model_profile
        if policy_engine is not None:
            resolved = policy_engine.resolve_policy(channel, chat_id)
            persona_text = load_persona_text(resolved.persona_file, Path(workspace))
            model_profile = model_profile or resolved.model_profile

        try:
            generated = await responder.process_direct(
                prompt,
                session_key=f"cron:{job.id}:voice-generate:{int(time.time())}",
                channel=channel,
                chat_id=chat_id,
                allowed_tools=set(),
                persona_text=persona_text,
                is_owner=True,
                model_profile=model_profile,
            )
        except Exception as exc:
            logger.warning("Cron voice generation failed for {}: {}", job.id, exc)
            return fallback

        cleaned = _clean_generated_voice(generated)
        if not cleaned or cleaned in set(recent):
            return fallback
        return cleaned

    async def _notify_persona_evolution_review(
        proposal: dict[str, object],
        *,
        approval_channel: str | None = None,
    ) -> None:
        if proposal.get("notified_at"):
            return
        channel = str(approval_channel or "telegram").strip() or "telegram"
        raw_targets = policy_adapter.owner_recipients(channel)
        if not raw_targets:
            logger.warning(
                "Persona evolution proposal {} has no owner recipients for {}",
                proposal.get("proposal_id"),
                channel,
            )
            return
        from yeoman_gateway.pipeline.new_chat import _normalize_owner_target

        targets = sorted(
            {
                target
                for raw in raw_targets
                if (target := _normalize_owner_target(channel, str(raw)))
            }
        )
        if not targets:
            logger.warning(
                "Persona evolution proposal {} had no valid owner targets for {}",
                proposal.get("proposal_id"),
                channel,
            )
            return
        message = build_persona_evolution_approval_message(proposal)
        for target in targets:
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=(
                        f"persona-evolution:{proposal.get('proposal_id')}:{target}"
                    ),
                    channel=channel,
                    chat_id=target,
                    content=message,
                )
                continue
            await bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=target, content=message)
            )
        ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
        try:
            ledger.mark_notified(
                str(proposal["proposal_id"]),
                channel=channel,
                chat_id=",".join(targets),
            )
        finally:
            ledger.close()

    async def _notify_pending_persona_evolution_reviews() -> None:
        ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
        try:
            proposals = ledger.pending_proposals()
        finally:
            ledger.close()
        for proposal in proposals:
            await _notify_persona_evolution_review(proposal)

    async def on_cron_job(job: CronJob) -> str | None:
        if job.payload.kind == "persona_evolution":
            if not config.persona_evolution.enabled:
                return "persona_evolution no proposal: disabled"
            persona_file = str(job.payload.persona_file or "").strip()
            if not persona_file:
                raise ValueError("persona_evolution job requires personaFile")
            allowlist = {
                str(item).strip()
                for item in config.persona_evolution.personas_allowlist
                if str(item).strip()
            }
            if allowlist and persona_file not in allowlist:
                return f"persona_evolution no proposal: persona_not_allowed persona_file={persona_file}"
            output_path = None
            if str(job.payload.persona_output or "").strip():
                output_path = Path(str(job.payload.persona_output).strip())
            result = await run_persona_evolution_cron(
                policy=policy_engine.policy,
                workspace=Path(workspace),
                persona_file=persona_file,
                memory=memory_service,
                speakup_log=speakup_log,
                inbound_archive=inbound_archive,
                window_days=max(1, int(job.payload.persona_window_days)),
                limit=max(1, int(job.payload.persona_limit)),
                output_path=output_path,
                min_meaningful_messages=max(
                    0, int(job.payload.persona_min_meaningful_messages)
                ),
                min_signal_score=max(0.0, float(job.payload.persona_min_signal_score)),
                max_accumulation_days=max(1, int(job.payload.persona_max_accumulation_days)),
                proposal_ttl_seconds=max(60, int(config.persona_evolution.proposal_ttl_seconds)),
                proposal_mode=config.persona_evolution.mode,
            )
            if persona_evolution_result_needs_notification(result):
                ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
                try:
                    proposal = ledger.pending_proposal(persona_file)
                finally:
                    ledger.close()
                if proposal is not None:
                    await _notify_persona_evolution_review(
                        proposal,
                        approval_channel=job.payload.approval_channel,
                    )
            return result

        if job.payload.kind == "voice_broadcast":
            phrases = [str(v).strip() for v in list(job.payload.voice_messages) if str(v).strip()]
            if not phrases and str(job.payload.message or "").strip():
                phrases = [str(job.payload.message).strip()]
            if not phrases:
                raise ValueError("voice_broadcast job has no message candidates")

            voice_channel = str(job.payload.voice_channel or "").strip() or "whatsapp"
            if str(job.payload.voice_group or "").strip():
                if voice_channel != "whatsapp":
                    raise ValueError("voice_broadcast group targets require WhatsApp")
                chat_target, err = policy_adapter.resolve_whatsapp_group(
                    str(job.payload.voice_group).strip()
                )
                if err is not None or not chat_target:
                    raise ValueError(err or "failed to resolve voice_broadcast group")
            else:
                chat_target = (
                    str(job.payload.voice_chat_id or "").strip()
                    or str(job.payload.to or "").strip()
                )
                if not chat_target:
                    raise ValueError("voice_broadcast job has no target chat")

            quiet = evaluate_voice_quiet_gate(
                payload=job.payload,
                inbound_archive=inbound_archive,
                channel=voice_channel,
                chat_id=chat_target,
                now=datetime.now(UTC).astimezone(),
            )
            if quiet.status == "defer":
                retry_at_ms = quiet.retry_at_ms or int((time.time() + 1800) * 1000)
                raise CronJobDeferredError(quiet.reason, retry_at_ms=retry_at_ms)
            if quiet.status == "skip":
                raise CronJobSkippedError(quiet.reason)

            content = await _generate_voice_phrase(
                job,
                channel=voice_channel,
                chat_id=chat_target,
                fallback_phrases=phrases,
            )
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

            args["channel"] = voice_channel
            if str(job.payload.voice_group or "").strip():
                args["group"] = str(job.payload.voice_group).strip()
            else:
                args["chat_id"] = chat_target

            result = await responder.tools.execute("send_voice", args)
            if str(result).startswith("Error:"):
                raise RuntimeError(str(result))
            _remember_voice_phrase(job, content)
            return str(result)

        response = await responder.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            model_profile=job.payload.model_profile,
        )
        if job.payload.deliver and job.payload.to:
            delivery_channel = job.payload.channel or "cli"
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=(
                        f"cron:{job.id}:"
                        f"{job.state.last_run_at_ms or job.state.next_run_at_ms or ''}"
                    ),
                    channel=delivery_channel,
                    chat_id=job.payload.to,
                    content=response or "",
                )
            else:
                await bus.publish_outbound(
                    OutboundMessage(
                        channel=delivery_channel,
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
                fail_content = (
                    f"Workflow '{wf_name}' failed at step {job.payload.workflow_step}: "
                    f"{response[:200]}. Use /cron workflow_list to review."
                )
                if service_effects is not None:
                    await service_effects.send(
                        source="cron",
                        operation_ref=f"workflow:{wf_name}:step{job.payload.workflow_step}:failed",
                        channel=fail_channel,
                        chat_id=fail_chat,
                        content=fail_content,
                    )
                else:
                    await bus.publish_outbound(OutboundMessage(
                        channel=fail_channel, chat_id=fail_chat,
                        content=fail_content,
                    ))
            else:
                from uuid import uuid4
                run_id = uuid4().hex[:8]
                await _handle_chain(job, response, run_id)

        return response

    async def _handle_chain(job: CronJob, output: str, run_id: str = "") -> None:
        next_job = cron.get_job(job.payload.next_job_id) if job.payload.next_job_id else None
        if not next_job:
            logger.warning("Chained job {} not found", job.payload.next_job_id)
            return

        remaining = job.payload.max_chain_depth - 1
        if remaining <= 0:
            wf_name = job.payload.workflow_id or job.id
            stop_channel = job.payload.approval_channel or job.payload.channel or "cli"
            stop_chat = job.payload.to or "direct"
            stop_content = f"Workflow '{wf_name}' stopped: max chain depth reached."
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"workflow:{wf_name}:max-depth",
                    channel=stop_channel,
                    chat_id=stop_chat,
                    content=stop_content,
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=stop_channel,
                    chat_id=stop_chat,
                    content=stop_content,
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

            approval_content = (
                f"{output}\n\n---\n"
                f"Workflow step {job.payload.workflow_step} complete.\n"
                f"Next: {next_job.name}\n"
                f"Reply with this code to approve: {approval_id}"
            )
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"workflow-approval:{approval_id}",
                    channel=approval_channel,
                    chat_id=approval_chat,
                    content=approval_content,
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=approval_channel, chat_id=approval_chat,
                    content=approval_content,
                ))
        else:
            prompt = build_chained_prompt(output, next_job.payload.message, input_from_previous=next_job.payload.input_from_previous)
            next_job.payload.max_chain_depth = remaining
            chain_response = await responder.process_direct(
                prompt,
                session_key=f"cron:{next_job.id}:{run_id}",
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to or "direct",
                model_profile=next_job.payload.model_profile,
            )
            if next_job.payload.deliver and next_job.payload.to:
                chain_channel = next_job.payload.channel or "cli"
                if service_effects is not None:
                    await service_effects.send(
                        source="cron",
                        operation_ref=f"cron:{next_job.id}:{run_id}",
                        channel=chain_channel,
                        chat_id=next_job.payload.to,
                        content=chain_response or "",
                    )
                else:
                    await bus.publish_outbound(OutboundMessage(
                        channel=chain_channel,
                        chat_id=next_job.payload.to,
                        content=chain_response or "",
                    ))
            if next_job.payload.next_job_id and chain_response is not None and not is_chain_failure(chain_response):
                await _handle_chain(next_job, chain_response, run_id)

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
        effect_router=effect_router,
        processing_store=processing_store,
        release_participation_chat=_release_participation_chat,
    )

    # IPC socket for overseer commands
    from yeoman_gateway.ipc.gateway_socket import GatewaySocket

    ipc_config = config.ipc
    socket_path = Path(ipc_config.gateway_socket_path).expanduser()

    async def ipc_send_message(channel: str, chat_id: str, content: str) -> dict:
        if service_effects is not None:
            await service_effects.send(
                source="ipc",
                operation_ref=f"ipc:{_uuid4().hex}",
                channel=channel,
                chat_id=chat_id,
                content=content,
            )
            return {"queued": True}
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

    async def ipc_owner_turn(
        prompt: str,
        session_key: str | None,
        chat_id: str,
        post_to_whatsapp: bool,
        actor_principal: str | None = None,
        peer: str | None = None,
    ) -> dict:
        from yeoman_gateway.ipc.owner_turn import process_owner_turn

        async def _owner_outbound(message: OutboundMessage) -> None:
            from yeoman_gateway.processing.dispatch import CURRENT_PRINCIPAL

            CURRENT_PRINCIPAL.set(str(chat_id))
            try:
                await responder.send_outbound(message)
            finally:
                CURRENT_PRINCIPAL.set("")

        return await process_owner_turn(
            prompt=prompt,
            chat_id=chat_id,
            session_key=session_key,
            post_to_whatsapp=post_to_whatsapp,
            policy_adapter=policy_adapter,
            responder=responder,
            bus=bus,
            outbound_dispatch=_owner_outbound,
            actor_principal=actor_principal,
            peer=peer,
        )

    async def ipc_a2a_send(
        target: str,
        kind: str,
        text: str,
        idempotency_key: str,
        peer: str,
    ) -> dict:
        from yeoman_gateway.ipc.owner_turn import process_a2a_delivery

        return await process_a2a_delivery(
            target=target,
            kind=kind,
            text=text,
            idempotency_key=idempotency_key,
            peer=peer,
            policy_adapter=policy_adapter,
            responder=responder,
        )

    configured_a2a_peer = os.environ.get("YEOMAN_A2A_PEER_ID", "").strip()
    a2a_content_types = {
        item.strip()
        for item in os.environ.get("YEOMAN_A2A_CONTENT_TYPES", "text").split(",")
        if item.strip()
    }
    a2a_whatsapp_enabled = os.environ.get(
        "YEOMAN_A2A_WHATSAPP_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    artifact_root_value = os.environ.get("YEOMAN_A2A_ARTIFACT_ROOT", "").strip()
    try:
        artifact_max_bytes = int(
            os.environ.get("YEOMAN_A2A_MAX_ARTIFACT_BYTES", str(5 * 1024 * 1024))
        )
        artifact_ttl_seconds = int(os.environ.get("YEOMAN_A2A_ARTIFACT_TTL_SECONDS", "300"))
    except ValueError:
        artifact_max_bytes = artifact_ttl_seconds = 0
    a2a_voice_store = build_a2a_voice_artifact_store(
        root=Path(artifact_root_value).expanduser() if artifact_root_value else None,
        managed_outgoing_root=config.channels.whatsapp.media.outgoing_path,
        tts=tts,
        model_router=model_router,
        max_bytes=artifact_max_bytes,
        ttl_seconds=artifact_ttl_seconds,
    )
    a2a_artifact_root = resolve_a2a_artifact_root(
        Path(artifact_root_value).expanduser() if artifact_root_value else None,
        config.channels.whatsapp.media.outgoing_path,
    )
    whatsapp_a2a_available = bool(
        configured_a2a_peer
        and a2a_whatsapp_enabled
        and a2a_content_types & {"text", "voice", "image", "file"}
        and config.channels.whatsapp.enabled
        and service_effects is not None
    )
    voice_a2a_available = bool(
        whatsapp_a2a_available
        and "voice" in a2a_content_types
        and a2a_voice_store is not None
    )
    advertised_a2a_skills = frozenset(
        ({"whatsapp.send"} if whatsapp_a2a_available else set())
        | ({"media.voice.generate"} if voice_a2a_available else set())
    )

    async def a2a_media_sender(
        *, operation_ref: str, chat_id: str, path: str, effect_id: str, caption: str = ""
    ) -> object:
        if effect_router is None or not effect_router.manages("whatsapp", chat_id):
            raise RuntimeError("managed effect path unavailable")
        payload = MediaPayload(media=(path,), caption=caption)
        return await effect_router.submit_message(
            OutboundMessage(
                channel="whatsapp",
                chat_id=chat_id,
                content=caption,
                media=[path],
                metadata={"message_id": operation_ref, "service_source": "a2a"},
            ),
            principal=SERVICE_PRINCIPALS["a2a"],
            capability="send_media",
            payload=payload,
            effect_id=effect_id,
        )

    async def ipc_a2a_invoke(
        peer: str,
        skill: str,
        input: dict[str, Any],
        task_id: str,
        context_id: str,
        effect_id: str,
        resolved_artifacts: list[dict[str, Any]],
    ) -> dict[str, object]:
        from functools import partial

        from yeoman_gateway.ipc.a2a_invoke import (
            process_a2a_invocation,
            resolve_whatsapp_recipient,
        )

        return await process_a2a_invocation(
            peer=peer,
            skill=skill,
            input=input,
            task_id=task_id,
            context_id=context_id,
            effect_id=effect_id,
            configured_peer=configured_a2a_peer,
            advertised_skills=advertised_a2a_skills,
            policy_adapter=policy_adapter,
            recipient_resolver=partial(
                resolve_whatsapp_recipient,
                policy_adapter=policy_adapter,
                contacts_service=contacts_service,
            ),
            effects=service_effects,
            effect_store=processing_store,
            sender_account="default",
            enabled_content_types=a2a_content_types,
            voice_generator=a2a_voice_store,
            resolved_artifacts=resolved_artifacts,
            artifact_root=a2a_artifact_root,
            media_sender=(
                a2a_media_sender
                if whatsapp_a2a_available
                and a2a_artifact_root is not None
                and (bool(a2a_content_types & {"image", "file"}) or voice_a2a_available)
                else None
            ),
        )

    async def ipc_a2a_capabilities() -> dict[str, object]:
        skills = sorted(advertised_a2a_skills)
        content_types = []
        if "whatsapp.send" in skills:
            content_types.extend(sorted(a2a_content_types & {"text", "image", "file"}))
        if "media.voice.generate" in skills:
            content_types.append("voice")
        return {
            "skills": skills,
            "content_types": content_types,
        }

    async def ipc_publish_event(kind: str, detail: dict) -> dict:
        from yeoman_gateway.bus.events import SystemEvent

        await bus.publish_event(SystemEvent(kind=kind, detail=detail, timestamp=time.time()))
        return {"published": True}

    gateway_socket = GatewaySocket(
        path=socket_path,
        send_message_handler=ipc_send_message,
        trigger_agent_turn_handler=ipc_trigger_agent_turn,
        owner_turn_handler=ipc_owner_turn,
        a2a_delivery_handler=ipc_a2a_send,
        a2a_invoke_handler=ipc_a2a_invoke,
        a2a_capabilities_handler=ipc_a2a_capabilities,
        publish_event_handler=ipc_publish_event,
        rate_limit=ipc_config.command_rate_limit,
    )

    lull_observer = None
    participation_maintenance = None
    if social_runtime_enabled and speakup_log is not None:
        assert policy_engine is not None
        from yeoman_gateway.consciousness.burst import BurstObserver
        from yeoman_gateway.consciousness.lull import LullObserver
        from yeoman_gateway.consciousness.participation_runtime import SourceOwner
        assert consciousness_tools is not None

        participation_material = None
        if participation_enabled:
            resolve_sources = getattr(inbound_archive, "resolve_source_ids", None)
            ensure_revisions = getattr(speakup_log, "ensure_source_revisions_sync", None)
            read_material = getattr(speakup_log, "material_for_opportunity", None)
            baseline_material = getattr(
                speakup_log, "initialize_material_baseline_sync", None
            )
            if all(
                callable(item)
                for item in (
                    resolve_sources,
                    ensure_revisions,
                    read_material,
                    baseline_material,
                )
            ):
                targets = {
                    str(item).strip()
                    for item in (
                        *config.processing.chats,
                        *config.processing.shadow_chats,
                    )
                    if str(item).strip()
                }
                for target in sorted(targets):
                    channel, separator, chat_id = target.partition(":")
                    if separator and channel and chat_id:
                        baseline_material(
                            channel=channel,
                            chat_id=chat_id,
                            source_ids=resolve_sources(channel, chat_id, None),
                        )

                def _participation_material(
                    channel: str,
                    chat_id: str,
                    source_ids: tuple[str, ...] | None,
                ) -> tuple[tuple[str, ...], int]:
                    current = getattr(policy_adapter, "current_activation", None)
                    snapshot = current(channel, chat_id) if callable(current) else None
                    if snapshot is None:
                        return (), 0
                    lane = str(getattr(snapshot, "lane", "production"))
                    resolved = resolve_sources(channel, chat_id, source_ids)
                    senders = inbound_archive.senders_for_messages(
                        channel, chat_id, tuple(resolved)
                    )
                    resolved = tuple(
                        source_id
                        for source_id in resolved
                        if participant_is_allowed(
                            engine=policy_engine,
                            channel=channel,
                            chat_id=chat_id,
                            sender=str(senders.get(source_id) or ""),
                        )
                    )
                    if resolved:
                        ensure_revisions(
                            channel=channel,
                            chat_id=chat_id,
                            source_ids=resolved,
                        )
                    return read_material(
                        channel,
                        chat_id,
                        resolved,
                        lane=lane,
                    )

                participation_material = _participation_material

        source_owner = SourceOwner(store=speakup_log)
        if participation_enabled:
            participation_runtime, opportunity_scheduler, participation_decision = (
                _build_participation_runtime(
                    config=config,
                    source_owner=source_owner,
                    log=speakup_log,
                    policy_engine=policy_engine,
                    inbound_archive=inbound_archive,
                    processing_store=processing_store,
                    responder=responder,
                    policy_adapter=policy_adapter,
                    approval_tools=consciousness_tools,
                )
            )
        else:
            participation_runtime, opportunity_scheduler, participation_decision = (
                None,
                None,
                None,
            )
        direct_fence_setter = getattr(processing_gate, "set_direct_fence_callbacks", None)
        cancel_participation = getattr(opportunity_scheduler, "cancel_chat", None)
        if (
            callable(direct_fence_setter)
            and processing_store is not None
            and callable(cancel_participation)
        ):

            def _trusted_direct_assignment(
                request: object, decision: object, assignment: object
            ) -> bool:
                del request
                return bool(
                    getattr(decision, "accept_message", False)
                    and getattr(decision, "should_respond", False)
                    and str(getattr(assignment, "turn_id", "") or "")
                    and str(getattr(assignment, "rule", "") or "") != "ambient"
                )

            direct_fence_setter(
                classifier=_trusted_direct_assignment,
                note=processing_store.note_direct_admission,
                cancel=cancel_participation,
            )
        if isinstance(participation_decision, tuple):
            _decision_runtime, _unused_reconciler = participation_decision
        else:
            _decision_runtime = None
        participation_maintenance = None
        maintenance_config = getattr(config.processing, "participation_maintenance", None)
        if maintenance_config is not None and bool(
            getattr(maintenance_config, "enabled", False)
        ):
            from yeoman_gateway.consciousness.participation_maintenance import (
                ParticipationMaintenance,
            )

            participation_maintenance = ParticipationMaintenance(
                ledger=speakup_log,
                # Receipt projection is owned by ReconciliationService. Maintenance
                # only classifies already-delivered outcomes.
                reconciler=None,
                archive=inbound_archive,
                classifier=None,
                observation_window_minutes=int(
                    getattr(maintenance_config, "observation_window_minutes", 120)
                ),
                batch_size=int(getattr(maintenance_config, "batch_size", 20)),
                interval_seconds=int(getattr(maintenance_config, "interval_seconds", 900)),
            )

        if participation_runtime is not None:
            from yeoman_gateway.consciousness.participation_runtime import (
                ParticipationIngress,
            )

            def _participation_active(channel: str, chat_id: str) -> bool:
                current = getattr(policy_adapter, "current_activation", None)
                snapshot = current(channel, chat_id) if callable(current) else None
                pause_reason = getattr(
                    policy_adapter, "participation_pause_reason", None
                )
                return bool(
                    snapshot is not None
                    and (getattr(snapshot, "live", False) or getattr(snapshot, "observing", False))
                    and not (
                        callable(pause_reason) and pause_reason(channel, chat_id)
                    )
                )

            def _canonical_direct_source(event: object) -> bool:
                if processing_store is None:
                    return False
                channel = str(getattr(event, "channel", "") or "")
                chat_id = str(getattr(event, "chat_id", "") or "")
                source_ids = tuple(
                    str(item)
                    for item in (getattr(event, "source_event_ids", ()) or ())
                    if str(item)
                )
                if not source_ids:
                    message_id = str(getattr(event, "message_id", "") or "")
                    source_ids = (message_id,) if message_id else ()
                return any(
                    processing_store.direct_admission_for_event(
                        source_id, channel=channel, chat_id=chat_id
                    )
                    is not None
                    for source_id in source_ids
                )

            _ingress = ParticipationIngress(
                runtime=participation_runtime,
                ledger=speakup_log,
                is_active=_participation_active,
                is_direct=_canonical_direct_source,
                material_provider=participation_material,
            )

            async def _on_observed_inbound(event: object) -> None:
                """Event-bus adapter: the bus awaits every handler, so this one awaits.

                The ingress itself stays synchronous and bounded: it claims the source,
                computes the durable revision and offers to the scheduler without
                awaiting a judge, generator or transport.
                """
                try:
                    _ingress.handle_event(event)
                except Exception as exc:  # noqa: BLE001 - one producer must not fail dispatch
                    logger.warning(
                        "participation_ingress_failed error_type={}", type(exc).__name__
                    )

            bus.subscribe_event("InboundObservedEvent", _on_observed_inbound)

        async def _observer_eligible(channel: str, chat_id: str, *, trigger: str) -> bool:
            current = getattr(policy_adapter, "current_activation", None)
            snapshot = current(channel, chat_id) if callable(current) else None
            pause_reason = getattr(policy_adapter, "participation_pause_reason", None)
            if callable(pause_reason) and pause_reason(channel, chat_id):
                return False
            if participation_runtime is not None and snapshot is not None:
                if getattr(snapshot, "live", False) or getattr(snapshot, "observing", False):
                    return True
            return False

        def _trigger(channel: str, chat_id: str, trigger: str) -> object:
            """Route one observer trigger to its already-resolved social owner."""
            result = _offer_participation_trigger(
                channel=channel,
                chat_id=chat_id,
                trigger=trigger,
                runtime=participation_runtime,
                policy_adapter=policy_adapter,
                material_provider=participation_material,
            )
            if result is not None:
                return result
            return {"status": "skipped"}

        burst_observer = BurstObserver(
            config=config,
            state_path=consciousness_data_dir / "burst_state.json",
            on_burst=lambda channel, chat_id: _trigger(channel, chat_id, "burst"),
            is_eligible=lambda channel, chat_id: _observer_eligible(
                channel, chat_id, trigger="burst"
            ),
            session_manager=session_manager,
        )
        bus.subscribe_event("InboundObservedEvent", burst_observer.handle)

        if config.consciousness.lull_enabled:
            lull_observer = LullObserver(
                config=config,
                state_path=consciousness_data_dir / "lull_state.json",
                on_lull=lambda channel, chat_id: _trigger(channel, chat_id, "lull"),
                is_eligible=lambda channel, chat_id: _observer_eligible(
                    channel, chat_id, trigger="lull"
                ),
                session_manager=session_manager,
            )
            bus.subscribe_event("InboundObservedEvent", lull_observer.handle)

    shared_fact_runtime = build_shared_fact_runtime(
        config,
        store=processing_store,
        processing=processing_store,
        chat_registry=chat_registry,
        policy=policy_adapter,
        memory=memory_service,
    )
    if shared_fact_runtime is not None:
        responder.shared_facts = shared_fact_runtime

    return GatewayRuntime(
        orchestrator=orchestrator_service,
        channels=channels,
        cron=cron,
        heartbeat=heartbeat,
        inbound_archive=inbound_archive,
        responder=responder,
        memory=memory_service,
        contacts=contacts_service,
        chat_registry=chat_registry,
        bus=bus,
        gateway_socket=gateway_socket,
        speakup_log=speakup_log,
        lull_observer=lull_observer,
        opportunity_scheduler=opportunity_scheduler,
        participation_maintenance=participation_maintenance,
        processing=processing_store,
        reconciliation=build_reconciliation_service(
            config,
            processing_store,
            project_participation_receipts=project_participation_receipts,
        ),
        retention=build_retention_service(config, processing_store),
        shared_facts=shared_fact_runtime,
        startup_hook=_notify_pending_persona_evolution_reviews,
    )
