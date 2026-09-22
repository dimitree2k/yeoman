"""Deterministic owner-only native WhatsApp forwarding command."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping

from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import OutboundEvent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext

ForwardTargetResolver = Callable[[str], tuple[str | None, str | None]]
ForwardSourceLookup = Callable[[str, str], Awaitable[Mapping[str, object]]]

_FORWARD_COMMAND = re.compile(r"^/?forward(?:\s+(.+))?$", re.IGNORECASE)
_SOURCE_UNAVAILABLE = "Die Originalnachricht ist nicht verfügbar; der Forward wurde nicht gesendet."
_FORWARD_UNAVAILABLE = "Forward ist hier nicht verfügbar."


class ForwardCommandMiddleware:
    """Handle ``forward`` without invoking the responder or language model."""

    def __init__(
        self,
        *,
        target_resolver: ForwardTargetResolver | None = None,
        source_lookup: ForwardSourceLookup | None = None,
    ) -> None:
        self._target_resolver = target_resolver
        self._source_lookup = source_lookup

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.event.channel != "whatsapp":
            await next(ctx)
            return

        match = _FORWARD_COMMAND.fullmatch(ctx.event.content.strip())
        if match is None:
            await next(ctx)
            return

        decision = ctx.decision
        if decision is None:
            self._respond(ctx, _FORWARD_UNAVAILABLE)
            return
        if not decision.is_owner:
            ctx.halt()
            return
        if "forward_message" not in decision.allowed_tools:
            self._respond(ctx, _FORWARD_UNAVAILABLE)
            return

        source_message_id = str(ctx.event.reply_to_message_id or "").strip()
        if not source_message_id:
            self._respond(ctx, "Bitte antworte auf die Nachricht, die weitergeleitet werden soll.")
            return
        if self._source_lookup is None:
            self._respond(ctx, _SOURCE_UNAVAILABLE)
            return

        try:
            source = await self._source_lookup(ctx.event.chat_id, source_message_id)
        except Exception:  # noqa: BLE001 - forwarding fails closed at the provider boundary
            source = {}
        if not isinstance(source, Mapping) or source.get("status") != "found":
            self._respond(ctx, _SOURCE_UNAVAILABLE)
            return

        target = ctx.event.chat_id
        reference = str(match.group(1) or "").strip()
        if reference:
            if self._target_resolver is None:
                self._respond(ctx, _FORWARD_UNAVAILABLE)
                return
            try:
                target, _ = self._target_resolver(reference)
            except Exception:  # noqa: BLE001 - target resolution fails closed
                target = None
            if not target:
                self._respond(ctx, _FORWARD_UNAVAILABLE)
                return

        event = ctx.event
        ctx.intents.append(
            SendOutboundIntent(
                event=OutboundEvent(
                    channel="whatsapp",
                    chat_id=target,
                    content="",
                    metadata={
                        "message_id": event.message_id or "forward-command",
                        "forward_message": {
                            "source_chat_id": event.chat_id,
                            "source_message_id": source_message_id,
                        },
                    },
                )
            )
        )
        ctx.halt()

    @staticmethod
    def _respond(ctx: PipelineContext, content: str) -> None:
        event = ctx.event
        ctx.intents.append(
            SendOutboundIntent(
                event=OutboundEvent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    content=content,
                    reply_to=event.message_id,
                    metadata={"message_id": event.message_id or "forward-command"},
                )
            )
        )
        ctx.halt()
