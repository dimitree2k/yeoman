"""Implicit bot-address handling for mention-only group chats."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from yeoman_shared.reactions import SYSTEM_ORIGIN

from yeoman_gateway.core.intents import SendReactionIntent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.implicit_addressing import (
    ConversationState,
    SessionManagerLike,
    classify_conversation_state,
    reaction_for_name_mention,
    reaction_for_reply_ack,
)


def _processing_owns_outcome(event: object) -> bool:
    """True when the processing core already acted for this message.

    The core reacts on its own paths (the judge's verdict, the `react` reply action) and
    grants ambient answers; both are recorded on the event before it reaches the pipeline.
    In a managed chat that decision is the whole reply, so the classic acknowledgement
    reactions step aside - exactly one reaction per message, and no halted answer.
    """
    metadata = getattr(event, "raw_metadata", None) or {}
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("processing_reacted") or metadata.get("processing_answer_granted")
    )


class ImplicitBotAddressMiddleware:
    """Promote strong implicit address signals without making groups reply to all."""

    def __init__(
        self,
        *,
        session_manager: SessionManagerLike | None = None,
        bot_name_aliases: Sequence[str] = ("arvid",),
        followup_window_seconds: float = 900.0,
        bait_reaction_streak_threshold: int = 2,
        bait_reaction_window_seconds: float = 120.0,
        bait_reaction_cooldown_seconds: float = 600.0,
    ) -> None:
        self._session_manager = session_manager
        self._bot_name_aliases = tuple(
            str(alias).strip() for alias in bot_name_aliases if str(alias).strip()
        )
        self._followup_window_seconds = max(0.0, float(followup_window_seconds))
        self._bait_reaction_streak_threshold = max(1, int(bait_reaction_streak_threshold))
        self._bait_reaction_window_seconds = max(0.0, float(bait_reaction_window_seconds))
        self._bait_reaction_cooldown_seconds = max(0.0, float(bait_reaction_cooldown_seconds))
        self._bait_reaction_events: dict[str, list[datetime]] = {}
        self._bait_reaction_cooldowns: dict[str, datetime] = {}

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        event = ctx.event
        if decision is None:
            await next(ctx)
            return
        if not decision.accept_message:
            await next(ctx)
            return
        if event.is_group:
            state = classify_conversation_state(
                session_manager=self._session_manager,
                channel=event.channel,
                chat_id=event.chat_id,
                event_time=event.timestamp,
                content=str(event.content or ""),
                metadata=dict(event.raw_metadata or {}),
                mentioned_bot=event.mentioned_bot,
                reply_to_bot=event.reply_to_bot,
                bot_name_aliases=self._bot_name_aliases,
                followup_window_seconds=self._followup_window_seconds,
            )
            self._apply_conversation_state(ctx, state)
        else:
            await next(ctx)
            return

        event = ctx.event
        if _processing_owns_outcome(event):
            # The processing core has already decided for this message: it either sent the
            # one reaction a message may carry or granted an answer. A second, mechanical
            # face here would overwrite that reaction in the chat (WhatsApp keeps one per
            # message) and its halt would swallow the granted answer.
            await next(ctx)
            return
        state_raw = event.raw_metadata.get("conversation_state")
        state_mode = str(
            state_raw.get("address_mode") if isinstance(state_raw, dict) else ""
        )
        if state_mode == "reply_ack":
            self._react_or_silence_bait(ctx, emoji=reaction_for_reply_ack(str(event.content or "")))
            return

        if state_mode == "group_member_bait":
            self._react_or_silence_bait(ctx, emoji="🙄")
            return

        if state_mode == "low_content_reply":
            self._react_or_silence_bait(ctx, emoji="👀")
            return

        if not decision.accept_message or decision.should_respond:
            await next(ctx)
            return
        if decision.when_to_reply_mode != "mention_only":
            await next(ctx)
            return
        if decision.reason != "when_to_reply:mention_only_group":
            await next(ctx)
            return
        if event.mentioned_bot or event.reply_to_bot:
            await next(ctx)
            return

        content = str(event.content or "").strip()
        if state_mode == "repair_feedback":
            self._promote_to_reply(ctx, mentioned_bot=True, reason="repair_feedback")
            await next(ctx)
            return

        if state_mode == "plain_name_request":
            self._promote_to_reply(ctx, mentioned_bot=True, reason="plain_name_request")
            await next(ctx)
            return

        if state_mode == "quoted_context_request":
            self._promote_to_reply(ctx, mentioned_bot=True, reason="quoted_context_request")
            await next(ctx)
            return

        if state_mode == "recent_assistant_followup":
            self._promote_to_reply(ctx, reply_to_bot=True, reason="recent_assistant_followup")
            await next(ctx)
            return

        if state_mode == "name_mention":
            if event.message_id:
                ctx.intents.append(
                    SendReactionIntent(
                        channel=event.channel,
                        chat_id=event.chat_id,
                        message_id=event.message_id,
                        emoji=reaction_for_name_mention(content),
                        participant_jid=event.participant,
                        origin=SYSTEM_ORIGIN,
                    )
                )
                ctx.metric("implicit_bot_address_reaction", labels=(("channel", event.channel),))
            else:
                ctx.metric(
                    "implicit_bot_address_reaction_skipped",
                    labels=(("channel", event.channel), ("reason", "missing_message_id")),
                )
            ctx.halt()
            return

        await next(ctx)

    def _apply_conversation_state(
        self,
        ctx: PipelineContext,
        state: ConversationState,
    ) -> None:
        raw = dict(ctx.event.raw_metadata or {})
        raw["conversation_state"] = state.as_metadata()
        ctx.event = replace(ctx.event, raw_metadata=raw)

    def _promote_to_reply(
        self,
        ctx: PipelineContext,
        *,
        mentioned_bot: bool = False,
        reply_to_bot: bool = False,
        reason: str,
    ) -> None:
        raw = dict(ctx.event.raw_metadata or {})
        raw["implicit_bot_address"] = reason
        ctx.event = replace(
            ctx.event,
            mentioned_bot=ctx.event.mentioned_bot or mentioned_bot,
            reply_to_bot=ctx.event.reply_to_bot or reply_to_bot,
            raw_metadata=raw,
        )
        if ctx.decision is not None:
            ctx.decision = replace(
                ctx.decision,
                should_respond=True,
                reason=f"when_to_reply:implicit_{reason}",
            )
        ctx.metric("implicit_bot_address_reply", labels=(("channel", ctx.event.channel),))

    def _react_or_silence_bait(self, ctx: PipelineContext, *, emoji: str) -> None:
        if self._bait_cooldown_active(ctx):
            self._set_conversation_state_mode(ctx, "bait_cooldown", preferred_action="silence")
            ctx.metric("implicit_bot_address_bait_cooldown", labels=(("channel", ctx.event.channel),))
            ctx.halt()
            return

        event = ctx.event
        if event.message_id:
            ctx.intents.append(
                SendReactionIntent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    message_id=event.message_id,
                    emoji=emoji,
                    participant_jid=event.participant,
                    origin=SYSTEM_ORIGIN,
                )
            )
            ctx.metric("implicit_bot_address_reaction", labels=(("channel", event.channel),))
            self._record_bait_reaction(event.channel, event.chat_id, event.timestamp)
        else:
            ctx.metric(
                "implicit_bot_address_reaction_skipped",
                labels=(("channel", event.channel), ("reason", "missing_message_id")),
            )
        ctx.halt()

    def _bait_cooldown_active(self, ctx: PipelineContext) -> bool:
        key = self._bait_key(ctx.event.channel, ctx.event.chat_id)
        event_time = self._normalized_time(ctx.event.timestamp)
        until = self._bait_reaction_cooldowns.get(key)
        if until is None:
            return False
        if event_time < until:
            return True
        self._bait_reaction_cooldowns.pop(key, None)
        return False

    def _record_bait_reaction(self, channel: str, chat_id: str, event_time: datetime) -> None:
        key = self._bait_key(channel, chat_id)
        normalized = self._normalized_time(event_time)
        cutoff = normalized - timedelta(seconds=self._bait_reaction_window_seconds)
        events = [
            previous
            for previous in self._bait_reaction_events.get(key, [])
            if previous >= cutoff
        ]
        events.append(normalized)
        self._bait_reaction_events[key] = events
        if len(events) >= self._bait_reaction_streak_threshold:
            self._bait_reaction_cooldowns[key] = normalized + timedelta(
                seconds=self._bait_reaction_cooldown_seconds
            )

    def _set_conversation_state_mode(
        self,
        ctx: PipelineContext,
        address_mode: str,
        *,
        preferred_action: str,
    ) -> None:
        raw = dict(ctx.event.raw_metadata or {})
        state_raw = raw.get("conversation_state")
        if isinstance(state_raw, dict):
            state = dict(state_raw)
            state["address_mode"] = address_mode
            state["preferred_action"] = preferred_action
            state["answer_shape"] = "none"
            raw["conversation_state"] = state
            ctx.event = replace(ctx.event, raw_metadata=raw)

    @staticmethod
    def _bait_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    @staticmethod
    def _normalized_time(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
