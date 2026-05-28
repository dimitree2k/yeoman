"""Implicit bot-address handling for mention-only group chats."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from yeoman_gateway.core.intents import SendReactionIntent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.implicit_addressing import (
    ConversationState,
    SessionManagerLike,
    classify_conversation_state,
    reaction_for_name_mention,
)


class ImplicitBotAddressMiddleware:
    """Promote strong implicit address signals without making groups reply to all."""

    def __init__(
        self,
        *,
        session_manager: SessionManagerLike | None = None,
        bot_name_aliases: Sequence[str] = ("arvid",),
        followup_window_seconds: float = 900.0,
    ) -> None:
        self._session_manager = session_manager
        self._bot_name_aliases = tuple(
            str(alias).strip() for alias in bot_name_aliases if str(alias).strip()
        )
        self._followup_window_seconds = max(0.0, float(followup_window_seconds))

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        event = ctx.event
        if decision is None:
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
        state_raw = event.raw_metadata.get("conversation_state")
        state_mode = str(
            state_raw.get("address_mode") if isinstance(state_raw, dict) else ""
        )
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
