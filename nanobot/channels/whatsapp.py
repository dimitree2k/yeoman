"""WhatsApp channel implementation using strict bridge protocol v2."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
from nanobot.config.schema import WhatsAppConfig

if TYPE_CHECKING:
    from nanobot.storage.inbound_archive import InboundArchive

PROTOCOL_VERSION = 2
DEDUPE_TTL_SECONDS = 20 * 60
DEDUPE_CLEANUP_INTERVAL_SECONDS = 30
TYPING_LOOP_INTERVAL_SECONDS = 4.0
TYPING_MAX_DURATION_SECONDS = 45.0


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


class WhatsAppChannel(BaseChannel):
    """WhatsApp channel backed by the Node.js bridge protocol v2."""

    name = "whatsapp"

    def __init__(
        self,
        config: WhatsAppConfig,
        bus: MessageBus,
        inbound_archive: "InboundArchive | None" = None,
    ):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self.inbound_archive = inbound_archive
        self._ws: Any | None = None
        self._connected = False
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recent_message_ids: dict[str, float] = {}
        self._debounce_buffers: dict[str, list[InboundEvent]] = {}
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._reconnect_attempts = 0
        self._repair_attempted = False
        self._next_dedupe_cleanup_at = 0.0
        self._max_dedupe_entries = max(1, int(self.config.max_dedupe_entries))
        self._max_debounce_buckets = max(1, int(self.config.max_debounce_buckets))
        self._dedupe_evictions = 0
        self._debounce_overflow = 0
        self._presence_supported = True
        self._presence_unsupported_logged = False
        self._runtime = WhatsAppRuntimeManager()

    def _require_token(self) -> str:
        token = (self.config.bridge_token or "").strip()
        if not token:
            raise RuntimeError(
                "channels.whatsapp.bridgeToken is required for protocol v2"
            )
        return token

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.resolved_bridge_url
        token = self._require_token()
        startup_timeout_s = max(1.0, self.config.bridge_startup_timeout_ms / 1000.0)

        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")

        self._running = True
        try:
            await asyncio.to_thread(self._runtime.ensure_runtime)
        except Exception as e:
            logger.error(f"WhatsApp runtime preparation failed: {e}")
            self._running = False
            return

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
                    self._reader_task = asyncio.create_task(self._read_loop())

                    try:
                        await self._verify_bridge_health(token, timeout_seconds=startup_timeout_s)
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

                    self._connected = True
                    self._repair_attempted = False
                    self._reconnect_attempts = 0
                    logger.info("Connected to WhatsApp bridge (protocol v2)")

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
                self._ws = None
                if self._reader_task:
                    self._reader_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._reader_task
                    self._reader_task = None
                self._fail_pending("Bridge connection closed")

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False

        for chat_id in list(self._typing_tasks):
            await self._stop_typing(chat_id)

        for task in list(self._inbound_tasks):
            task.cancel()
        self._inbound_tasks.clear()

        for task in self._debounce_tasks.values():
            task.cancel()
        self._debounce_tasks.clear()
        self._debounce_buffers.clear()

        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._fail_pending("Channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        await self._stop_typing(msg.chat_id)

        await self._send_command(
            "send_text",
            {
                "to": msg.chat_id,
                "text": msg.content,
            },
            timeout_seconds=20.0,
        )

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
                    "Likely outdated bridge build; run `nanobot channels bridge restart` to refresh."
                )
            else:
                logger.warning(f"Unexpected bridge protocol version: {version!r}")
            return

        msg_type = data.get("type")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if msg_type == "response":
            request_id = data.get("requestId")
            if isinstance(request_id, str):
                self._resolve_pending(request_id, payload)
            return

        if msg_type == "message":
            event = self._parse_inbound_event(payload)
            if not event:
                return
            # Keep reader loop free so bridge command responses can be consumed
            # while inbound events are being ingested/published.
            task = asyncio.create_task(self._ingest_inbound_event(event))
            self._inbound_tasks.add(task)
            task.add_done_callback(self._on_inbound_task_done)
            return

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
        media_kind = str(media.get("kind")) if isinstance(media, dict) and media.get("kind") else None
        media_type = (
            str(media.get("mimeType"))
            if isinstance(media, dict) and media.get("mimeType")
            else None
        )

        reply_to_message_id = str(payload.get("replyToMessageId") or "").strip() or None
        reply_to_participant = str(payload.get("replyToParticipantJid") or "").strip() or None
        reply_to_text = str(payload.get("replyToText") or "").strip() or None
        reply_to_bot = bool(payload.get("replyToBot", False))

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
        )

    async def _ingest_inbound_event(self, event: InboundEvent) -> None:
        if self._is_duplicate(event.chat_jid, event.message_id):
            return

        self._archive_inbound_event(event)

        if self.config.debounce_ms <= 0 or event.media_kind is not None:
            await self._publish_event(event)
            return

        key = f"{event.chat_jid}:{event.sender_id}"
        if key not in self._debounce_buffers and len(self._debounce_buffers) >= self._max_debounce_buckets:
            self._debounce_overflow += 1
            if self._debounce_overflow == 1 or self._debounce_overflow % 100 == 0:
                logger.warning(
                    "WhatsApp debounce bucket overflow: "
                    f"dropped_batches={self._debounce_overflow} max={self._max_debounce_buckets}"
                )
            await self._publish_event(event)
            return
        bucket = self._debounce_buffers.setdefault(key, [])
        bucket.append(event)

        existing_task = self._debounce_tasks.get(key)
        if existing_task:
            existing_task.cancel()

        self._debounce_tasks[key] = asyncio.create_task(self._flush_debounce_bucket(key))

    def _on_inbound_task_done(self, task: asyncio.Task[None]) -> None:
        self._inbound_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"WhatsApp inbound task failed: {exc}")

    async def _flush_debounce_bucket(self, key: str) -> None:
        try:
            await asyncio.sleep(self.config.debounce_ms / 1000.0)
        except asyncio.CancelledError:
            return

        events = self._debounce_buffers.pop(key, [])
        self._debounce_tasks.pop(key, None)
        if not events:
            return

        if len(events) == 1:
            await self._publish_event(events[0])
            return

        combined_text = "\n".join(event.text for event in events if event.text).strip()
        last = events[-1]
        mentioned_jids = sorted({jid for event in events for jid in event.mentioned_jids})
        reply_to_message_id = next(
            (event.reply_to_message_id for event in reversed(events) if event.reply_to_message_id),
            None,
        )
        reply_to_participant = next(
            (event.reply_to_participant for event in reversed(events) if event.reply_to_participant),
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
            is_group=last.is_group,
            text=combined_text or last.text,
            timestamp=last.timestamp,
            mentioned_jids=mentioned_jids,
            mentioned_bot=any(event.mentioned_bot for event in events),
            reply_to_bot=any(event.reply_to_bot for event in events),
            reply_to_message_id=reply_to_message_id,
            reply_to_participant=reply_to_participant,
            reply_to_text=reply_to_text,
            media_kind=last.media_kind,
            media_type=last.media_type,
        )

        await self._publish_event(merged)

    def _archive_inbound_event(self, event: InboundEvent) -> None:
        if self.inbound_archive is None:
            return
        try:
            self.inbound_archive.record_inbound(
                channel=self.name,
                chat_id=event.chat_jid,
                message_id=event.message_id,
                participant=event.participant_jid,
                sender_id=event.sender_id,
                text=event.text,
                timestamp=event.timestamp,
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

    async def _publish_event(self, event: InboundEvent) -> None:
        text = event.text
        if text == "[Voice Message]":
            text = "[Voice Message: Transcription not available for WhatsApp yet]"

        # Best-effort typing indicator while the agent is processing this message.
        if self._should_start_typing(event):
            await self._start_typing(event.chat_jid)

        await self._handle_message(
            sender_id=event.sender_id,
            chat_id=event.chat_jid,
            content=text,
            metadata={
                "message_id": event.message_id,
                "timestamp": event.timestamp,
                "chat": event.chat_jid,
                "participant": event.participant_jid,
                "sender": event.sender_id,
                "is_group": event.is_group,
                "mentioned_bot": event.mentioned_bot,
                "reply_to_bot": event.reply_to_bot,
                "reply_to": event.reply_to_message_id,
                "reply_to_message_id": event.reply_to_message_id,
                "reply_to_participant": event.reply_to_participant,
                "reply_to_text": event.reply_to_text,
                "mentioned_jids": event.mentioned_jids,
                "media_path": None,
                "media_type": event.media_type,
                "media_kind": event.media_kind,
            },
        )

    def _should_start_typing(self, event: InboundEvent) -> bool:
        if not event.is_group:
            return True
        if event.mentioned_bot or event.reply_to_bot:
            return True
        return False

    async def _start_typing(self, chat_jid: str) -> None:
        if not chat_jid:
            return
        await self._stop_typing(chat_jid, send_paused=False)
        self._typing_tasks[chat_jid] = asyncio.create_task(self._typing_loop(chat_jid))

    async def _stop_typing(self, chat_jid: str, *, send_paused: bool = True) -> None:
        task = self._typing_tasks.pop(chat_jid, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if send_paused:
            await self._send_presence(chat_jid, "paused")

    async def _typing_loop(self, chat_jid: str) -> None:
        task = asyncio.current_task()
        started_at = asyncio.get_running_loop().time()
        while (
            self._running
            and self._connected
            and asyncio.get_running_loop().time() - started_at < TYPING_MAX_DURATION_SECONDS
        ):
            await self._send_presence(chat_jid, "composing")
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
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        envelope = {
            "version": PROTOCOL_VERSION,
            "type": command_type,
            "token": token or self._require_token(),
            "requestId": request_id,
            "accountId": "default",
            "payload": payload,
        }

        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(envelope))
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            self._pending.pop(request_id, None)

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
