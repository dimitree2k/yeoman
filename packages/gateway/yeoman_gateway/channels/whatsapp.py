"""WhatsApp channel implementation using the strict bridge protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION, REPLAYABLE_EVENT_TYPES

from yeoman_gateway.bus.events import OutboundMessage, ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.base import BaseChannel
from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager
from yeoman_gateway.core.models import InboundEvent as CoreInboundEvent
from yeoman_gateway.implicit_addressing import (
    DEFAULT_BOT_NAME_ALIASES,
    contains_bot_name,
    looks_like_question_or_request,
)
from yeoman_gateway.media.asr import ASRTranscriber
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.media.vision import VisionDescriber

_VOLATILE_PROVIDER_FIELDS = frozenset(
    {
        "observedAt",
        "observed_at_ms",
        "ingestedAt",
        "ingested_at_ms",
        "ingestionAt",
        "ingestion_at_ms",
        "receivedAt",
        "received_at_ms",
        "committedAt",
        "committed_at_ms",
        "commit_ms",
        "confirmed_ms",
    }
)


def _canonical_provider_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical_provider_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _canonical_provider_value(value[key])
        for key in sorted(value)
        if key not in _VOLATILE_PROVIDER_FIELDS
    }

if TYPE_CHECKING:
    from yeoman_gateway.media.document_cache import DocumentCache
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_gateway.providers.factory import ProviderFactory
    from yeoman_gateway.storage.chat_registry import ChatRegistry
    from yeoman_gateway.storage.inbound_archive import InboundArchive


def _markdown_to_whatsapp(text: str) -> str:
    """Convert markdown to WhatsApp-compatible format."""
    if not text:
        return ""

    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", save_code_block, text)

    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)

    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*(.*)$", r"> \1", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", lambda m: f"{m.group(0)}", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", code)

    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", f"```\n{code}\n```")

    return text.strip()


_WHATSAPP_MENTION_RE = re.compile(r"(?<!\w)@([^\s@]+(?:@[^\s@]+)?)")
_WHATSAPP_MENTION_TOKEN_TRAILING = ".,;:!?)]}>\"'”’"
_PHONE_MENTION_RE = re.compile(r"(?<!\w)\+(\d{10,15})(?!\d)")


def _normalize_whatsapp_jid(value: str) -> str:
    """Normalize WhatsApp JIDs into a stable wire format."""
    token = str(value or "").strip()
    if not token:
        return ""
    left, sep, right = token.partition("@")
    left = left.split(":", 1)[0].strip()
    right = right.strip()
    if not left:
        return ""
    return f"{left}@{right}" if sep and right else left


def _whatsapp_jid_user_token(value: str) -> str:
    """Extract the user token portion from a WhatsApp JID."""
    normalized = _normalize_whatsapp_jid(value)
    return normalized.split("@", 1)[0] if normalized else ""


def _timestamp_to_datetime(value: int) -> datetime:
    """Bridge timestamps are epoch seconds; tolerate millisecond values."""
    seconds = float(value or 0)
    if seconds > 1e11:
        seconds /= 1000.0
    if seconds <= 0:
        return datetime.now(UTC)
    return datetime.fromtimestamp(seconds, tz=UTC)


DEDUPE_TTL_SECONDS = 20 * 60
DEDUPE_CLEANUP_INTERVAL_SECONDS = 30
TYPING_LOOP_INTERVAL_SECONDS = 4.0
TYPING_MAX_DURATION_SECONDS = 45.0
SEND_CONNECT_WAIT_SECONDS = 8.0
SEND_MAX_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY_SECONDS = 0.6
BRIDGE_ACK_QUEUE_MAXSIZE = 128
BRIDGE_ACK_TIMEOUT_SECONDS = 20.0
# A single sender/chat must not be able to retain an unbounded acknowledged burst while
# the legacy debounce timer is repeatedly reset.  Oversized/overflowing buckets are
# flushed synchronously; no acknowledged event is dropped.
WHATSAPP_DEBOUNCE_MAX_ITEMS = 32
WHATSAPP_DEBOUNCE_MAX_BYTES = 64 * 1024


class BridgeProtocolMismatchError(RuntimeError):
    """Bridge protocol version mismatch."""


class BridgeProtocolError(RuntimeError):
    """Bridge returned a protocol-level error."""

    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class InboundEvent:
    message_id: str
    chat_jid: str
    participant_jid: str
    sender_id: str
    sender_phone_jid: str | None
    is_group: bool
    text: str
    timestamp: int
    mentioned_jids: list[str]
    mentioned_bot: bool
    reply_to_bot: bool
    reply_to_message_id: str | None
    reply_to_participant: str | None
    reply_to_text: str | None
    media_kind: str | None
    media_type: str | None
    media_file_name: str | None
    media_path: str | None
    media_bytes: int | None
    media_description: str | None
    voice_transcript: str | None
    sender_name: str | None = None
    thread_assignment: "dict[str, Any] | None" = None
    #: Set when the processing core already acted for this message, so the classic pipeline
    #: must not add its own acknowledgement reaction or stop the granted answer.
    processing_reacted: bool = False
    processing_answer_granted: bool = False
    reply_to_media_kind: str | None = None
    reply_to_media_type: str | None = None
    reply_to_media_path: str | None = None
    reply_to_media_bytes: int | None = None
    lid_conflict: bool = False
    source_event_ids: tuple[str, ...] = ()

    @property
    def source_ids(self) -> tuple[str, ...]:
        """Ordered provider ids represented by this event, including debounced batches."""
        return self.source_event_ids or ((self.message_id,) if self.message_id else ())


#: Bridge frame types that only carry journal evidence (never a chat turn).
_PROCESSING_SIGNAL_TYPES = frozenset({"edit", "delete", "reaction", "receipt"})


@dataclass(frozen=True, slots=True)
class _AmbientOutcome:
    """What the judge's verdict turned into: an answer turn, one reaction, or nothing."""

    assignment: Any | None = None
    reacted: bool = False


