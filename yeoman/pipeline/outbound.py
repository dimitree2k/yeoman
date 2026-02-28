"""Outbound assembly middleware — reaction, security, voice, threading.

Corresponds to orchestrator stages 13-17: detect reaction markers, apply
output security, optionally synthesize voice, decide threading, and assemble
the final outbound intent.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from yeoman.core.intents import (
    PersistSessionIntent,
    SendOutboundIntent,
    SendReactionIntent,
)
from yeoman.core.models import OutboundEvent
from yeoman.core.pipeline import NextFn, PipelineContext
from yeoman.core.ports import SecurityPort

if TYPE_CHECKING:
    from yeoman.media.router import ModelRouter
    from yeoman.media.tts import TTSSynthesizer

_REACTION_RE = re.compile(r"^\s*::reaction::(.+?)\s*$", re.DOTALL)
# Matches text followed by a reaction suffix: "some text\n\n::reaction::emoji"
_REACTION_SUFFIX_RE = re.compile(r"^([\s\S]+?)\n+::reaction::([^\n]+?)\s*$")


class OutboundMiddleware:
    """Assemble final outbound intent from the reply in ``ctx.reply``.

    Handles:
    - ``::reaction::`` marker detection (emoji-only reply)
    - Output security check (sanitize / block)
    - Voice reply synthesis (WhatsApp TTS)
    - Threading decision (mention_only groups)
    - System channel routing (``system:channel:chat_id``)
    - Session persistence intent
    """

    def __init__(
        self,
        *,
        security: SecurityPort | None = None,
        security_block_message: str = "😂",
        tts: "TTSSynthesizer | None" = None,
        whatsapp_tts_outgoing_dir: Path | None = None,
        whatsapp_tts_max_raw_bytes: int = 160 * 1024,
        model_router: "ModelRouter | None" = None,
        owner_alert_resolver: Callable[[str], list[str]] | None = None,
        owner_alert_cooldown_seconds: int = 300,
    ) -> None:
        self._security = security
        self._security_block_message = security_block_message
        self._tts = tts
        self._tts_outgoing_dir = whatsapp_tts_outgoing_dir
        self._tts_max_raw_bytes = max(1, int(whatsapp_tts_max_raw_bytes))
        self._model_router = model_router
        self._owner_alert_resolver = owner_alert_resolver
        self._owner_alert_cooldown_seconds = max(30, int(owner_alert_cooldown_seconds))
        self._recent_alert_keys: dict[str, float] = {}

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        reply = ctx.reply
        if reply is None:
            return

        event = ctx.event
        decision = ctx.decision

        # ── Reaction marker ──────────────────────────────────────────
        reaction_match = _REACTION_RE.match(reply)
        if reaction_match and event.message_id:
            full_content = reaction_match.group(1).strip()
            # Split emoji from optional text body (separated by newline)
            parts = full_content.split("\n", 1)
            emoji = parts[0].strip()
            text_body = parts[1].strip() if len(parts) > 1 else ""
            ctx.intents.append(
                SendReactionIntent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    message_id=event.message_id,
                    emoji=emoji,
                    participant_jid=event.participant,
                )
            )
            ctx.metric("reaction_sent", labels=(("channel", event.channel),))
            if not text_body:
                ctx.intents.append(
                    PersistSessionIntent(
                        session_key=f"{event.channel}:{event.chat_id}",
                        user_content=event.content,
                        assistant_content=f"[reacted with {emoji}]",
                    )
                )
                return
            # Fall through with text_body as the reply to send
            reply = text_body
        else:
            # Model appended ::reaction:: after text instead of using it standalone.
            # Strip the marker and send only the clean text body.
            suffix_match = _REACTION_SUFFIX_RE.match(reply)
            if suffix_match:
                reply = suffix_match.group(1).strip()

        # ── Output security ──────────────────────────────────────────
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
                ctx.metric("security_output_sanitized", labels=(("channel", event.channel),))
            elif output_result.decision.action == "block":
                reply = output_result.sanitized_text or self._security_block_message
                ctx.metric(
                    "security_output_blocked",
                    labels=(("channel", event.channel), ("reason", output_result.decision.reason)),
                )

        # ── Outbound channel routing ─────────────────────────────────
        outbound_channel = event.channel
        outbound_chat_id = event.chat_id
        if event.channel == "system" and ":" in event.chat_id:
            route_channel, route_chat_id = event.chat_id.split(":", 1)
            if route_channel and route_chat_id:
                outbound_channel = route_channel
                outbound_chat_id = route_chat_id

        # ── Threading decision ───────────────────────────────────────
        should_thread = (
            decision is not None
            and outbound_channel == "whatsapp"
            and event.is_group
            and bool(event.message_id)
            and decision.when_to_reply_mode == "mention_only"
            and (event.mentioned_bot or event.reply_to_bot)
        )

        outbound = OutboundEvent(
            channel=outbound_channel,
            chat_id=outbound_chat_id,
            content=reply,
            reply_to=event.message_id if should_thread else None,
        )

        # ── Voice reply ──────────────────────────────────────────────
        voice_outbound = await self._maybe_voice_reply(
            ctx=ctx,
            reply=reply,
            outbound_channel=outbound_channel,
            outbound_chat_id=outbound_chat_id,
        )
        if voice_outbound is not None:
            outbound = voice_outbound

        # ── Emit intents ─────────────────────────────────────────────
        ctx.intents.append(SendOutboundIntent(event=outbound))
        ctx.intents.append(
            PersistSessionIntent(
                session_key=f"{event.channel}:{event.chat_id}",
                user_content=event.content,
                assistant_content=reply,
            )
        )
        ctx.metric("response_sent", labels=(("channel", event.channel),))

    # ── Voice reply synthesis ────────────────────────────────────────

    async def _maybe_voice_reply(
        self,
        *,
        ctx: PipelineContext,
        reply: str,
        outbound_channel: str,
        outbound_chat_id: str,
    ) -> OutboundEvent | None:
        if self._tts is None or self._tts_outgoing_dir is None:
            return None
        if outbound_channel != "whatsapp":
            return None

        decision = ctx.decision
        if decision is None:
            return None

        mode = str(getattr(decision, "voice_output_mode", "text") or "text").strip().lower()
        if mode in {"", "off", "text"}:
            return None
        if mode == "in_kind" and not self._is_inbound_voice(ctx.event):
            return None

        fmt = str(getattr(decision, "voice_output_format", "opus") or "opus").strip().lower()
        if fmt != "opus":
            return None

        route = str(getattr(decision, "voice_output_tts_route", "") or "").strip() or "tts.speak"
        profile = self._resolve_tts_profile(route=route, channel=outbound_channel)
        if profile is None:
            self._append_owner_alert(
                ctx, channel=outbound_channel, chat_id=outbound_chat_id,
                reason=f"tts_route_unresolved:{route}",
            )
            return None

        voice = str(getattr(decision, "voice_output_voice", "") or "").strip() or "alloy"
        max_sentences = int(getattr(decision, "voice_output_max_sentences", 2) or 2)
        max_chars = int(getattr(decision, "voice_output_max_chars", 150) or 150)

        from yeoman.media.tts import (
            strip_markdown_for_tts,
            truncate_for_voice,
            write_tts_audio_file,
        )

        plain = strip_markdown_for_tts(reply)
        limited = truncate_for_voice(plain, max_sentences=max_sentences, max_chars=max_chars)
        if not limited:
            return None

        try:
            audio, tts_error = await self._tts.synthesize_with_status(
                limited, profile=profile, voice=voice, format=fmt,
            )
        except Exception:
            self._append_owner_alert(
                ctx, channel=outbound_channel, chat_id=outbound_chat_id, reason="tts_exception",
            )
            return None

        if not audio:
            self._append_owner_alert(
                ctx, channel=outbound_channel, chat_id=outbound_chat_id,
                reason=tts_error or "tts_empty_audio",
            )
            return None

        if len(audio) > self._tts_max_raw_bytes:
            self._append_owner_alert(
                ctx, channel=outbound_channel, chat_id=outbound_chat_id,
                reason=f"tts_audio_too_large:{len(audio)}>{self._tts_max_raw_bytes}",
            )
            return None

        out_dir = self._tts_outgoing_dir / "tts"
        path = write_tts_audio_file(out_dir, audio, ext=".ogg")
        return OutboundEvent(
            channel=outbound_channel,
            chat_id=outbound_chat_id,
            content="",
            reply_to=ctx.event.message_id,
            media=(str(path),),
        )

    @staticmethod
    def _is_inbound_voice(event: object) -> bool:
        raw = getattr(event, "raw_metadata", {})
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

    def _append_owner_alert(
        self,
        ctx: PipelineContext,
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
        for key, expires_at in list(self._recent_alert_keys.items()):
            if expires_at <= now:
                self._recent_alert_keys.pop(key, None)

        reason_compact = " ".join(str(reason or "unknown").split()).strip() or "unknown"
        key = f"{channel}:{reason_compact}"
        if key in self._recent_alert_keys:
            return
        self._recent_alert_keys[key] = now + float(self._owner_alert_cooldown_seconds)

        from yeoman.pipeline.new_chat import _normalize_owner_target

        content = (
            f"⚠️ Nano diagnostic\nvoice fallback in {channel}:{chat_id}\nreason={reason_compact}"
        )
        normalized_targets: list[str] = []
        for raw in targets_raw:
            target = _normalize_owner_target(channel, raw)
            if target:
                normalized_targets.append(target)

        for target in sorted(set(normalized_targets)):
            ctx.intents.append(
                SendOutboundIntent(
                    event=OutboundEvent(
                        channel=channel,
                        chat_id=target,
                        content=content,
                    )
                )
            )
