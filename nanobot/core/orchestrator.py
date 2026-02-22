"""Typed vNext orchestrator pipeline."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Awaitable, Callable

from nanobot.core.admin_commands import AdminCommandResult
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
from nanobot.core.models import ArchivedMessage, InboundEvent, OutboundEvent, PolicyDecision
from nanobot.core.ports import PolicyPort, ReplyArchivePort, ResponderPort, SecurityPort
from nanobot.media.tts import (
    TTSSynthesizer,
    strip_markdown_for_tts,
    truncate_for_voice,
    write_tts_audio_file,
)

if TYPE_CHECKING:
    from nanobot.media.router import ModelRouter

_REACTION_RE = re.compile(r"^\s*::reaction::(.+?)\s*$", re.DOTALL)

_IDEA_MARKERS = ("[idea]", "#idea", "idea:", "inbox idea")
_BACKLOG_MARKERS = ("[backlog]", "#backlog", "backlog:")
_IDEA_PREFIX_WORDS = {
    "idea",
    "idee",
    "ideia",
    "идея",
    "아이디어",
    "アイデア",
    "想法",
}
_IDEA_PREFIX_PHRASES = {
    "new idea",
    "inbox idea",
}
_BACKLOG_PREFIX_WORDS = {
    "backlog",
    "todo",
    "aufgabe",
    "aufgaben",
    "tache",
    "tarea",
    "задача",
    "任务",
    "할일",
}
_BACKLOG_PREFIX_PHRASES = {
    "to do",
}


class Orchestrator:
    """Deterministic pipeline for inbound processing."""

    def __init__(
        self,
        *,
        policy: PolicyPort,
        responder: ResponderPort,
        reply_archive: ReplyArchivePort | None = None,
        reply_context_window_limit: int,
        reply_context_line_max_chars: int,
        ambient_window_limit: int = 8,
        dedupe_ttl_seconds: int = 20 * 60,
        typing_notifier: Callable[[str, str, bool], Awaitable[None]] | None = None,
        security: SecurityPort | None = None,
        security_block_message: str = "😂",
        policy_admin_handler: Callable[[InboundEvent], AdminCommandResult | str | None]
        | None = None,
        model_router: "ModelRouter | None" = None,
        tts: TTSSynthesizer | None = None,
        whatsapp_tts_outgoing_dir: Path | None = None,
        whatsapp_tts_max_raw_bytes: int = 160 * 1024,
        owner_alert_resolver: Callable[[str], list[str]] | None = None,
        owner_alert_cooldown_seconds: int = 300,
    ) -> None:
        self._policy = policy
        self._responder = responder
        self._reply_archive = reply_archive
        self._reply_context_window_limit = max(1, int(reply_context_window_limit))
        self._reply_context_line_max_chars = max(32, int(reply_context_line_max_chars))
        self._ambient_window_limit = max(0, int(ambient_window_limit))
        self._typing_notifier = typing_notifier
        self._security = security
        self._security_block_message = security_block_message
        self._policy_admin_handler = policy_admin_handler
        self._model_router = model_router
        self._tts = tts
        self._whatsapp_tts_outgoing_dir = whatsapp_tts_outgoing_dir
        self._whatsapp_tts_max_raw_bytes = max(1, int(whatsapp_tts_max_raw_bytes))
        self._owner_alert_resolver = owner_alert_resolver
        self._owner_alert_cooldown_seconds = max(30, int(owner_alert_cooldown_seconds))
        self._recent_owner_alert_keys: dict[str, float] = {}
        self._dedupe_ttl_seconds = max(1, int(dedupe_ttl_seconds))
        self._recent_message_keys: dict[str, float] = {}
        self._next_dedupe_cleanup_at = 0.0

    @staticmethod
    def _fold_accents(text: str) -> str:
        return "".join(
            ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
        )

    @classmethod
    def _capture_kind_and_body(cls, content: str) -> tuple[str, str] | None:
        """Classify explicit idea/backlog capture intent and return normalized body."""
        text = str(content or "").strip()
        if not text:
            return None

        lowered = text.lower()
        for marker in _BACKLOG_MARKERS:
            if lowered.startswith(marker):
                body = text[len(marker) :].lstrip(" \t:;.,-")
                return "backlog", (body or text)
        for marker in _IDEA_MARKERS:
            if lowered.startswith(marker):
                body = text[len(marker) :].lstrip(" \t:;.,-")
                return "idea", (body or text)

        tokens = list(re.finditer(r"[^\W_]+", text, flags=re.UNICODE))
        if not tokens:
            return None

        first = cls._fold_accents(tokens[0].group(0)).lower()
        first_two = first
        first_three = first
        if len(tokens) >= 2:
            second = cls._fold_accents(tokens[1].group(0)).lower()
            first_two = f"{first} {second}"
            first_three = first_two
        if len(tokens) >= 3:
            third = cls._fold_accents(tokens[2].group(0)).lower()
            first_three = f"{first_two} {third}"

        cut_at = tokens[0].end()
        kind: str | None = None
        if (
            first in _BACKLOG_PREFIX_WORDS
            or first_two in _BACKLOG_PREFIX_PHRASES
            or first_three in _BACKLOG_PREFIX_PHRASES
        ):
            kind = "backlog"
            if first_three in _BACKLOG_PREFIX_PHRASES and len(tokens) >= 3:
                cut_at = tokens[2].end()
            elif first_two in _BACKLOG_PREFIX_PHRASES and len(tokens) >= 2:
                cut_at = tokens[1].end()
        elif (
            first in _IDEA_PREFIX_WORDS
            or first_two in _IDEA_PREFIX_PHRASES
            or first_three in _IDEA_PREFIX_PHRASES
        ):
            kind = "idea"
            if first_three in _IDEA_PREFIX_PHRASES and len(tokens) >= 3:
                cut_at = tokens[2].end()
            elif first_two in _IDEA_PREFIX_PHRASES and len(tokens) >= 2:
                cut_at = tokens[1].end()

        if kind is None:
            return None

        body = text[cut_at:].lstrip(" \t:;.,-")
        return kind, (body or text)

    async def handle(self, event: InboundEvent) -> list[OrchestratorIntent]:
        """Process one inbound event and return executable intents."""
        intents: list[OrchestratorIntent] = []
        normalized = event.normalized_content()
        if not normalized:
            intents.append(
                RecordMetricIntent(
                    name="event_drop_empty",
                    labels=(("channel", event.channel),),
                )
            )
            return intents
        event = replace(event, content=normalized)

        if self._is_duplicate(event):
            intents.append(
                RecordMetricIntent(
                    name="event_drop_duplicate",
                    labels=(("channel", event.channel),),
                )
            )
            return intents

        self._record_archive(event)
        event, lookup_attempted, archive_hit = self._resolve_reply_context(event)
        if event.channel == "whatsapp" and lookup_attempted:
            intents.append(
                RecordMetricIntent(
                    name="reply_ctx_archive_hit" if archive_hit else "reply_ctx_archive_miss",
                    labels=(("channel", event.channel),),
                )
            )

        if self._policy_admin_handler is not None:
            admin_result = self._policy_admin_handler(event)
            if isinstance(admin_result, str):
                admin_result = AdminCommandResult(status="handled", response=admin_result)
            if admin_result is not None:
                for metric in admin_result.metric_events:
                    intents.append(
                        RecordMetricIntent(
                            name=metric.name,
                            value=metric.value,
                            labels=metric.labels,
                        )
                    )
                if admin_result.status == "ignored":
                    intents.append(
                        RecordMetricIntent(
                            name="admin_command_denied_or_ignored",
                            labels=(
                                ("channel", event.channel),
                                ("command", admin_result.command_name or "unknown"),
                            ),
                        )
                    )
                elif admin_result.intercepts_normal_flow:
                    metric_name = (
                        "admin_command_handled"
                        if admin_result.status == "handled"
                        else "admin_command_unknown"
                    )
                    intents.append(
                        RecordMetricIntent(
                            name=metric_name,
                            labels=(
                                ("channel", event.channel),
                                ("command", admin_result.command_name or "unknown"),
                            ),
                        )
                    )
                    intents.append(
                        RecordMetricIntent(
                            name="policy_admin_command",
                            labels=(("channel", event.channel),),
                        )
                    )
                    if admin_result.response:
                        intents.append(
                            SendOutboundIntent(
                                event=OutboundEvent(
                                    channel=event.channel,
                                    chat_id=event.chat_id,
                                    content=admin_result.response,
                                )
                            )
                        )
                    return intents

        decision = self._policy.evaluate(event)
        notes_capture_allowed = bool(decision.notes_enabled)
        notes_mode = decision.notes_mode

        capture_signal = None
        if event.channel == "whatsapp":
            capture_signal = self._capture_kind_and_body(event.content)
        if decision.accept_message and capture_signal is not None:
            capture_kind, capture_body = capture_signal
            canonical = f"[{capture_kind.upper()}] {capture_body}".strip()

            if self._security is not None:
                security_input = self._security.check_input(
                    canonical,
                    context={
                        "channel": event.channel,
                        "chat_id": event.chat_id,
                        "sender_id": event.sender_id,
                        "message_id": event.message_id or "",
                        "path": "idea_capture",
                    },
                )
                if security_input.decision.action == "block":
                    intents.append(
                        RecordMetricIntent(
                            name="security_input_blocked",
                            labels=(
                                ("channel", event.channel),
                                ("reason", security_input.decision.reason),
                            ),
                        )
                    )
                    intents.append(
                        RecordMetricIntent(
                            name="idea_capture_dropped_security",
                            labels=(("channel", event.channel), ("kind", capture_kind)),
                        )
                    )
                    return intents

            intents.append(
                RecordManualMemoryIntent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    content=canonical,
                    entry_kind="backlog" if capture_kind == "backlog" else "idea",
                )
            )
            intents.append(
                RecordMetricIntent(
                    name="idea_capture_saved",
                    labels=(("channel", event.channel), ("kind", capture_kind)),
                )
            )
            if event.message_id:
                emoji = "📌" if capture_kind == "backlog" else "💡"
                intents.append(
                    SendOutboundIntent(
                        event=OutboundEvent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            content="",
                            metadata={
                                "reaction_only": True,
                                "reaction": {
                                    "message_id": event.message_id,
                                    "emoji": emoji,
                                    "participant_jid": event.participant,
                                    "from_me": False,
                                },
                            },
                        )
                    )
                )
                intents.append(
                    RecordMetricIntent(
                        name="idea_capture_reacted",
                        labels=(("channel", event.channel), ("kind", capture_kind)),
                    )
                )
            else:
                intents.append(
                    RecordMetricIntent(
                        name="idea_capture_no_message_id",
                        labels=(("channel", event.channel), ("kind", capture_kind)),
                    )
                )
            return intents

        if not decision.accept_message:
            if notes_capture_allowed and decision.notes_allow_blocked_senders:
                if self._security is not None:
                    security_input = self._security.check_input(
                        event.content,
                        context={
                            "channel": event.channel,
                            "chat_id": event.chat_id,
                            "sender_id": event.sender_id,
                            "message_id": event.message_id or "",
                            "path": "memory_notes_background",
                        },
                    )
                    if security_input.decision.action == "block":
                        intents.append(
                            RecordMetricIntent(
                                name="security_input_blocked",
                                labels=(
                                    ("channel", event.channel),
                                    ("reason", security_input.decision.reason),
                                ),
                            )
                        )
                        intents.append(
                            RecordMetricIntent(
                                name="memory_notes_dropped_security",
                                labels=(("channel", event.channel),),
                            )
                        )
                    else:
                        intents.append(
                            QueueMemoryNotesCaptureIntent(
                                channel=event.channel,
                                chat_id=event.chat_id,
                                sender_id=event.sender_id,
                                message_id=event.message_id,
                                content=event.content,
                                is_group=event.is_group,
                                mode=notes_mode,
                                batch_interval_seconds=decision.notes_batch_interval_seconds,
                                batch_max_messages=decision.notes_batch_max_messages,
                            )
                        )
                        intents.append(
                            RecordMetricIntent(
                                name="memory_notes_enqueued",
                                labels=(("channel", event.channel),),
                            )
                        )
                else:
                    intents.append(
                        QueueMemoryNotesCaptureIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            sender_id=event.sender_id,
                            message_id=event.message_id,
                            content=event.content,
                            is_group=event.is_group,
                            mode=notes_mode,
                            batch_interval_seconds=decision.notes_batch_interval_seconds,
                            batch_max_messages=decision.notes_batch_max_messages,
                        )
                    )
                    intents.append(
                        RecordMetricIntent(
                            name="memory_notes_enqueued",
                            labels=(("channel", event.channel),),
                        )
                    )
            elif notes_capture_allowed:
                intents.append(
                    RecordMetricIntent(
                        name="memory_notes_dropped_policy",
                        labels=(("channel", event.channel),),
                    )
                )
            intents.append(
                RecordMetricIntent(
                    name="policy_drop_access",
                    labels=(("channel", event.channel), ("reason", decision.reason)),
                )
            )
            return intents
        if not decision.should_respond:
            if notes_capture_allowed:
                if self._security is not None:
                    security_input = self._security.check_input(
                        event.content,
                        context={
                            "channel": event.channel,
                            "chat_id": event.chat_id,
                            "sender_id": event.sender_id,
                            "message_id": event.message_id or "",
                            "path": "memory_notes_background",
                        },
                    )
                    if security_input.decision.action == "block":
                        intents.append(
                            RecordMetricIntent(
                                name="security_input_blocked",
                                labels=(
                                    ("channel", event.channel),
                                    ("reason", security_input.decision.reason),
                                ),
                            )
                        )
                        intents.append(
                            RecordMetricIntent(
                                name="memory_notes_dropped_security",
                                labels=(("channel", event.channel),),
                            )
                        )
                    else:
                        intents.append(
                            QueueMemoryNotesCaptureIntent(
                                channel=event.channel,
                                chat_id=event.chat_id,
                                sender_id=event.sender_id,
                                message_id=event.message_id,
                                content=event.content,
                                is_group=event.is_group,
                                mode=notes_mode,
                                batch_interval_seconds=decision.notes_batch_interval_seconds,
                                batch_max_messages=decision.notes_batch_max_messages,
                            )
                        )
                        intents.append(
                            RecordMetricIntent(
                                name="memory_notes_enqueued",
                                labels=(("channel", event.channel),),
                            )
                        )
                else:
                    intents.append(
                        QueueMemoryNotesCaptureIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            sender_id=event.sender_id,
                            message_id=event.message_id,
                            content=event.content,
                            is_group=event.is_group,
                            mode=notes_mode,
                            batch_interval_seconds=decision.notes_batch_interval_seconds,
                            batch_max_messages=decision.notes_batch_max_messages,
                        )
                    )
                    intents.append(
                        RecordMetricIntent(
                            name="memory_notes_enqueued",
                            labels=(("channel", event.channel),),
                        )
                    )
            intents.append(
                RecordMetricIntent(
                    name="policy_drop_reply",
                    labels=(("channel", event.channel), ("reason", decision.reason)),
                )
            )
            return intents

        if self._security is not None:
            security_input = self._security.check_input(
                event.content,
                context={
                    "channel": event.channel,
                    "chat_id": event.chat_id,
                    "sender_id": event.sender_id,
                    "message_id": event.message_id or "",
                },
            )
            if security_input.decision.action == "block":
                intents.append(
                    RecordMetricIntent(
                        name="security_input_blocked",
                        labels=(
                            ("channel", event.channel),
                            ("reason", security_input.decision.reason),
                        ),
                    )
                )
                if event.message_id:
                    intents.append(
                        SendReactionIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            message_id=event.message_id,
                            emoji=self._security_block_message,
                            participant_jid=event.participant,
                        )
                    )
                else:
                    intents.append(
                        SendOutboundIntent(
                            event=OutboundEvent(
                                channel=event.channel,
                                chat_id=event.chat_id,
                                content=self._security_block_message,
                            )
                        )
                    )
                return intents

        typing_started = False
        try:
            if event.channel == "whatsapp":
                if self._typing_notifier is None:
                    intents.append(
                        SetTypingIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            enabled=True,
                        )
                    )
                else:
                    await self._typing_notifier(event.channel, event.chat_id, True)
                typing_started = True

            reply = await self._responder.generate_reply(event, decision)
            if not reply:
                intents.append(
                    RecordMetricIntent(
                        name="responder_empty",
                        labels=(("channel", event.channel),),
                    )
                )
                return intents

            # ── Reaction marker: LLM returned ::reaction::<emoji> ──
            reaction_match = _REACTION_RE.match(reply)
            if reaction_match and event.message_id:
                emoji = reaction_match.group(1).strip()
                intents.append(
                    SendReactionIntent(
                        channel=event.channel,
                        chat_id=event.chat_id,
                        message_id=event.message_id,
                        emoji=emoji,
                        participant_jid=event.participant,
                    )
                )
                intents.append(
                    PersistSessionIntent(
                        session_key=f"{event.channel}:{event.chat_id}",
                        user_content=event.content,
                        assistant_content=f"[reacted with {emoji}]",
                    )
                )
                intents.append(
                    RecordMetricIntent(
                        name="reaction_sent",
                        labels=(("channel", event.channel),),
                    )
                )
                return intents

            if self._security is not None:
                output_result = self._security.check_output(
                    reply,
                    context={
                        "channel": event.channel,
                        "chat_id": event.chat_id,
                        "sender_id": event.sender_id,
                        "message_id": event.message_id or "",
                    },
                )
                if output_result.decision.action == "sanitize":
                    reply = output_result.sanitized_text or self._security_block_message
                    intents.append(
                        RecordMetricIntent(
                            name="security_output_sanitized",
                            labels=(("channel", event.channel),),
                        )
                    )
                elif output_result.decision.action == "block":
                    reply = output_result.sanitized_text or self._security_block_message
                    intents.append(
                        RecordMetricIntent(
                            name="security_output_blocked",
                            labels=(
                                ("channel", event.channel),
                                ("reason", output_result.decision.reason),
                            ),
                        )
                    )

            outbound_channel = event.channel
            outbound_chat_id = event.chat_id
            if event.channel == "system" and ":" in event.chat_id:
                route_channel, route_chat_id = event.chat_id.split(":", 1)
                if route_channel and route_chat_id:
                    outbound_channel = route_channel
                    outbound_chat_id = route_chat_id

            outbound = OutboundEvent(
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                content=reply,
            )
            voice_outbound = await self._maybe_voice_reply(
                event=event,
                reply=reply,
                outbound_channel=outbound_channel,
                outbound_chat_id=outbound_chat_id,
                decision=decision,
                intents=intents,
            )
            if voice_outbound is not None:
                outbound = voice_outbound
            intents.append(SendOutboundIntent(event=outbound))
            intents.append(
                PersistSessionIntent(
                    session_key=f"{event.channel}:{event.chat_id}",
                    user_content=event.content,
                    assistant_content=reply,
                )
            )
            intents.append(
                RecordMetricIntent(
                    name="response_sent",
                    labels=(("channel", event.channel),),
                )
            )
            return intents
        finally:
            if typing_started:
                if self._typing_notifier is None:
                    intents.append(
                        SetTypingIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            enabled=False,
                        )
                    )
                else:
                    await self._typing_notifier(event.channel, event.chat_id, False)

    @staticmethod
    def _is_inbound_voice(event: InboundEvent) -> bool:
        raw = event.raw_metadata
        if bool(raw.get("is_voice", False)):
            return True
        return str(raw.get("media_kind") or "").strip().lower() == "audio"

    def _resolve_tts_profile(self, *, route: str, channel: str) -> object | None:
        if self._model_router is None:
            return None
        task_key = str(route or "").strip() or "tts.speak"
        if task_key.startswith(f"{channel}."):
            return self._model_router.resolve(task_key)
        return self._model_router.resolve(task_key, channel=channel)

    async def _maybe_voice_reply(
        self,
        *,
        event: InboundEvent,
        reply: str,
        outbound_channel: str,
        outbound_chat_id: str,
        decision: PolicyDecision,
        intents: list[OrchestratorIntent],
    ) -> OutboundEvent | None:
        if self._tts is None or self._whatsapp_tts_outgoing_dir is None:
            return None
        if outbound_channel != "whatsapp":
            return None

        mode = str(getattr(decision, "voice_output_mode", "text") or "text").strip().lower()
        if mode in {"", "off", "text"}:
            return None
        if mode == "in_kind" and not self._is_inbound_voice(event):
            return None

        fmt = str(getattr(decision, "voice_output_format", "opus") or "opus").strip().lower()
        if fmt != "opus":
            return None

        route = str(getattr(decision, "voice_output_tts_route", "") or "").strip() or "tts.speak"
        profile = self._resolve_tts_profile(route=route, channel=outbound_channel)
        if profile is None:
            self._append_owner_alert(
                intents,
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                reason=f"tts_route_unresolved:{route}",
            )
            return None

        voice = str(getattr(decision, "voice_output_voice", "") or "").strip() or "alloy"
        max_sentences = int(getattr(decision, "voice_output_max_sentences", 2) or 2)
        max_chars = int(getattr(decision, "voice_output_max_chars", 150) or 150)

        plain = strip_markdown_for_tts(reply)
        limited = truncate_for_voice(plain, max_sentences=max_sentences, max_chars=max_chars)
        if not limited:
            return None

        try:
            audio, tts_error = await self._tts.synthesize_with_status(
                limited,
                profile=profile,
                voice=voice,
                format=fmt,
            )
        except Exception:
            self._append_owner_alert(
                intents,
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                reason="tts_exception",
            )
            return None
        if not audio:
            self._append_owner_alert(
                intents,
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                reason=tts_error or "tts_empty_audio",
            )
            return None
        if len(audio) > self._whatsapp_tts_max_raw_bytes:
            self._append_owner_alert(
                intents,
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                reason=f"tts_audio_too_large:{len(audio)}>{self._whatsapp_tts_max_raw_bytes}",
            )
            return None

        out_dir = self._whatsapp_tts_outgoing_dir / "tts"
        path = write_tts_audio_file(out_dir, audio, ext=".ogg")
        return OutboundEvent(
            channel=outbound_channel,
            chat_id=outbound_chat_id,
            content="",
            reply_to=event.message_id,
            media=(str(path),),
        )

    def _append_owner_alert(
        self,
        intents: list[OrchestratorIntent],
        *,
        channel: str,
        chat_id: str,
        reason: str,
    ) -> None:
        if self._owner_alert_resolver is None:
            return
        targets_raw = self._owner_alert_resolver(channel)
        if not targets_raw:
            return

        now = time.monotonic()
        for key, expires_at in list(self._recent_owner_alert_keys.items()):
            if expires_at <= now:
                self._recent_owner_alert_keys.pop(key, None)

        normalized_targets: list[str] = []
        for raw in targets_raw:
            target = self._normalize_owner_target(channel, raw)
            if target:
                normalized_targets.append(target)
        if not normalized_targets:
            return

        reason_compact = " ".join(str(reason or "unknown").split()).strip() or "unknown"
        key = f"{channel}:{reason_compact}"
        if key in self._recent_owner_alert_keys:
            return
        self._recent_owner_alert_keys[key] = now + float(self._owner_alert_cooldown_seconds)

        content = (
            f"⚠️ Nano diagnostic\nvoice fallback in {channel}:{chat_id}\nreason={reason_compact}"
        )
        for target in sorted(set(normalized_targets)):
            intents.append(
                SendOutboundIntent(
                    event=OutboundEvent(
                        channel=channel,
                        chat_id=target,
                        content=content,
                    )
                )
            )

    @staticmethod
    def _normalize_owner_target(channel: str, raw: str) -> str | None:
        value = str(raw or "").strip()
        if not value:
            return None
        if channel != "whatsapp":
            return value
        if "@" in value:
            return value
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            return None
        return f"{digits}@s.whatsapp.net"

    def _dedupe_key(self, event: InboundEvent) -> str | None:
        if not event.message_id:
            return None
        return f"{event.channel}:{event.chat_id}:{event.message_id}"

    def _is_duplicate(self, event: InboundEvent) -> bool:
        key = self._dedupe_key(event)
        if key is None:
            return False
        now = time.monotonic()
        if now >= self._next_dedupe_cleanup_at:
            expired = [
                k for k, expires_at in self._recent_message_keys.items() if expires_at <= now
            ]
            for expired_key in expired:
                self._recent_message_keys.pop(expired_key, None)
            self._next_dedupe_cleanup_at = now + 30.0
        if key in self._recent_message_keys:
            return True
        self._recent_message_keys[key] = now + float(self._dedupe_ttl_seconds)
        return False

    def _record_archive(self, event: InboundEvent) -> None:
        if self._reply_archive is None:
            return
        self._reply_archive.record_inbound(event)
        if event.reply_to_message_id and event.reply_to_text:
            seeded = replace(
                event,
                message_id=event.reply_to_message_id,
                content=event.reply_to_text,
            )
            self._reply_archive.record_inbound(seeded)

    def _resolve_reply_context(self, event: InboundEvent) -> tuple[InboundEvent, bool, bool]:
        if self._reply_archive is None:
            return event, False, False
        if event.channel != "whatsapp":
            return event, False, False
        reply_to_message_id = (event.reply_to_message_id or "").strip()
        has_payload_reply_text = bool((event.reply_to_text or "").strip())

        # Ambient window: last N messages before this one (always, regardless of reply)
        ambient_lines = self._build_ambient_window(event=event)

        if not reply_to_message_id:
            if has_payload_reply_text or ambient_lines:
                raw = dict(event.raw_metadata)
                if has_payload_reply_text:
                    raw.setdefault("reply_context_source", "payload")
                if ambient_lines:
                    raw["ambient_context_window"] = ambient_lines
                return replace(event, raw_metadata=raw), False, False
            return event, False, False

        row = self._reply_archive.lookup_message(event.channel, event.chat_id, reply_to_message_id)
        if row is None:
            row = self._reply_archive.lookup_message_any_chat(
                event.channel,
                reply_to_message_id,
                preferred_chat_id=event.chat_id,
            )
        if row is None:
            if has_payload_reply_text or ambient_lines:
                raw = dict(event.raw_metadata)
                if has_payload_reply_text:
                    raw.setdefault("reply_context_source", "payload")
                if ambient_lines:
                    raw["ambient_context_window"] = ambient_lines
                return replace(event, raw_metadata=raw), True, False
            return event, True, False

        raw = dict(event.raw_metadata)
        raw.setdefault("reply_context_source", "payload" if has_payload_reply_text else "archive")
        window_lines = self._build_reply_context_window(event=event, anchor=row)
        if window_lines:
            raw["reply_context_window"] = window_lines
        if ambient_lines:
            raw["ambient_context_window"] = ambient_lines

        if has_payload_reply_text:
            return replace(event, raw_metadata=raw), True, True

        text = row.text.strip()
        if not text:
            return replace(event, raw_metadata=raw), True, False
        raw["reply_context_source"] = "archive"
        return replace(event, reply_to_text=text, raw_metadata=raw), True, True

    def _build_reply_context_window(
        self, *, event: InboundEvent, anchor: ArchivedMessage
    ) -> list[str]:
        if self._reply_archive is None:
            return []
        if not anchor.message_id:
            return []
        # Rows with missing sender are usually synthetic seed rows from reply payloads,
        # which do not carry a reliable anchor point for earlier context.
        if not anchor.sender_id:
            return []

        try:
            before = self._reply_archive.lookup_messages_before(
                event.channel,
                anchor.chat_id or event.chat_id,
                anchor.message_id,
                limit=self._reply_context_window_limit,
            )
        except Exception:
            return []
        if not before:
            return []

        lines: list[str] = []
        for row in reversed(before):
            compact = " ".join(row.text.split())
            if not compact:
                continue
            if len(compact) > self._reply_context_line_max_chars:
                compact = compact[: self._reply_context_line_max_chars] + "..."
            speaker = (row.sender_id or row.participant or "unknown").strip() or "unknown"
            lines.append(f"[{speaker}] {compact}")
        return lines[: self._reply_context_window_limit]

    def _build_ambient_window(self, *, event: InboundEvent) -> list[str]:
        """Fetch last N messages before the current event — ambient group state."""
        if self._reply_archive is None or self._ambient_window_limit <= 0:
            return []
        if not event.message_id:
            return []
        try:
            before = self._reply_archive.lookup_messages_before(
                event.channel,
                event.chat_id,
                event.message_id,
                limit=self._ambient_window_limit,
            )
        except Exception:
            return []
        if not before:
            return []
        lines: list[str] = []
        for row in reversed(before):
            compact = " ".join(row.text.split())
            if not compact:
                continue
            if len(compact) > self._reply_context_line_max_chars:
                compact = compact[: self._reply_context_line_max_chars] + "..."
            speaker = (row.sender_id or row.participant or "unknown").strip() or "unknown"
            lines.append(f"[{speaker}] {compact}")
        return lines[: self._ambient_window_limit]