@dataclass(slots=True)
class _BridgeEventWork:
    """One committed Bridge event waiting for an ACK and projection."""

    frame: dict[str, Any]
    kind: str
    payload: dict[str, Any]
    event_id: str
    fingerprint: tuple[str, ...]
    completion: asyncio.Future[bool]


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel backed by the Node.js bridge protocol v3."""

    name = "whatsapp"

    def __init__(
        self,
        config: WhatsAppConfig,
        bus: MessageBus,
        inbound_archive: "InboundArchive | None" = None,
        model_router: "ModelRouter | None" = None,
        media_storage: MediaStorage | None = None,
        provider_factory: "ProviderFactory | None" = None,
        document_cache: "DocumentCache | None" = None,
        groq_api_key: str | None = None,
        openai_api_key: str | None = None,
        openai_api_base: str | None = None,
        openai_extra_headers: dict[str, str] | None = None,
    ):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self.inbound_archive = inbound_archive
        self._model_router = model_router
        self._media_storage = media_storage or MediaStorage(
            incoming_dir=self.config.media.incoming_path,
            outgoing_dir=self.config.media.outgoing_path,
        )
        self._document_cache = document_cache
        self._vision_describer = (
            VisionDescriber(provider_factory) if provider_factory is not None else None
        )
        self._asr_transcriber = ASRTranscriber(
            groq_api_key=groq_api_key,
            openai_api_key=openai_api_key,
            openai_api_base=openai_api_base,
            openai_extra_headers=openai_extra_headers,
            max_concurrency=self.config.media.max_asr_concurrency,
        )
        self._ws: Any | None = None
        self._connected = False
        self._events_subscribed = False
        self._events_subscription_pending = False
        self._pending_bridge_events: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._media_cleanup_task: asyncio.Task[None] | None = None
        self._processing_gate: Any | None = None
        self._processing_signals: Any | None = None
        self._reaction_action: Any | None = None
        self._ambient_judge: Any | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recent_message_ids: dict[str, float] = {}
        #: The platform account the bridge currently reports, carried so the identity
        #: projection can namespace its identifiers.  Empty until the first frame.
        self._processing_account_id: str = ""
        self._debounce_buffers: dict[str, list[InboundEvent]] = {}
        self._debounce_buffer_bytes: dict[str, int] = {}
        self._debounce_delays: dict[str, float] = {}
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self._debounce_locks: dict[str, asyncio.Lock] = {}
        self._debounce_tails: dict[str, asyncio.Task[None]] = {}
        self._debounce_publish_tasks: set[asyncio.Task[None]] = set()
        self._debounce_batch_sequences: dict[str, int] = {}
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._bridge_ack_queue: asyncio.Queue[_BridgeEventWork] | None = None
        self._bridge_ack_worker: asyncio.Task[None] | None = None
        self._bridge_pipeline_loop: asyncio.AbstractEventLoop | None = None
        self._bridge_event_lock: asyncio.Lock | None = None
        self._bridge_inflight: dict[str, _BridgeEventWork] = {}
        self._bridge_capture_events: set[threading.Event] = set()
        self._bridge_intake_closed = False
        self._stopping = False
        self._ack_queue_maxsize = BRIDGE_ACK_QUEUE_MAXSIZE
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._reconnect_attempts = 0
        self._repair_attempted = False
        self._next_dedupe_cleanup_at = 0.0
        self._max_dedupe_entries = max(1, int(self.config.max_dedupe_entries))
        self._max_debounce_buckets = max(1, int(self.config.max_debounce_buckets))
        self._debounce_max_items = WHATSAPP_DEBOUNCE_MAX_ITEMS
        self._debounce_max_bytes = WHATSAPP_DEBOUNCE_MAX_BYTES
        self._dedupe_evictions = 0
        self._debounce_overflow = 0
        self._presence_supported = True
        self._presence_unsupported_logged = False
        self._runtime = WhatsAppRuntimeManager()
        self._chat_registry: ChatRegistry | None = None
        try:
            from yeoman_gateway.storage.chat_registry import ChatRegistry

            self._chat_registry = ChatRegistry()
        except Exception as e:
            logger.warning(f"Failed to initialize chat registry: {e}")

    def _require_token(self) -> str:
        token = (self.config.bridge_token or "").strip()
        if not token:
            raise RuntimeError("channels.whatsapp.bridgeToken is required for protocol v3")
        return token

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.resolved_bridge_url
        token = self._require_token()
        startup_timeout_s = max(1.0, self.config.bridge_startup_timeout_ms / 1000.0)

        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")

        self._running = True
        self._stopping = False
        try:
            await asyncio.to_thread(
                self._runtime.ensure_ready,
                auto_repair=self.config.bridge_auto_repair,
                start_if_needed=True,
                timeout_s=startup_timeout_s,
            )
        except Exception as e:
            logger.error(f"WhatsApp runtime preparation failed: {e}")
            self._running = False
            return
        await self._run_media_cleanup_once()
        if self.config.media.enabled:
            self._media_cleanup_task = asyncio.create_task(self._media_cleanup_loop())

        while self._running:
            try:
                async with websockets.connect(
                    bridge_url,
                    max_size=self.config.max_payload_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    self._connected = False
                    self._events_subscribed = False
                    self._events_subscription_pending = False
                    self._pending_bridge_events.clear()
                    self._reader_task = asyncio.create_task(self._read_loop())

                    try:
                        await self._verify_bridge_health(token, timeout_seconds=startup_timeout_s)
                        await self._subscribe_bridge_events(token, timeout_seconds=startup_timeout_s)
                    except Exception as e:
                        if self._is_repairable_startup_error(e):
                            if self.config.bridge_auto_repair and not self._repair_attempted:
                                self._repair_attempted = True
                                logger.warning(
                                    f"WhatsApp bridge startup failed ({e}); attempting auto-repair once..."
                                )
                                await asyncio.to_thread(self._runtime.repair_once)
                                raise RuntimeError("bridge repaired, retrying startup") from e
                            raise RuntimeError(
                                "WhatsApp bridge failed deterministic startup after auto-repair: "
                                f"{e}"
                            ) from e
                        raise

                    if not self._connected or self._bridge_intake_closed:
                        raise BridgeProtocolError(
                            "ERR_SUBSCRIBE_REPLAY",
                            "Bridge subscription replay did not reach a healthy connected state",
                            True,
                        )
                    self._repair_attempted = False
                    self._reconnect_attempts = 0
                    logger.info(
                        "Connected to WhatsApp bridge (protocol v{})",
                        PROTOCOL_VERSION,
                    )

                    await self._reader_task

            except asyncio.CancelledError:
                break
            except RuntimeError as e:
                # Fail-fast for deterministic startup contract violations.
                if "deterministic startup" in str(e):
                    logger.error(f"WhatsApp channel fatal error: {e}")
                    self._running = False
                    break
                logger.warning(f"WhatsApp bridge connection error: {e}")
            except Exception as e:
                logger.warning(f"WhatsApp bridge connection error: {e}")
                if not self._running:
                    break

                self._reconnect_attempts += 1
                if self.config.reconnect_max_attempts > 0 and (
                    self._reconnect_attempts >= self.config.reconnect_max_attempts
                ):
                    logger.error(
                        "WhatsApp reconnect attempts exhausted "
                        f"({self._reconnect_attempts}/{self.config.reconnect_max_attempts})"
                    )
                    self._running = False
                    break

                delay = self._compute_backoff_ms(self._reconnect_attempts) / 1000.0
                logger.info(f"Reconnecting in {delay:.2f}s...")
                await asyncio.sleep(delay)
            finally:
                self._connected = False
                self._events_subscribed = False
                self._events_subscription_pending = False
                self._pending_bridge_events.clear()
                websocket = self._ws
                self._fail_pending("Bridge connection closed")
                if websocket is not None:
                    with contextlib.suppress(Exception):
                        await websocket.close()
                if self._reader_task:
                    reader = self._reader_task
                    if reader is not asyncio.current_task() and not reader.done():
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reader
                    self._reader_task = None
                # A connection teardown may race an ACKed projection.  Drain the
                # worker before resetting it; pre-ACK commands are bounded by their
                # normal command timeout and an ACKed projection is never cancelled.
                await self._drain_bridge_worker()
                await self._drain_debounce_projections()
                inbound_tasks = tuple(self._inbound_tasks)
                if inbound_tasks:
                    await asyncio.gather(*inbound_tasks, return_exceptions=True)
                    self._inbound_tasks.difference_update(inbound_tasks)
                await self._stop_bridge_worker()
                self._ws = None

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False
        # Keep the reader and websocket alive while the ACK worker drains.  It
        # still has to resolve ack_event responses; stopping intake below makes
        # any newly delivered event fail closed without entering the pipeline.
        self._bridge_intake_closed = True
        self._pending_bridge_events.clear()
        self._stopping = True

        for chat_id in list(self._typing_tasks):
            await self._stop_typing(chat_id)

        while self._bridge_capture_events:
            await asyncio.sleep(0.001)
        # Do not cancel the ACK worker here.  Once an event is ACKed, its
        # projection (including a debounce flush) must finish before shutdown.
        await self._drain_bridge_worker()
        await self._drain_debounce_projections()

        if self._reader_task:
            reader = self._reader_task
            if reader is not asyncio.current_task():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            self._reader_task = None

        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        self._events_subscribed = False
        self._events_subscription_pending = False
        self._fail_pending("Channel stopped")
        await self._stop_bridge_worker()

        inbound_tasks = tuple(self._inbound_tasks)
        if inbound_tasks:
            await asyncio.gather(*inbound_tasks, return_exceptions=True)
        self._inbound_tasks.difference_update(inbound_tasks)

        self._debounce_tasks.clear()
        self._debounce_buffers.clear()
        self._debounce_buffer_bytes.clear()
        self._debounce_delays.clear()
        self._debounce_tails.clear()
        self._debounce_batch_sequences.clear()

        if self._media_cleanup_task:
            self._media_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._media_cleanup_task
            self._media_cleanup_task = None

        if self._bridge_event_lock is not None:
            async with self._bridge_event_lock:
                for work in self._bridge_inflight.values():
                    if not work.completion.done():
                        work.completion.set_result(False)
                self._bridge_inflight.clear()
        if self._chat_registry is not None:
            with contextlib.suppress(Exception):
                self._chat_registry.close()
            self._chat_registry = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._connected:
            connected = await self._wait_connected_for_send(SEND_CONNECT_WAIT_SECONDS)
            if not connected:
                raise RuntimeError("WhatsApp bridge not connected")

        await self._stop_typing(msg.chat_id)

        forward_request = (
            msg.metadata.get("forward_message") if isinstance(msg.metadata, dict) else None
        )
        if forward_request is not None:
            if not isinstance(forward_request, dict):
                raise RuntimeError("WhatsApp forward request is invalid")
            source_chat_jid = str(forward_request.get("source_chat_id") or "").strip()
            source_message_id = str(forward_request.get("source_message_id") or "").strip()
            if not source_chat_jid or not source_message_id:
                raise RuntimeError("WhatsApp forward request missing source")
            result = await self._send_command_with_retry(
                "forward_message",
                {
                    "to": msg.chat_id,
                    "sourceChatJid": source_chat_jid,
                    "sourceMessageId": source_message_id,
                },
                timeout_seconds=20.0,
                max_attempts=self._send_attempts(msg.metadata),
            )
            return _receipt_from_bridge(result)

        delete_request = (
            msg.metadata.get("delete_message") if isinstance(msg.metadata, dict) else None
        )
        if isinstance(delete_request, dict):
            message_id = str(delete_request.get("message_id") or "").strip()
            if not message_id:
                raise RuntimeError("WhatsApp delete request missing message_id")
            result = await self._send_command_with_retry(
                "delete_message",
                {"chatJid": msg.chat_id, "messageId": message_id},
                timeout_seconds=20.0,
                max_attempts=self._send_attempts(msg.metadata),
            )
            return _receipt_from_bridge(result, target_message_id=message_id)

        reaction = msg.metadata.get("reaction") if isinstance(msg.metadata, dict) else None
        if isinstance(reaction, dict):
            reaction_message_id = str(reaction.get("message_id") or "").strip()
            reaction_emoji = str(reaction.get("emoji") or "").strip()
            if reaction_message_id and reaction_emoji:
                payload: dict[str, object] = {
                    "chatJid": msg.chat_id,
                    "messageId": reaction_message_id,
                    "emoji": reaction_emoji,
                }
                participant = str(reaction.get("participant_jid") or "").strip()
                if participant:
                    payload["participantJid"] = participant
                if "from_me" in reaction:
                    payload["fromMe"] = bool(reaction.get("from_me"))
                result = await self._send_command_with_retry(
                    "react",
                    payload,
                    timeout_seconds=12.0,
                    max_attempts=self._send_attempts(msg.metadata),
                )
                return _receipt_from_bridge(result, target_message_id=reaction_message_id)
                if bool(msg.metadata.get("reaction_only", False)):
                    return

        reply_to = str(msg.reply_to or "").strip() or None
        text = _markdown_to_whatsapp(msg.content)
        # Rewrite +phone to @phone so the mention resolver picks them up.
        text = _PHONE_MENTION_RE.sub(r"@\1", text)
        mentions = self._resolve_outbound_mentions(text, msg.metadata)
        allow_mentions = bool(mentions) and msg.chat_id.endswith("@g.us")

        if msg.media:
            caption_used = False
            sent_any_media = False
            media_receipt: dict[str, Any] | None = None
            for media_path in msg.media:
                validated = self._media_storage.validate_outgoing_path(media_path)
                if validated is None or not validated.exists() or not validated.is_file():
                    logger.warning("WhatsApp outbound media path rejected: {}", media_path)
                    continue

                mime = "application/octet-stream"
                suffix = validated.suffix.lower()
                if suffix in {".ogg", ".opus"}:
                    mime = "audio/ogg; codecs=opus"
                elif suffix == ".mp3":
                    mime = "audio/mpeg"
                elif suffix == ".wav":
                    mime = "audio/wav"
                elif suffix in {".jpg", ".jpeg"}:
                    mime = "image/jpeg"
                elif suffix == ".png":
                    mime = "image/png"
                elif suffix == ".webp":
                    mime = "image/webp"
                elif suffix == ".gif":
                    mime = "image/gif"
                elif suffix == ".pdf":
                    mime = "application/pdf"
                elif suffix == ".txt":
                    mime = "text/plain"
                elif suffix == ".mp4":
                    mime = "video/mp4"

                caption = None
                if not caption_used and text and not mime.startswith("audio/"):
                    caption = text
                    caption_used = True

                payload: dict[str, object] = {
                    "to": msg.chat_id,
                    "mediaPath": str(validated),
                    "mimeType": mime,
                    "fileName": validated.name,
                    "caption": caption,
                }
                if reply_to:
                    payload["replyToMessageId"] = reply_to
                if allow_mentions and caption:
                    payload["mentions"] = list(mentions)
                media_result = await self._send_command_with_retry(
                    "send_media",
                    payload,
                    timeout_seconds=30.0,
                    max_attempts=self._send_attempts(msg.metadata),
                )
                media_receipt = _receipt_from_bridge(media_result)
                sent_any_media = True

                # Best-effort cleanup for generated TTS voice notes.
                if (
                    validated.name.startswith("tts-")
                    and validated.suffix.lower() in {".ogg", ".opus"}
                    and validated.parent.name == "tts"
                ):
                    with contextlib.suppress(OSError):
                        validated.unlink()

            if sent_any_media:
                return media_receipt
            if not text:
                raise RuntimeError(
                    "WhatsApp outbound had no valid media and no text"
                )

        if not text:
            return

        payload: dict[str, object] = {
            "to": msg.chat_id,
            "text": text,
        }
        if reply_to:
            payload["replyToMessageId"] = reply_to
        if allow_mentions:
            payload["mentions"] = list(mentions)
        text_result = await self._send_command_with_retry(
            "send_text",
            payload,
            timeout_seconds=20.0,
            max_attempts=self._send_attempts(msg.metadata),
        )
        return _receipt_from_bridge(text_result)

    async def start_typing(self, chat_id: str) -> None:
        """Public typing API used by policy-aware orchestration."""
        await self._start_typing(chat_id, state="composing")

    async def start_recording(self, chat_id: str) -> None:
        """Show recording indicator (microphone icon) instead of typing dots."""
        await self._start_typing(chat_id, state="recording")

    async def stop_typing(self, chat_id: str) -> None:
        """Public typing API used by policy-aware orchestration."""
        await self._stop_typing(chat_id)

    async def send_reaction(self, msg: ReactionMessage) -> None:
        """Send a reaction emoji to a specific message via WhatsApp."""
        if not self._connected:
            connected = await self._wait_connected_for_send(SEND_CONNECT_WAIT_SECONDS)
            if not connected:
                raise RuntimeError("WhatsApp bridge not connected")

        if not msg.message_id:
            logger.warning("Cannot send reaction: missing message_id")
            return

        payload: dict[str, object] = {
            "chatJid": msg.chat_id,
            "messageId": msg.message_id,
            "emoji": msg.emoji,
        }
        if msg.participant_jid:
            payload["participantJid"] = msg.participant_jid
        result = await self._send_command_with_retry(
            "react",
            payload,
            timeout_seconds=20.0,
            max_attempts=self._send_attempts(msg.metadata),
        )
        return _receipt_from_bridge(result, target_message_id=msg.message_id)

    async def lookup_message(self, chat_id: str, message_id: str) -> dict[str, object]:
        """Look up one exact locally retained provider message reference."""
        if not self._connected:
            connected = await self._wait_connected_for_send(SEND_CONNECT_WAIT_SECONDS)
            if not connected:
                return {"status": "unsupported"}
        result = await self._send_command_with_retry(
            "lookup_message",
            {"chatJid": str(chat_id), "messageId": str(message_id)},
            timeout_seconds=12.0,
            max_attempts=SEND_MAX_ATTEMPTS,
        )
        return result if isinstance(result, dict) else {"status": "unsupported"}

    async def _verify_bridge_health(self, token: str, timeout_seconds: float) -> None:
        response = await self._send_command(
            "health",
            {},
            timeout_seconds=timeout_seconds,
            token=token,
        )
        version = response.get("protocolVersion", response.get("version"))
        if version != PROTOCOL_VERSION:
            raise BridgeProtocolMismatchError(
                f"Bridge protocol mismatch: expected v{PROTOCOL_VERSION}, got {version!r}"
            )

    async def _subscribe_bridge_events(self, token: str, timeout_seconds: float) -> None:
        """Authenticate this connection as the sole durable event subscriber."""
        self._events_subscription_pending = True
        try:
            response = await self._send_command(
                "subscribe_events",
                {},
                timeout_seconds=timeout_seconds,
                token=token,
            )
            if response.get("subscribed") is not True:
                raise BridgeProtocolError(
                    "ERR_AUTH", "Bridge did not confirm canonical event subscription", True
                )
            self._events_subscribed = True
            # The authenticated response is the transport-ready fence.  Replay
            # projection may send a reaction/effect immediately, so publish the
            # connected state before draining frames received during subscribe.
            self._connected = True
            while self._pending_bridge_events:
                frame, kind, payload = self._pending_bridge_events.pop(0)
                captured = await self._capture_and_queue_bridge_event(frame, kind, payload)
                if not captured or self._bridge_intake_closed:
                    self._events_subscribed = False
                    self._connected = False
                    raise BridgeProtocolError(
                        "ERR_SUBSCRIBE_REPLAY",
                        "Canonical replay capture/ACK failed",
                        True,
                    )
        finally:
            self._events_subscription_pending = False
            if not self._events_subscribed:
                self._pending_bridge_events.clear()

    def _is_repairable_startup_error(self, err: Exception) -> bool:
        if isinstance(err, (BridgeProtocolMismatchError, BridgeProtocolError, TimeoutError)):
            return True
        # websockets connection errors are typically OSError based.
        if isinstance(err, OSError):
            return True
        text = str(err)
        return "Connect call failed" in text or "timed out" in text.lower()

    async def _read_loop(self) -> None:
        if not self._ws:
            return

        self._ensure_bridge_pipeline()
        async for raw in self._ws:
            await self._handle_bridge_message(raw)

    async def _handle_bridge_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge")
            return

        if not isinstance(data, dict):
            logger.warning("Invalid bridge frame shape")
            return

        version = data.get("version")
        if version != PROTOCOL_VERSION:
            if version is None:
                logger.warning(
                    "Bridge frame missing protocol version. "
                    "Likely outdated bridge build; run `yeoman channels bridge restart` to refresh."
                )
            else:
                logger.warning(f"Unexpected bridge protocol version: {version!r}")
            return

        msg_type = data.get("type")
        payload = data.get("payload")
        if msg_type == "response":
            if not isinstance(payload, dict):
                payload = {}
            request_id = data.get("requestId")
            if isinstance(request_id, str):
                self._resolve_pending(request_id, payload)
            return

        if isinstance(msg_type, str) and msg_type in REPLAYABLE_EVENT_TYPES:
            if not isinstance(payload, dict):
                self._reject_replayable_frame(msg_type, "payload_not_object")
                return
            from_reader = asyncio.current_task() is self._reader_task
            if from_reader and self._events_subscription_pending:
                if len(self._pending_bridge_events) >= max(1, int(self._ack_queue_maxsize)):
                    await self._close_bridge_intake("subscribe_queue_full")
                    return
                self._pending_bridge_events.append((data, str(msg_type), payload))
                return
            if from_reader and not self._events_subscribed:
                self._reject_replayable_frame(msg_type, "not_subscribed")
                return
            await self._capture_and_queue_bridge_event(data, str(msg_type), payload)
            return

        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "status":
            status = payload.get("status")
            logger.info(f"WhatsApp status: {status}")
            return

        if msg_type == "qr":
            logger.info("Scan QR code in bridge logs or login flow")
            return

        if msg_type == "error":
            logger.error(f"WhatsApp bridge error: {payload.get('error')}")
            return

    def _parse_inbound_event(self, payload: dict[str, Any]) -> InboundEvent | None:
        message_id = str(payload.get("messageId") or "").strip()
        chat_jid = str(payload.get("chatJid") or "").strip()
        participant_jid = str(payload.get("participantJid") or "").strip()
        sender_id = str(payload.get("senderId") or "").strip()
        sender_phone_jid = str(payload.get("senderPhoneJid") or "").strip() or None
        lid_conflict = bool(payload.get("lidConflict", False))
        sender_name = str(payload.get("senderName") or "").strip() or None
        text = str(payload.get("text") or "").strip()

        if not message_id or not chat_jid or not sender_id or not text:
            logger.warning("Dropping malformed inbound message event")
            return None

        timestamp_raw = payload.get("timestamp")
        timestamp = int(timestamp_raw) if isinstance(timestamp_raw, (int, float)) else 0

        mentioned_jids_raw = payload.get("mentionedJids")
        mentioned_jids = (
            [str(x) for x in mentioned_jids_raw if isinstance(x, str)]
            if isinstance(mentioned_jids_raw, list)
            else []
        )

        media = payload.get("media") if isinstance(payload.get("media"), dict) else None
        media_kind = (
            str(media.get("kind")) if isinstance(media, dict) and media.get("kind") else None
        )
        media_type = (
            str(media.get("mimeType"))
            if isinstance(media, dict) and media.get("mimeType")
            else None
        )
        media_file_name = (
            str(media.get("fileName")).strip()
            if isinstance(media, dict) and isinstance(media.get("fileName"), str)
            else ""
        )
        media_path = (
            str(media.get("path")).strip()
            if isinstance(media, dict) and isinstance(media.get("path"), str)
            else ""
        )
        media_bytes_raw = media.get("bytes") if isinstance(media, dict) else None
        media_bytes = int(media_bytes_raw) if isinstance(media_bytes_raw, (int, float)) else None

        reply_to_message_id = str(payload.get("replyToMessageId") or "").strip() or None
        reply_to_participant = str(payload.get("replyToParticipantJid") or "").strip() or None
        reply_to_text = str(payload.get("replyToText") or "").strip() or None
        reply_to_bot = bool(payload.get("replyToBot", False))

        reply_media = (
            payload.get("replyToMedia")
            if isinstance(payload.get("replyToMedia"), dict)
            else None
        )
        reply_to_media_kind = (
            str(reply_media.get("kind"))
            if isinstance(reply_media, dict) and reply_media.get("kind")
            else None
        )
        reply_to_media_type = (
            str(reply_media.get("mimeType"))
            if isinstance(reply_media, dict) and reply_media.get("mimeType")
            else None
        )
        reply_to_media_path = (
            str(reply_media.get("path")).strip()
            if isinstance(reply_media, dict) and isinstance(reply_media.get("path"), str)
            else ""
        ) or None
        reply_to_media_bytes_raw = (
            reply_media.get("bytes") if isinstance(reply_media, dict) else None
        )
        reply_to_media_bytes = (
            int(reply_to_media_bytes_raw)
            if isinstance(reply_to_media_bytes_raw, (int, float))
            else None
        )

        if reply_to_bot or reply_to_message_id or reply_to_text:
            logger.debug(
                "whatsapp_inbound_reply_meta chat={} message_id={} reply_to_bot={} "
                "reply_to_message_id={} has_reply_to_text={} reply_to_participant={}",
                chat_jid,
                message_id,
                reply_to_bot,
                reply_to_message_id or "-",
                bool(reply_to_text),
                reply_to_participant or "-",
            )

        return InboundEvent(
            message_id=message_id,
            chat_jid=chat_jid,
            participant_jid=participant_jid,
            sender_id=sender_id,
            sender_phone_jid=sender_phone_jid,
            lid_conflict=lid_conflict,
            sender_name=sender_name,
            is_group=bool(payload.get("isGroup", False)),
            text=text,
            timestamp=timestamp,
            mentioned_jids=mentioned_jids,
            mentioned_bot=bool(payload.get("mentionedBot", False)),
            reply_to_bot=reply_to_bot,
            reply_to_message_id=reply_to_message_id,
            reply_to_participant=reply_to_participant,
            reply_to_text=reply_to_text,
            media_kind=media_kind,
            media_type=media_type,
            media_file_name=media_file_name or None,
            media_path=media_path or None,
            media_bytes=media_bytes,
            media_description=None,
            voice_transcript=None,
            reply_to_media_kind=reply_to_media_kind,
            reply_to_media_type=reply_to_media_type,
            reply_to_media_path=reply_to_media_path,
            reply_to_media_bytes=reply_to_media_bytes,
        )

    def set_processing_signals(self, sink: Any | None) -> None:
        """Attach the journal sink for provider signals (edit/delete/reaction/receipt)."""
        self._processing_signals = sink

    def _reject_replayable_frame(self, kind: object, reason: str) -> None:
        """Reject malformed canonical input without echoing its untrusted payload."""
        logger.warning("Malformed replayable bridge frame type={} reason={}", kind, reason)

    @staticmethod
    def _valid_root_identity(value: object) -> bool:
        if not isinstance(value, str) or not value or value != value.strip():
            return False
        return not any(
            char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}
            for char in value
        )

    def _ensure_bridge_pipeline(self) -> None:
        loop = asyncio.get_running_loop()
        worker = self._bridge_ack_worker
        if (
            self._bridge_pipeline_loop is loop
            and self._bridge_ack_queue is not None
            and worker is not None
            and not worker.done()
        ):
            return
        self._bridge_pipeline_loop = loop
        self._bridge_ack_queue = asyncio.Queue(maxsize=max(1, int(self._ack_queue_maxsize)))
        self._bridge_event_lock = asyncio.Lock()
        self._bridge_inflight.clear()
        self._bridge_intake_closed = False
        self._bridge_ack_worker = asyncio.create_task(self._bridge_ack_worker_loop())

    def _event_fingerprint(
        self,
        *,
        kind: str,
        event_key: str,
        account_id: str,
        observed_at: int | float,
        payload: dict[str, Any],
    ) -> tuple[str, ...]:
        return (
            kind,
            event_key,
            account_id,
            json.dumps(
                _canonical_provider_value(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )

    async def _run_off_loop(self, operation: Any) -> Any:
        result: list[Any] = []
        error: list[BaseException] = []
        complete = threading.Event()
        self._bridge_capture_events.add(complete)

        def run_capture() -> None:
            try:
                result.append(operation())
            except BaseException as exc:
                error.append(exc)
            finally:
                complete.set()

        thread = threading.Thread(target=run_capture, name="whatsapp-canonical-capture", daemon=True)
        thread.start()
        try:
            while not complete.is_set():
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            while not complete.is_set():
                await asyncio.sleep(0.001)
            raise
        finally:
            thread.join()
            self._bridge_capture_events.discard(complete)
        if error:
            raise error[0]
        return result[0] if result else None

    async def _capture_off_loop(
        self,
        capture: Any,
        kind: str,
        payload: dict[str, Any],
        *,
        event_id: str,
        event_key: str,
        account: str,
        observed_at_ms: int,
    ) -> Any:
        return await self._run_off_loop(
            lambda: capture(
                kind,
                payload,
                event_id=event_id,
                event_key=event_key,
                account=account,
                observed_at_ms=observed_at_ms,
                strict=True,
            )
        )

    async def _capture_and_queue_bridge_event(
        self, frame: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> bool:
        """Commit one Bridge event before queueing its ACK/projection work."""
        event_id = frame.get("eventId")
        event_key = frame.get("eventKey")
        account_id = frame.get("accountId")
        observed_at = frame.get("observedAt")
        if not self._valid_root_identity(event_id):
            self._reject_replayable_frame(kind, "missing_event_id")
            return False
        if not self._valid_root_identity(event_key):
            self._reject_replayable_frame(kind, "missing_event_key")
            return False
        if not self._valid_root_identity(account_id):
            self._reject_replayable_frame(kind, "missing_account_id")
            return False
        # The account namespaces every identifier this message produces.
        self._processing_account_id = str(account_id)
        try:
            valid_observed_at = (
                not isinstance(observed_at, bool)
                and isinstance(observed_at, (int, float))
                and math.isfinite(observed_at)
                and observed_at > 0
            )
        except (OverflowError, ValueError):
            valid_observed_at = False
        if not valid_observed_at:
            self._reject_replayable_frame(kind, "missing_observed_at")
            return False

        if self._bridge_intake_closed or self._stopping:
            self._reject_replayable_frame(kind, "intake_closed")
            return False
        self._ensure_bridge_pipeline()

        assert self._bridge_event_lock is not None
        assert self._bridge_ack_queue is not None
        fingerprint = self._event_fingerprint(
            kind=kind,
            event_key=event_key,
            account_id=account_id,
            observed_at=observed_at,
            payload=payload,
        )
        current_task = asyncio.current_task()
        reader_task = self._reader_task
        wait_for_completion: asyncio.Future[bool] | None = None
        async with self._bridge_event_lock:
            existing = self._bridge_inflight.get(event_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    self._reject_replayable_frame(kind, "conflicting_event_id")
                    return False
                wait_for_completion = existing.completion
            else:
                work = _BridgeEventWork(
                    frame=frame,
                    kind=kind,
                    payload=payload,
                    event_id=event_id,
                    fingerprint=fingerprint,
                    completion=asyncio.get_running_loop().create_future(),
                )
                self._bridge_inflight[event_id] = work

        if wait_for_completion is not None:
            if current_task is not reader_task:
                return bool(await wait_for_completion)
            return True

        capture = getattr(self._processing_signals, "capture", None)
        if not callable(capture):
            self._reject_replayable_frame(kind, "canonical_sink_unavailable")
            await self._forget_bridge_work(work)
            if self._ws is not None:
                await self._close_bridge_intake("capture_unavailable")
            return False

        try:
            await self._capture_off_loop(
                capture,
                kind,
                payload,
                event_id=event_id,
                event_key=event_key,
                account=account_id,
                observed_at_ms=int(observed_at),
            )
        except Exception as exc:
            logger.warning(
                "WhatsApp canonical capture failed type={} error_type={}",
                kind,
                type(exc).__name__,
            )
            await self._forget_bridge_work(work)
            if self._ws is not None:
                await self._close_bridge_intake("capture_failed")
            return False

        if self._bridge_intake_closed or self._stopping:
            await self._forget_bridge_work(work)
            return False

        try:
            self._bridge_ack_queue.put_nowait(work)
        except asyncio.QueueFull:
            await self._forget_bridge_work(work)
            await self._close_bridge_intake("ack_queue_full")
            return False

        if current_task is not reader_task:
            return bool(await work.completion)
        return True

    async def _forget_bridge_work(self, work: _BridgeEventWork) -> None:
        lock = self._bridge_event_lock
        if lock is not None:
            async with lock:
                if self._bridge_inflight.get(work.event_id) is work:
                    self._bridge_inflight.pop(work.event_id, None)
        if not work.completion.done():
            work.completion.set_result(False)

    async def _close_bridge_intake(self, reason: str) -> None:
        if self._bridge_intake_closed:
            return
        self._bridge_intake_closed = True
        self._connected = False
        logger.warning("WhatsApp bridge intake closed reason={}", reason)
        # Wake every command waiter so the outer channel loop can tear down and
        # reconnect instead of leaving the Bridge outbox pending on a live socket.
        self._fail_pending(f"Bridge intake closed: {reason}")
        websocket = self._ws
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()

    async def _bridge_ack_worker_loop(self) -> None:
        queue = self._bridge_ack_queue
        if queue is None:
            return
        try:
            while True:
                work = await queue.get()
                try:
                    await self._ack_and_project_bridge_work(work)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _ack_and_project_bridge_work(self, work: _BridgeEventWork) -> None:
        succeeded = False
        try:
            await self._send_command(
                "ack_event",
                {"eventId": work.event_id},
                timeout_seconds=BRIDGE_ACK_TIMEOUT_SECONDS,
            )
            succeeded = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "WhatsApp canonical ACK failed type={} error_type={}",
                work.kind,
                type(exc).__name__,
            )
            # A synthetic/unit channel without a websocket is already
            # disconnected; only a live socket needs the forced reconnect fence.
            if self._ws is not None:
                await self._close_bridge_intake("ack_failed")
        else:
            try:
                await self._project_bridge_event(work)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The Bridge ACK is durable; projection errors cannot revoke it.
                logger.warning(
                    "WhatsApp inbound projection failed type={} error_type={}",
                    work.kind,
                    type(exc).__name__,
                )
        finally:
            lock = self._bridge_event_lock
            if lock is not None:
                async with lock:
                    if self._bridge_inflight.get(work.event_id) is work:
                        self._bridge_inflight.pop(work.event_id, None)
            if not work.completion.done():
                work.completion.set_result(succeeded)

    async def _project_bridge_event(self, work: _BridgeEventWork) -> None:
        if work.kind in _PROCESSING_SIGNAL_TYPES:
            # Strict edit/delete invalidation needs a durable event-id keyed
            # source-authority projection.  The legacy callback is resettable
            # process-local state, so Task 3B intentionally does not invoke it;
            # Task 4 owns that durable projection.
            return
        elif work.kind == "message":
            event = self._parse_inbound_event(work.payload)
            if event:
                await self._ingest_inbound_event(event)

    async def _drain_bridge_worker(self) -> None:
        """Drain all queued ACK/projection work before the worker is torn down.

        Each ACK command already has the normal finite command timeout.  Waiting
        on ``Queue.join`` here is deliberate: once an ACK succeeds, cancellation
        would lose a projection that the Bridge will not replay.
        """
        queue = self._bridge_ack_queue
        if queue is None:
            return
        await queue.join()

    async def _drain_debounce_projections(self) -> None:
        """Flush acknowledged debounce batches before connection/store teardown."""
        while True:
            timers = tuple(self._debounce_tasks.values())
            for timer in timers:
                if timer is not asyncio.current_task() and not timer.done():
                    timer.cancel()
            if timers:
                await asyncio.gather(*timers, return_exceptions=True)
            for key, timer in tuple(self._debounce_tasks.items()):
                if timer.done() and self._debounce_tasks.get(key) is timer:
                    self._debounce_tasks.pop(key, None)

            for key in tuple(self._debounce_buffers):
                lock = self._debounce_locks.setdefault(key, asyncio.Lock())
                async with lock:
                    events = self._detach_debounce_batch_locked(key)
                    if events:
                        self._schedule_debounce_publish_locked(key, events)

            publishers = tuple(self._debounce_publish_tasks)
            if publishers:
                await asyncio.gather(*publishers, return_exceptions=True)
                # Done callbacks remove completed tasks from the global set.  A
                # turn gives those callbacks a chance before the final check.
                await asyncio.sleep(0)
                continue

            if not self._debounce_tasks and not self._debounce_buffers:
                return

    async def _stop_bridge_worker(self) -> None:
        worker = self._bridge_ack_worker
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._bridge_ack_worker = None
        queue = self._bridge_ack_queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
        lock = self._bridge_event_lock
        if lock is not None:
            async with lock:
                for work in self._bridge_inflight.values():
                    if not work.completion.done():
                        work.completion.set_result(False)
                self._bridge_inflight.clear()

    def set_processing_gate(self, gate: Any | None) -> None:
        """Attach the fast gate of the new processing mode (``None`` keeps legacy)."""
        self._processing_gate = gate

    def set_reaction_action(self, action: Any | None) -> None:
        """Attach the ``react`` reply action (``None`` leaves reactions to the pipeline)."""
        self._reaction_action = action

    def set_ambient_judge(self, judge: Any | None) -> None:
        """Attach the verdict that decides whether an unaddressed message may be answered."""
        self._ambient_judge = judge

    @staticmethod
    def _judge_input(event: InboundEvent) -> str:
        """What the judge reads: the message, and for media what it actually contains.

        A voice message carries no text until it is transcribed, and an image carries its
        description - both belong to the question "does this need an answer?".
        """
        parts = [str(event.text or "").strip()]
        for extra in (event.voice_transcript, event.media_description):
            text = str(extra or "").strip()
            if text and text not in parts:
                parts.append(text)
        return "\n".join(part for part in parts if part)

    async def _maybe_answer_ambient(self, event: InboundEvent) -> _AmbientOutcome:
        """Ask the judge about an unaddressed message.

        The brake already ran in the gate, so this is the second half of the decision. The
        judge has three outcomes: a real answer (turn opened here, after the verdict, so no
        generation or typing happens before it), a single reaction from the owner's
        vocabulary (no turn at all), or silence. A decline restarts the brake window, so the
        judge is asked at most once per window instead of once per message.

        The configured reply action caps what a verdict may become: a chat on ``react`` never
        gets text, so an ``answer`` verdict is delivered as the reaction that action allows.
        """
        if self._ambient_judge is None:
            logger.warning(
                "ambient answer requested without a judge chat={} message_id={}",
                event.chat_jid,
                event.message_id,
            )
            return _AmbientOutcome()
        try:
            verdict = await self._ambient_judge.decide(self._judge_input(event))
        except Exception as exc:
            logger.warning(
                "ambient_judge_failed chat={} message_id={} error_type={}",
                event.chat_jid,
                event.message_id,
                type(exc).__name__,
            )
            return _AmbientOutcome()
        if not verdict.speaks:
            self._processing_gate.note_ambient_declined(event.message_id)
            return _AmbientOutcome()
        if not verdict.needs_turn:
            # A reaction is the whole reply: no turn, no typing, no text, own lineage.
            return _AmbientOutcome(reacted=await self._send_ambient_reaction(event, verdict))
        if not self._ambient_text_allowed(event):
            # The judge said "answer", but this chat does not want text: deliver the verdict
            # as a reaction instead of withdrawing it silently (owner decision, option E).
            return _AmbientOutcome(reacted=await self._reaction_from_verdict(event, verdict))
        try:
            core_event = self._to_core_event(event, event.message_id)
            assignment = self._processing_gate.reconcile_reply(core_event)
        except Exception as exc:
            logger.warning(
                "ambient_reply_failed chat={} message_id={} error_type={}",
                event.chat_jid,
                event.message_id,
                type(exc).__name__,
            )
            return _AmbientOutcome()
        if assignment is None:
            return _AmbientOutcome()
        self._processing_gate.note_ambient_answer(event.message_id)
        logger.info(
            "ambient_answer_granted chat={} message_id={} thread_id={} turn_id={}",
            event.chat_jid,
            event.message_id,
            getattr(assignment, "thread_id", "-"),
            getattr(assignment, "turn_id", "-"),
        )
        return _AmbientOutcome(assignment=assignment)

    def _ambient_text_allowed(self, event: InboundEvent) -> bool:
        """Whether an ``answer`` verdict may open a text turn in this chat."""
        action_for = getattr(self._processing_gate, "answer_kind_for", None)
        if action_for is None:  # a gate without the accessor keeps the old behaviour
            return True
        return str(action_for(self.name, event.chat_jid)) == "answer"

    async def _reaction_from_verdict(self, event: InboundEvent, verdict: Any) -> bool:
        """Deliver a capped ``answer`` verdict as one reaction, choosing an emoji if needed."""
        if getattr(verdict, "emoji", None):
            return await self._send_ambient_reaction(event, verdict)
        if self._reaction_action is None:
            self._processing_gate.note_ambient_declined(event.message_id)
            return False
        # An `answer` verdict carries no emoji, so the chooser picks one - one small call,
        # and only in a chat that does not want text.
        sent = await self._reaction_action(
            channel=self.name,
            chat_id=event.chat_jid,
            message_id=event.message_id,
            text=self._judge_input(event),
            principal=event.sender_id,
            participant_jid=event.participant_jid or event.sender_phone_jid,
        )
        if not sent:
            self._processing_gate.note_ambient_declined(event.message_id)
            return False
        self._processing_gate.note_ambient_reacted(event.message_id)
        logger.info(
            "ambient_answer_capped_to_reaction chat={} message_id={} emoji={}",
            event.chat_jid,
            event.message_id,
            sent,
        )
        return True

    async def _send_ambient_reaction(self, event: InboundEvent, verdict: Any) -> bool:
        """Send the judge's chosen emoji; a missing reaction path means silence."""
        if self._reaction_action is None or not getattr(verdict, "emoji", None):
            self._processing_gate.note_ambient_declined(event.message_id)
            return False
        sent = await self._reaction_action.send(
            emoji=str(verdict.emoji),
            channel=self.name,
            chat_id=event.chat_jid,
            message_id=event.message_id,
            principal=event.sender_id,
            participant_jid=event.participant_jid or event.sender_phone_jid,
        )
        if not sent:
            self._processing_gate.note_ambient_declined(event.message_id)
            return False
        # The reaction is the reply, so the message stays withdrawn from the answer path.
        self._processing_gate.note_ambient_reacted(event.message_id)
        logger.info(
            "ambient_reaction_sent chat={} message_id={} emoji={}",
            event.chat_jid,
            event.message_id,
            sent,
        )
        return True

    async def _maybe_react(self, event: InboundEvent) -> str | None:
        """One reaction instead of an answer, for chats configured with ``react``.

        The answer turn is already withdrawn by the gate (and refused again at admission),
        so this is the whole reply: a small model call picks an approved emoji and the
        effect router sends it with the message as its own lineage.
        """
        if self._reaction_action is None:
            logger.warning(
                "reply_action=react has no reaction path chat={} message_id={}",
                event.chat_jid,
                event.message_id,
            )
            return None
        try:
            emoji = await self._reaction_action(
                channel="whatsapp",
                chat_id=event.chat_jid,
                message_id=event.message_id,
                text=event.text,
                principal=event.sender_id,
                participant_jid=event.participant_jid or event.sender_phone_jid,
            )
        except Exception as exc:
            logger.warning(
                "reaction_action_failed chat={} message_id={} error_type={}",
                event.chat_jid,
                event.message_id,
                type(exc).__name__,
            )
            return None
        logger.info(
            "reaction_action chat={} message_id={} emoji={}",
            event.chat_jid,
            event.message_id,
            emoji or "-",
        )
        return emoji

    def _processing_request(self, event: InboundEvent) -> Any:
        """Canonical base data for the pre-enrichment journal and policy check."""
        from yeoman_gateway.processing.policy import IngestRequest

        message_id = event.message_id or uuid.uuid4().hex
        return IngestRequest(
            event_key=f"whatsapp:{event.chat_jid}:{message_id}",
            event_id=message_id,
            trace_id=f"whatsapp:{event.chat_jid}:{message_id}",
            event=self._to_core_event(event, message_id),
            payload_extra={
                "media_kind": event.media_kind,
                "media_type": event.media_type,
                "origin": "whatsapp_bridge",
            },
        )

    def _to_core_event(self, event: InboundEvent, message_id: str) -> CoreInboundEvent:
        implicit_address = self._is_implicit_address(event)
        """Local identity normalization, done before any vision/transcription call.

        A permission decision must never depend on enriched content, so this conversion
        carries only canonical base data.
        """
        effective_participant = event.sender_phone_jid or event.participant_jid
        effective_sender = (
            _whatsapp_jid_user_token(event.sender_phone_jid)
            if event.sender_phone_jid
            else event.sender_id
        )
        return CoreInboundEvent(
            channel=self.name,
            chat_id=event.chat_jid,
            sender_id=effective_sender or event.sender_id,
            content=event.text,
            message_id=message_id,
            timestamp=_timestamp_to_datetime(event.timestamp),
            participant=effective_participant,
            is_group=event.is_group,
            mentioned_bot=event.mentioned_bot or implicit_address,
            reply_to_bot=event.reply_to_bot,
            reply_to_message_id=event.reply_to_message_id,
            reply_to_participant=event.reply_to_participant,
            reply_to_text=event.reply_to_text,
            raw_metadata={
                **({"implicit_bot_address": "plain_name_request"} if implicit_address else {}),
                "message_id": message_id,
                "is_group": event.is_group,
                "media_kind": event.media_kind,
                "is_voice": event.media_kind == "audio",
                "sender_phone_jid": event.sender_phone_jid,
                # The platform account that observed the message.  It namespaces the
                # identifier, so two accounts never share one binding by accident.
                "account_id": self._processing_account_id,
                "lid_conflict": event.lid_conflict,
            },
        )

    def _is_implicit_address(self, event: InboundEvent) -> bool:
        """Whether a group message addresses the bot by name, without a platform mention.

        The classic pipeline classifies this as well, but only after the fast gate has
        already journalled the event and chosen its thread - so routing would treat
        "Arvid, ..." as ambient and attach it only with a continuity signal (routing spec,
        criterion 5). The canonical event the gate sees carries the mark instead.
        """
        if not event.is_group or event.mentioned_bot or event.reply_to_bot:
            return False
        text = str(event.text or "")
        if not text or not contains_bot_name(text, bot_name_aliases=DEFAULT_BOT_NAME_ALIASES):
            return False
        return looks_like_question_or_request(text)

    async def _ingest_inbound_event(self, event: InboundEvent) -> None:
        if self._is_duplicate(event.chat_jid, event.message_id):
            return

        ambient_candidate = False
        if self._processing_gate is not None:
            verdict = self._processing_gate.admit(self._processing_request(event))
            if verdict is not None and not verdict.denied:
                assignment = getattr(verdict, "assignment", None)
                if assignment is not None and assignment.thread_id:
                    event = replace(
                        event,
                        thread_assignment={
                            "thread_id": assignment.thread_id,
                            "turn_id": assignment.turn_id,
                            "source_message_ids": list(assignment.source_message_ids),
                        },
                    )
                if bool(getattr(verdict, "react", False)):
                    # The answer is withdrawn; the reaction is the whole reply and needs no
                    # turn, no typing indicator and no pipeline run of its own. Messages the
                    # gate only observed stay observed - no acknowledgement per message.
                    reacted = await self._maybe_react(event)
                    event = replace(event, processing_reacted=bool(reacted))
                else:
                    # Unaddressed and the brake passed. The verdict waits until the media is
                    # readable: a voice message has no text before its transcript exists,
                    # and the judge must see what was actually said.
                    ambient_candidate = bool(getattr(verdict, "ambient_candidate", False))
            if verdict is not None and verdict.denied:
                logger.debug(
                    "processing fast gate denied channel=whatsapp chat={} message_id={} "
                    "reason={} decision_id={}",
                    event.chat_jid,
                    event.message_id,
                    verdict.reason,
                    verdict.decision.decision_id if verdict.decision else "-",
                )
                # The owner wants a complete inbound record, so a refused message is
                # archived before the pipeline drops it.
                self._archive_inbound_event(event)
                return

        event = await self._enrich_media_event(event)
        self._index_approved_enrichments(event)
        if ambient_candidate:
            # Only the judge decides whether this becomes an answer. Either way the message
            # is archived and stays context for the chat.
            outcome = await self._maybe_answer_ambient(event)
            if outcome.assignment is not None:
                event = replace(
                    event,
                    thread_assignment={
                        "thread_id": outcome.assignment.thread_id,
                        "turn_id": outcome.assignment.turn_id,
                        "source_message_ids": list(outcome.assignment.source_message_ids),
                    },
                    # The core granted this answer; the classic pipeline must not stop it
                    # with an acknowledgement reaction.
                    processing_answer_granted=True,
                )
            elif outcome.reacted:
                # The judge's reply was a reaction, so no turn opens - but the classic
                # acknowledgement branches must stand down all the same.
                event = replace(event, processing_reacted=True)
        self._record_document_cache_item(event)
        self._archive_inbound_event(event)
        self._sync_chat_registry(event)

        effective_debounce = (
            self.config.debounce_media_ms
            if event.media_kind is not None
            else self.config.debounce_ms
        )
        if effective_debounce <= 0:
            await self._publish_event(event)
            return

        key = f"{event.chat_jid}:{event.sender_id}"
        event_bytes = self._debounce_event_bytes(event)
        direct_publish: asyncio.Task[None] | None = None
        lock = self._debounce_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if (
                key not in self._debounce_buffers
                and len(self._debounce_buffers) >= self._max_debounce_buckets
            ):
                self._debounce_overflow += 1
                if self._debounce_overflow == 1 or self._debounce_overflow % 100 == 0:
                    logger.warning(
                        "WhatsApp debounce bucket overflow: "
                        f"direct_batches={self._debounce_overflow} max={self._max_debounce_buckets}"
                    )
                direct_publish = self._schedule_debounce_publish_locked(key, [event])
            else:
                bucket = self._debounce_buffers.get(key)
                bucket_bytes = self._debounce_buffer_bytes.get(key, 0)
                if bucket and (
                    len(bucket) >= self._debounce_max_items
                    or bucket_bytes + event_bytes > self._debounce_max_bytes
                    or event_bytes > self._debounce_max_bytes
                ):
                    self._schedule_debounce_publish_locked(
                        key, self._detach_debounce_batch_locked(key)
                    )

                if event_bytes > self._debounce_max_bytes:
                    # A single oversized event is published directly, after
                    # any buffered predecessor has been chained first.
                    direct_publish = self._schedule_debounce_publish_locked(key, [event])
                else:
                    bucket = self._debounce_buffers.setdefault(key, [])
                    bucket.append(event)
                    self._debounce_buffer_bytes[key] = (
                        self._debounce_buffer_bytes.get(key, 0) + event_bytes
                    )
                    self._debounce_delays[key] = min(
                        self._debounce_delays.get(key, effective_debounce),
                        effective_debounce,
                    )
                    self._schedule_debounce_timer_locked(
                        key, self._debounce_delays[key]
                    )

        if direct_publish is not None:
            await direct_publish

    def _on_inbound_task_done(self, task: asyncio.Task[None]) -> None:
        self._inbound_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"WhatsApp inbound task failed: {exc}")

    @staticmethod
    def _debounce_event_bytes(event: InboundEvent) -> int:
        # ``repr`` includes every slotted field, including relation/media
        # metadata, so the ceiling covers the retained object rather than only
        # its visible text.
        return max(1, len(repr(event).encode("utf-8")))

    def _detach_debounce_batch_locked(self, key: str) -> list[InboundEvent]:
        timer = self._debounce_tasks.pop(key, None)
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        self._debounce_buffer_bytes.pop(key, None)
        self._debounce_delays.pop(key, None)
        return self._debounce_buffers.pop(key, [])

    def _schedule_debounce_timer_locked(self, key: str, delay_ms: float) -> None:
        existing = self._debounce_tasks.get(key)
        if existing is not None and existing is not asyncio.current_task():
            existing.cancel()
        timer = asyncio.create_task(self._flush_debounce_bucket(key, delay_ms))
        self._debounce_tasks[key] = timer
        timer.add_done_callback(
            lambda completed, timer_key=key: self._on_debounce_timer_done(
                timer_key, completed
            )
        )

    def _schedule_debounce_publish_locked(
        self, key: str, events: list[InboundEvent]
    ) -> asyncio.Task[None]:
        if not events:
            raise RuntimeError("cannot schedule an empty debounce batch")
        sequence = self._debounce_batch_sequences.get(key, 0) + 1
        self._debounce_batch_sequences[key] = sequence
        previous = self._debounce_tails.get(key)
        publisher = asyncio.create_task(
            self._publish_debounce_batch_after(key, sequence, events, previous)
        )
        self._debounce_tails[key] = publisher
        self._debounce_publish_tasks.add(publisher)
        publisher.add_done_callback(
            lambda completed, publish_key=key, publish_sequence=sequence: self._on_debounce_publish_done(
                publish_key, publish_sequence, completed
            )
        )
        return publisher

    def _on_debounce_timer_done(self, key: str, task: asyncio.Task[None]) -> None:
        if self._debounce_tasks.get(key) is task:
            self._debounce_tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning(
                "WhatsApp debounce timer failed key={} error_type={}",
                key,
                type(error).__name__,
            )

    def _on_debounce_publish_done(
        self, key: str, sequence: int, task: asyncio.Task[None]
    ) -> None:
        self._debounce_publish_tasks.discard(task)
        if self._debounce_tails.get(key) is task:
            self._debounce_tails.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning(
                "WhatsApp debounce publication failed key={} sequence={} error_type={}",
                key,
                sequence,
                type(error).__name__,
            )

    async def _flush_debounce_bucket(self, key: str, delay_ms: float) -> None:
        try:
            await asyncio.sleep(delay_ms / 1000.0)
        except asyncio.CancelledError:
            return

        lock = self._debounce_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self._debounce_tasks.get(key) is not asyncio.current_task():
                return
            events = self._detach_debounce_batch_locked(key)
            if events:
                self._schedule_debounce_publish_locked(key, events)

    async def _publish_debounce_batch_after(
        self,
        key: str,
        sequence: int,
        events: list[InboundEvent],
        previous: asyncio.Task[None] | None,
    ) -> None:
        del key, sequence
        if previous is not None:
            try:
                await previous
            except asyncio.CancelledError:
                if not previous.cancelled():
                    raise
            except Exception:
                # The predecessor's done callback retrieved and logged the
                # failure; the serialized successor must still continue.
                pass
        await self._publish_debounce_batch(events)

    async def _publish_debounce_batch(self, events: list[InboundEvent]) -> None:
        if len(events) == 1:
            await self._publish_event(events[0])
            return

        combined_text = "\n".join(event.text for event in events if event.text).strip()
        last = events[-1]
        media_source = next((e for e in events if e.media_kind is not None), last)
        mentioned_jids = sorted({jid for event in events for jid in event.mentioned_jids})
        reply_to_message_id = next(
            (event.reply_to_message_id for event in reversed(events) if event.reply_to_message_id),
            None,
        )
        reply_to_participant = next(
            (
                event.reply_to_participant
                for event in reversed(events)
                if event.reply_to_participant
            ),
            None,
        )
        reply_to_text = next(
            (event.reply_to_text for event in reversed(events) if event.reply_to_text),
            None,
        )

        merged = InboundEvent(
            message_id=last.message_id,
            chat_jid=last.chat_jid,
            participant_jid=last.participant_jid,
            sender_id=last.sender_id,
            sender_phone_jid=last.sender_phone_jid,
            sender_name=last.sender_name,
            is_group=last.is_group,
            text=combined_text or last.text,
            timestamp=last.timestamp,
            mentioned_jids=mentioned_jids,
            mentioned_bot=any(event.mentioned_bot for event in events),
            reply_to_bot=any(event.reply_to_bot for event in events),
            reply_to_message_id=reply_to_message_id,
            reply_to_participant=reply_to_participant,
            reply_to_text=reply_to_text,
            media_kind=media_source.media_kind,
            media_type=media_source.media_type,
            media_file_name=media_source.media_file_name,
            media_path=media_source.media_path,
            media_bytes=media_source.media_bytes,
            media_description=media_source.media_description,
            voice_transcript=media_source.voice_transcript,
            source_event_ids=tuple(
                dict.fromkeys(
                    source_id
                    for event in events
                    for source_id in event.source_ids
                    if source_id
                )
            ),
        )

        await self._publish_event(merged)

    def _record_document_cache_item(self, event: InboundEvent) -> None:
        if self._document_cache is None or not self.config.media.enabled:
            return
        if event.media_kind not in {"document", "image"} or not event.media_path:
            return

        validated_path = self._media_storage.validate_incoming_path(event.media_path)
        if validated_path is None:
            logger.warning(
                "Skipping WhatsApp media cache record due to invalid media path: {}",
                event.media_path,
            )
            return
        try:
            size_bytes = validated_path.stat().st_size
        except OSError:
            size_bytes = event.media_bytes

        try:
            self._document_cache.record_media_item(
                channel=self.name,
                chat_id=event.chat_jid,
                message_id=event.message_id,
                sender_id=event.sender_phone_jid or event.sender_id,
                sender_name=event.sender_name,
                kind=event.media_kind,
                mime_type=event.media_type,
                file_name=event.media_file_name,
                local_path=validated_path,
                size_bytes=size_bytes,
                timestamp=event.timestamp,
                retention_days=self.config.media.retention_days,
            )
        except Exception as e:
            logger.warning(
                "Failed to cache WhatsApp media metadata {}: {}",
                event.message_id,
                e,
            )

    def _archive_inbound_event(self, event: InboundEvent) -> None:
        if self.inbound_archive is None:
            return
        try:
            self.inbound_archive.record_inbound(
                channel=self.name,
                chat_id=event.chat_jid,
                message_id=event.message_id,
                participant=event.sender_phone_jid or event.participant_jid,
                sender_id=(
                    _whatsapp_jid_user_token(event.sender_phone_jid)
                    if event.sender_phone_jid
                    else event.sender_id
                ) or event.sender_id,
                sender_name=event.sender_name,
                text=event.text,
                timestamp=event.timestamp,
                reply_to_message_id=event.reply_to_message_id,
            )
            # Seed quoted target text when available so reply lookups can work
            # even if the original inbound message was not captured by this runtime.
            if event.reply_to_message_id and event.reply_to_text:
                self.inbound_archive.record_inbound(
                    channel=self.name,
                    chat_id=event.chat_jid,
                    message_id=event.reply_to_message_id,
                    participant=event.reply_to_participant,
                    sender_id=None,
                    text=event.reply_to_text,
                    timestamp=event.timestamp,
                )
        except Exception as e:
            logger.warning(f"Failed to archive inbound WhatsApp message {event.message_id}: {e}")

    def _sync_chat_registry(self, event: InboundEvent) -> None:
        if self._chat_registry is None:
            return
        try:
            self._chat_registry.register_chat(
                channel=self.name,
                chat_id=event.chat_jid,
                chat_type="group" if event.is_group else "dm",
                readable_name=(
                    self._policy_comment_for_chat(event.chat_jid) if event.is_group else None
                ),
            )
        except Exception as e:
            logger.warning(f"Failed to update chat registry for {event.chat_jid}: {e}")

    def _policy_comment_for_chat(self, chat_id: str) -> str | None:
        try:
            from yeoman_gateway.policy.loader import load_policy

            policy = load_policy()
            channel_policy = policy.channels.get(self.name)
            if channel_policy is None:
                return None
            override = channel_policy.chats.get(str(chat_id))
            if override is None:
                return None
            comment = str(override.comment or "").strip()
            return comment or None
        except Exception:
            return None

    async def _enrich_media_event(self, event: InboundEvent) -> InboundEvent:
        event = await self._enrich_primary_media_event(event)
        event = await self._enrich_quoted_image_event(event)
        return event

    def _index_approved_enrichments(self, event: InboundEvent) -> None:
        """Forward existing, bounded media text to the canonical FTS projection."""
        indexer = getattr(self._processing_signals, "index_enrichments", None)
        if not callable(indexer):
            return
        enrichments: list[dict[str, object]] = []
        if event.voice_transcript:
            enrichments.append(
                {"kind": "voice_transcript", "text": event.voice_transcript, "approved": True}
            )
        if event.media_description:
            kind = {
                "image": "image_description",
                "video": "video_description",
                "sticker": "sticker_description",
            }.get(event.media_kind or "", "media_description")
            enrichments.append(
                {"kind": kind, "text": event.media_description, "approved": True}
            )
        if enrichments:
            indexer(event.message_id, enrichments)

    async def _enrich_quoted_image_event(self, event: InboundEvent) -> InboundEvent:
        if (
            not self.config.media.enabled
            or not self.config.media.describe_images
            or event.reply_to_media_kind != "image"
            or not event.reply_to_media_path
            or self._vision_describer is None
            or self._model_router is None
        ):
            return event
        if event.reply_to_text and "[image_description]" in event.reply_to_text:
            return event

        validated_path = self._media_storage.validate_incoming_path(event.reply_to_media_path)
        if validated_path is None:
            logger.warning(
                "Skipping WhatsApp quoted-image description due to invalid media path: {}",
                event.reply_to_media_path,
            )
            return event
        try:
            size_bytes = validated_path.stat().st_size
        except OSError:
            return event
        max_bytes = max(1, int(self.config.media.max_image_bytes_mb)) * 1024 * 1024
        if size_bytes > max_bytes:
            logger.info(
                "Skipping WhatsApp quoted-image description due to size limit: path={} bytes={} limit={}",
                validated_path,
                size_bytes,
                max_bytes,
            )
            return event

        try:
            profile = self._model_router.resolve("vision.describe_image", channel=self.name)
        except KeyError as e:
            logger.warning(
                f"Skipping WhatsApp quoted-image description due to missing route: {e}"
            )
            return event

        try:
            description = await self._vision_describer.describe(validated_path, profile)
        except Exception as e:
            logger.warning(
                "WhatsApp quoted-image description failed {}: {}",
                e.__class__.__name__,
                e,
            )
            return event
        if not description:
            return event

        base = (event.reply_to_text or "").strip()
        if not base or base == "[Image]":
            caption = "[Image]"
        elif base.startswith("[Image] "):
            caption = base
        else:
            caption = f"[Image] {base}"
        enriched_reply_text = f"{caption}\n[image_description] {description}"
        return replace(event, reply_to_text=enriched_reply_text)

    async def _enrich_primary_media_event(self, event: InboundEvent) -> InboundEvent:
        if not self.config.media.enabled:
            return event

        if event.media_kind == "image":
            if (
                not self.config.media.describe_images
                or not event.media_path
                or self._vision_describer is None
                or self._model_router is None
            ):
                return event

            validated_path = self._media_storage.validate_incoming_path(event.media_path)
            if validated_path is None:
                logger.warning(
                    "Skipping WhatsApp image description due to invalid media path: {}",
                    event.media_path,
                )
                return event
            try:
                size_bytes = validated_path.stat().st_size
            except OSError:
                return event
            max_bytes = max(1, int(self.config.media.max_image_bytes_mb)) * 1024 * 1024
            if size_bytes > max_bytes:
                logger.info(
                    "Skipping WhatsApp image description due to size limit: path={} bytes={} limit={}",
                    validated_path,
                    size_bytes,
                    max_bytes,
                )
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                profile = self._model_router.resolve("vision.describe_image", channel=self.name)
            except KeyError as e:
                logger.warning(f"Skipping WhatsApp image description due to missing route: {e}")
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                description = await self._vision_describer.describe(validated_path, profile)
            except Exception as e:
                logger.warning("WhatsApp image description failed {}: {}", e.__class__.__name__, e)
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            if not description:
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)
            if "[image_description]" in event.text:
                enriched_text = event.text
            else:
                enriched_text = f"{event.text}\n[image_description] {description}"
            return replace(
                event,
                text=enriched_text,
                media_path=str(validated_path),
                media_bytes=size_bytes,
                media_description=description,
            )

        if event.media_kind == "audio":
            if (
                not self.config.media.transcribe_audio
                or not event.media_path
                or self._model_router is None
            ):
                return event

            validated_path = self._media_storage.validate_incoming_path(event.media_path)
            if validated_path is None:
                logger.warning(
                    "Skipping WhatsApp audio transcription due to invalid media path: {}",
                    event.media_path,
                )
                return event
            try:
                size_bytes = validated_path.stat().st_size
            except OSError:
                return event
            max_bytes = max(1, int(self.config.media.max_audio_bytes_mb)) * 1024 * 1024
            if size_bytes > max_bytes:
                logger.info(
                    "Skipping WhatsApp audio transcription due to size limit: path={} bytes={} limit={}",
                    validated_path,
                    size_bytes,
                    max_bytes,
                )
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                profile = self._model_router.resolve("asr.transcribe_audio", channel=self.name)
            except KeyError as e:
                logger.warning(f"Skipping WhatsApp audio transcription due to missing route: {e}")
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            transcript = None
            try:
                transcript = await self._asr_transcriber.transcribe(validated_path, profile)
            except Exception as e:
                logger.warning(
                    "WhatsApp audio transcription failed {}: {}", e.__class__.__name__, e
                )

            if not transcript:
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            if self.config.media.delete_audio_after_transcription:
                with contextlib.suppress(OSError):
                    validated_path.unlink()

            return replace(
                event,
                text=transcript,
                media_path=str(validated_path),
                media_bytes=size_bytes,
                voice_transcript=transcript,
            )

        if event.media_kind == "video":
            if (
                not self.config.media.describe_videos
                or not event.media_path
                or self._vision_describer is None
                or self._model_router is None
            ):
                return event

            validated_path = self._media_storage.validate_incoming_path(event.media_path)
            if validated_path is None:
                logger.warning(
                    "Skipping WhatsApp video description due to invalid media path: {}",
                    event.media_path,
                )
                return event
            try:
                size_bytes = validated_path.stat().st_size
            except OSError:
                return event
            max_bytes = max(1, int(self.config.media.max_video_bytes_mb)) * 1024 * 1024
            if size_bytes > max_bytes:
                logger.info(
                    "Skipping WhatsApp video description due to size limit: path={} bytes={} limit={}",
                    validated_path,
                    size_bytes,
                    max_bytes,
                )
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                profile = self._model_router.resolve("vision.describe_video", channel=self.name)
            except KeyError as e:
                logger.warning(f"Skipping WhatsApp video description due to missing route: {e}")
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                description = await self._vision_describer.describe_video(
                    validated_path,
                    profile,
                    frame_count=self.config.media.video_frame_count,
                )
            except Exception as e:
                logger.warning("WhatsApp video description failed {}: {}", e.__class__.__name__, e)
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            if self.config.media.delete_video_after_description:
                with contextlib.suppress(OSError):
                    validated_path.unlink()

            if not description:
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)
            if "[video_description]" in event.text:
                enriched_text = event.text
            else:
                enriched_text = f"{event.text}\n[video_description] {description}"
            return replace(
                event,
                text=enriched_text,
                media_path=str(validated_path),
                media_bytes=size_bytes,
                media_description=description,
            )

        if event.media_kind == "sticker":
            if (
                not self.config.media.describe_stickers
                or not event.media_path
                or self._vision_describer is None
                or self._model_router is None
            ):
                return event

            validated_path = self._media_storage.validate_incoming_path(event.media_path)
            if validated_path is None:
                logger.warning(
                    "Skipping WhatsApp sticker description due to invalid media path: {}",
                    event.media_path,
                )
                return event
            try:
                size_bytes = validated_path.stat().st_size
            except OSError:
                return event

            try:
                profile = self._model_router.resolve(
                    "vision.describe_image", channel=self.name
                )
            except KeyError as e:
                logger.warning(f"Skipping WhatsApp sticker description due to missing route: {e}")
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            try:
                description = await self._vision_describer.describe(validated_path, profile)
            except Exception as e:
                logger.warning(
                    "WhatsApp sticker description failed {}: {}", e.__class__.__name__, e
                )
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)

            if self.config.media.delete_sticker_after_description:
                with contextlib.suppress(OSError):
                    validated_path.unlink()

            if not description:
                return replace(event, media_path=str(validated_path), media_bytes=size_bytes)
            if "[sticker_description]" in event.text:
                enriched_text = event.text
            else:
                enriched_text = f"{event.text}\n[sticker_description] {description}"
            return replace(
                event,
                text=enriched_text,
                media_path=str(validated_path),
                media_bytes=size_bytes,
                media_description=description,
            )

        return event

    async def _run_media_cleanup_once(self) -> None:
        if not self.config.media.enabled:
            return
        try:
            deleted = await asyncio.to_thread(
                self._media_storage.cleanup_expired,
                self.name,
                self.config.media.retention_days,
            )
            if deleted > 0:
                logger.info(
                    "WhatsApp media cleanup removed {} files (retention={}d)",
                    deleted,
                    self.config.media.retention_days,
                )
        except Exception as e:
            logger.warning("WhatsApp media cleanup failed {}: {}", e.__class__.__name__, e)

    async def _media_cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(3600)
                await self._run_media_cleanup_once()
            except asyncio.CancelledError:
                break

    async def _publish_event(self, event: InboundEvent) -> None:
        is_voice = event.media_kind == "audio"
        media_for_assistant: list[str] = []
        if (
            self.config.media.pass_image_to_assistant
            and event.media_path
            and event.media_kind == "image"
        ):
            media_for_assistant = [event.media_path]

        # Prefer phone JID over LID for participant/sender — phone JIDs
        # are required for WhatsApp @-mentions to render with contact names.
        effective_participant = event.sender_phone_jid or event.participant_jid
        effective_sender = (
            _whatsapp_jid_user_token(event.sender_phone_jid)
            if event.sender_phone_jid
            else event.sender_id
        )

        await self._handle_message(
            sender_id=effective_sender or event.sender_id,
            chat_id=event.chat_jid,
            content=event.text,
            media=media_for_assistant,
            metadata={
                "message_id": event.message_id,
                "timestamp": event.timestamp,
                "chat": event.chat_jid,
                "participant": effective_participant,
                "participant_lid": event.participant_jid if event.sender_phone_jid else None,
                "sender_phone_jid": event.sender_phone_jid,
                "lid_conflict": event.lid_conflict,
                "sender": effective_sender or event.sender_id,
                "sender_name": event.sender_name,
                "is_group": event.is_group,
                "mentioned_bot": event.mentioned_bot,
                "reply_to_bot": event.reply_to_bot,
                "reply_to": event.reply_to_message_id,
                "reply_to_message_id": event.reply_to_message_id,
                "reply_to_participant": event.reply_to_participant,
                "reply_to_text": event.reply_to_text,
                "mentioned_jids": event.mentioned_jids,
                "processing_reacted": event.processing_reacted,
                "processing_answer_granted": event.processing_answer_granted,
                "media_path": event.media_path,
                "media_bytes": event.media_bytes,
                "media_type": event.media_type,
                "media_file_name": event.media_file_name,
                "media_kind": event.media_kind,
                "media_description": event.media_description,
                "is_voice": is_voice,
                "voice_transcript": event.voice_transcript,
                "source_event_ids": list(event.source_ids),
                **(event.thread_assignment or {}),
                "thread_source_message_ids": list(
                    (event.thread_assignment or {}).get("source_message_ids") or []
                ),
            },
        )

    async def _start_typing(self, chat_jid: str, *, state: str = "composing") -> None:
        if not chat_jid:
            return
        await self._stop_typing(chat_jid, send_paused=False)
        self._typing_tasks[chat_jid] = asyncio.create_task(self._typing_loop(chat_jid, state=state))

    async def _stop_typing(self, chat_jid: str, *, send_paused: bool = True) -> None:
        task = self._typing_tasks.pop(chat_jid, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if send_paused:
            await self._send_presence(chat_jid, "paused")

    async def _typing_loop(self, chat_jid: str, *, state: str = "composing") -> None:
        task = asyncio.current_task()
        started_at = asyncio.get_running_loop().time()
        while (
            self._running
            and self._connected
            and asyncio.get_running_loop().time() - started_at < TYPING_MAX_DURATION_SECONDS
        ):
            await self._send_presence(chat_jid, state)
            await asyncio.sleep(TYPING_LOOP_INTERVAL_SECONDS)

        if self._typing_tasks.get(chat_jid) is task:
            self._typing_tasks.pop(chat_jid, None)

    async def _send_presence(self, chat_jid: str, state: str) -> None:
        if not self._connected or not self._presence_supported:
            return

        payload: dict[str, Any] = {"state": state}
        if chat_jid:
            payload["chatJid"] = chat_jid
        try:
            await self._send_command("presence_update", payload, timeout_seconds=6.0)
        except BridgeProtocolError as e:
            if e.code == "ERR_UNSUPPORTED":
                self._presence_supported = False
                if not self._presence_unsupported_logged:
                    self._presence_unsupported_logged = True
                    logger.warning(
                        "WhatsApp bridge presence_update unsupported; typing indicator disabled until restart"
                    )
                return
            logger.debug(f"WhatsApp presence update failed ({state}) for {chat_jid}: {e}")
        except Exception as e:
            logger.debug(
                "WhatsApp presence update failed ({}) for {}: {} {}",
                state,
                chat_jid,
                e.__class__.__name__,
                e,
            )

    def _is_duplicate(self, chat_jid: str, message_id: str) -> bool:
        now = asyncio.get_running_loop().time()
        dedupe_key = f"{chat_jid}:{message_id}"

        if now >= self._next_dedupe_cleanup_at:
            for key, expires_at in list(self._recent_message_ids.items()):
                if expires_at <= now:
                    self._recent_message_ids.pop(key, None)
            self._next_dedupe_cleanup_at = now + DEDUPE_CLEANUP_INTERVAL_SECONDS

        if dedupe_key in self._recent_message_ids:
            return True

        if len(self._recent_message_ids) >= self._max_dedupe_entries:
            oldest = next(iter(self._recent_message_ids), None)
            if oldest is not None:
                self._recent_message_ids.pop(oldest, None)
                self._dedupe_evictions += 1
                if self._dedupe_evictions == 1 or self._dedupe_evictions % 500 == 0:
                    logger.warning(
                        "WhatsApp dedupe cache overflow: "
                        f"evictions={self._dedupe_evictions} max={self._max_dedupe_entries}"
                    )

        self._recent_message_ids[dedupe_key] = now + DEDUPE_TTL_SECONDS
        return False

    async def _send_command(
        self,
        command_type: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        token: str | None = None,
    ) -> dict[str, Any]:
        if not self._ws:
            raise RuntimeError("Bridge websocket not connected")

        request_id = uuid.uuid4().hex
        envelope = {
            "version": PROTOCOL_VERSION,
            "type": command_type,
            "token": token or self._require_token(),
            "requestId": request_id,
            "accountId": "default",
            "payload": payload,
        }
        encoded = json.dumps(envelope)
        envelope_bytes = len(encoded.encode("utf-8"))
        max_payload_bytes = max(1, int(self.config.max_payload_bytes))
        if envelope_bytes > max_payload_bytes:
            raise ValueError(
                f"Bridge command payload too large: {envelope_bytes} > {max_payload_bytes} bytes"
            )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        try:
            logger.debug(
                "WhatsApp bridge command start type={} request_id={} timeout_s={} payload={}",
                command_type,
                request_id,
                timeout_seconds,
                self._summarize_command_payload(command_type, payload),
            )
            async with self._send_lock:
                await self._ws.send(encoded)
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            logger.debug(
                "WhatsApp bridge command ok type={} request_id={}",
                command_type,
                request_id,
            )
            return result
        except Exception as e:
            logger.warning(
                "WhatsApp bridge command failed type={} request_id={} error={} {}",
                command_type,
                request_id,
                e.__class__.__name__,
                e,
            )
            raise
        finally:
            self._pending.pop(request_id, None)

    @staticmethod
    def _summarize_command_payload(command_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "to": "[REDACTED]" if payload.get("to") else None
        }
        if command_type == "send_text":
            summary["text_len"] = len(str(payload.get("text") or ""))
            summary["reply_to"] = bool(payload.get("replyToMessageId"))
            summary["mentions"] = len(payload.get("mentions") or [])
            return summary
        if command_type == "send_media":
            summary["has_media_path"] = bool(payload.get("mediaPath"))
            summary["mime_type"] = payload.get("mimeType")
            summary["file_name"] = payload.get("fileName")
            summary["caption_len"] = len(str(payload.get("caption") or ""))
            summary["reply_to"] = bool(payload.get("replyToMessageId"))
            summary["mentions"] = len(payload.get("mentions") or [])
            return summary
        if command_type == "presence_update":
            summary["state"] = payload.get("state")
            summary["chat_jid"] = "[REDACTED]" if payload.get("chatJid") else None
            return summary
        if command_type == "react":
            summary["chat_jid"] = "[REDACTED]" if payload.get("chatJid") else None
            summary["message_id"] = payload.get("messageId")
            summary["emoji"] = payload.get("emoji")
            return summary
        if command_type == "delete_message":
            summary["chat_jid"] = "[REDACTED]" if payload.get("chatJid") else None
            summary["message_id"] = payload.get("messageId")
            return summary
        if command_type == "forward_message":
            summary["source_chat_jid"] = "[REDACTED]" if payload.get("sourceChatJid") else None
            summary["source_message_id"] = "[REDACTED]" if payload.get("sourceMessageId") else None
            return summary
        return summary

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def _resolve_outbound_mentions(
        self, text: str, metadata: dict[str, object] | None
    ) -> list[str]:
        if not text or not isinstance(metadata, dict):
            return []

        explicit_mentions: list[str] = []
        explicit_seen: set[str] = set()
        for raw in self._string_list(metadata.get("mentions")):
            normalized = _normalize_whatsapp_jid(raw)
            if not normalized or normalized in explicit_seen:
                continue
            explicit_seen.add(normalized)
            explicit_mentions.append(normalized)
        if explicit_mentions:
            return explicit_mentions

        candidates_by_full: dict[str, str] = {}
        candidates_by_token: dict[str, str] = {}
        for raw in self._string_list(metadata.get("mention_candidates")):
            normalized = _normalize_whatsapp_jid(raw)
            if not normalized:
                continue
            candidates_by_full.setdefault(normalized.lower(), normalized)
            token = _whatsapp_jid_user_token(normalized)
            if not token:
                continue
            lowered = token.lower()
            candidates_by_token.setdefault(lowered, normalized)
            if token.startswith("+"):
                candidates_by_token.setdefault(token[1:].lower(), normalized)
            elif token.isdigit():
                candidates_by_token.setdefault(f"+{token}".lower(), normalized)

        resolved: list[str] = []
        seen: set[str] = set()
        for match in _WHATSAPP_MENTION_RE.finditer(text):
            raw_token = str(match.group(1) or "").strip()
            raw_token = raw_token.rstrip(_WHATSAPP_MENTION_TOKEN_TRAILING)
            if not raw_token:
                continue
            candidate: str | None = None
            if "@" in raw_token:
                candidate = candidates_by_full.get(_normalize_whatsapp_jid(raw_token).lower())
            else:
                candidate = candidates_by_token.get(raw_token.lower())
                # If token is all digits but not in candidates, pass it through
                # as a LID JID — the bridge will translate via its LID→phone cache.
                if not candidate and raw_token.isdigit() and len(raw_token) >= 10:
                    candidate = f"{raw_token}@lid"
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            resolved.append(candidate)

        return resolved

    async def _wait_connected_for_send(self, timeout_seconds: float) -> bool:
        if self._connected and self._ws is not None:
            return True
        timeout = max(0.1, float(timeout_seconds))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._connected and self._ws is not None:
                return True
            await asyncio.sleep(0.1)
        return self._connected and self._ws is not None

    @staticmethod
    def _is_retryable_send_error(err: Exception) -> bool:
        if isinstance(err, TimeoutError):
            return True
        if isinstance(err, OSError):
            return True
        if isinstance(err, BridgeProtocolError):
            if err.retryable:
                return True
            return err.code in {
                "ERR_INTERNAL",
                "ERR_QUEUE_OVERFLOW",
            }
        text = str(err).lower()
        return (
            "not connected" in text
            or "bridge websocket not connected" in text
            or "connection closed" in text
        )

    @staticmethod
    def _send_attempts(metadata: Any) -> int:
        """Transport attempts for one outbound message.

        An effect-delivered message gets exactly one attempt: a timeout after a possible
        dispatch proves nothing, and a second send command below the effect gateway would
        be an unauthorized retry of an unknown action (spec R06, R07). Legacy traffic
        keeps its existing retry behaviour, and safe pre-dispatch reconnects still happen
        through the connection wait that precedes the attempt.
        """
        if isinstance(metadata, dict) and metadata.get("processing_effect"):
            return 1
        return SEND_MAX_ATTEMPTS

    async def _send_command_with_retry(
        self,
        command_type: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        max_attempts: int,
    ) -> dict[str, Any]:
        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                return await self._send_command(
                    command_type,
                    payload,
                    timeout_seconds=timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if attempt >= attempts or not self._is_retryable_send_error(err):
                    raise
                delay = min(
                    3.0,
                    SEND_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                )
                logger.warning(
                    "WhatsApp {} failed (attempt {}/{}): {}. retrying in {:.2f}s",
                    command_type,
                    attempt,
                    attempts,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                await self._wait_connected_for_send(timeout_seconds=delay + 1.0)
        raise RuntimeError(f"Failed to send command after retries: {command_type}")

    def _resolve_pending(self, request_id: str, payload: dict[str, Any]) -> None:
        future = self._pending.get(request_id)
        if not future or future.done():
            return

        ok = bool(payload.get("ok"))
        if ok:
            result = payload.get("result")
            future.set_result(result if isinstance(result, dict) else {})
            return

        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "ERR_INTERNAL")
        message = str(error.get("message") or "Bridge command failed")
        retryable = bool(error.get("retryable", False))
        future.set_exception(BridgeProtocolError(code, message, retryable))

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self._pending.clear()

    def _compute_backoff_ms(self, attempt: int) -> int:
        initial = max(100, self.config.reconnect_initial_ms)
        factor = max(1.1, self.config.reconnect_factor)
        raw = initial * (factor ** max(0, attempt - 1))
        capped = min(float(self.config.reconnect_max_ms), raw)
        jitter_ratio = max(0.0, min(1.0, self.config.reconnect_jitter))
        jitter = capped * jitter_ratio
        low = max(100.0, capped - jitter)
        high = capped + jitter
        return int(random.uniform(low, high))


#: The named envelopes the bridge wraps its command results in.
_BRIDGE_ENVELOPE_KEYS = ("sent", "forwarded", "reacted", "deleted", "presence")


def _receipt_from_bridge(
    result: Any, *, target_message_id: str | None = None
) -> dict[str, Any] | None:
    """Normalise a bridge reply into the receipt the effect layer persists.

    ``send_text``/``send_media`` report ``messageId``; ``react`` reports the reaction's own
    id plus the message it was applied to. Anything unrecognised yields ``None`` so the
    effect stays locally accepted without a provider reference.
    """
    if not isinstance(result, dict):
        return None
    # The bridge wraps every command result in a named envelope (``{"sent": {...}}``,
    # ``{"reacted": {...}}``). Reading the id from the outer object found nothing, so no
    # provider id ever reached the effect layer: "sent" stayed unproven, no transport
    # receipt was recorded, and a reply to one of our own messages could not be resolved.
    payload_source = result
    for envelope in _BRIDGE_ENVELOPE_KEYS:
        nested = result.get(envelope)
        if isinstance(nested, dict):
            payload_source = nested
            break
    # A result may name the message it acted on and the one it created; the created id is
    # ours and is the provider reference. ``send_text`` reports only ``messageId``, a
    # reaction reports both, a delete only the target.
    provider_message_id = payload_source.get("outboundMessageId") or payload_source.get(
        "messageId"
    )
    payload: dict[str, Any] = {}
    if provider_message_id:
        payload["provider_message_id"] = str(provider_message_id)
    client_message_id = payload_source.get("clientMessageId")
    if client_message_id:
        payload["client_message_id"] = str(client_message_id)
    if target_message_id:
        payload["target_message_id"] = str(target_message_id)
    return payload or None
