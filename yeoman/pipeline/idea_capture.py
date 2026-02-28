"""Idea and backlog capture middleware.

Corresponds to orchestrator stage 7A: detect ``[idea]`` / ``[backlog]``
prefixed messages and capture them as manual memory entries with a reaction
emoji, bypassing the LLM entirely.
"""

from __future__ import annotations

import re
import unicodedata

from yeoman.core.intents import (
    RecordManualMemoryIntent,
    SendOutboundIntent,
)
from yeoman.core.models import OutboundEvent
from yeoman.core.pipeline import NextFn, PipelineContext
from yeoman.core.ports import SecurityPort

_IDEA_MARKERS = ("[idea]", "#idea", "idea:", "inbox idea")
_BACKLOG_MARKERS = ("[backlog]", "#backlog", "backlog:")

_IDEA_PREFIX_WORDS = {
    "idea", "idee", "ideia", "идея", "아이디어", "アイデア", "想法",
}
_IDEA_PREFIX_PHRASES = {"new idea", "inbox idea"}

_BACKLOG_PREFIX_WORDS = {
    "backlog", "todo", "aufgabe", "aufgaben", "tache", "tarea", "задача", "任务", "할일",
}
_BACKLOG_PREFIX_PHRASES = {"to do"}


class IdeaCaptureMiddleware:
    """Intercept idea/backlog messages and capture directly to memory."""

    def __init__(self, *, security: SecurityPort | None = None) -> None:
        self._security = security

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        # Only capture on WhatsApp and only when message is accepted.
        if ctx.event.channel != "whatsapp":
            await next(ctx)
            return
        if ctx.decision is None or not ctx.decision.accept_message:
            await next(ctx)
            return

        capture_signal = _capture_kind_and_body(ctx.event.content)
        if capture_signal is None:
            await next(ctx)
            return

        capture_kind, capture_body = capture_signal
        canonical = f"[{capture_kind.upper()}] {capture_body}".strip()

        # Security check on the canonical form.
        if self._security is not None:
            result = self._security.check_input(
                canonical,
                context={
                    "channel": ctx.event.channel,
                    "chat_id": ctx.event.chat_id,
                    "sender_id": ctx.event.sender_id,
                    "message_id": ctx.event.message_id or "",
                    "path": "idea_capture",
                },
            )
            if result.decision.action == "block":
                ctx.metric(
                    "security_input_blocked",
                    labels=(
                        ("channel", ctx.event.channel),
                        ("reason", result.decision.reason),
                    ),
                )
                ctx.metric(
                    "idea_capture_dropped_security",
                    labels=(("channel", ctx.event.channel), ("kind", capture_kind)),
                )
                ctx.halt()
                return

        # Persist the capture.
        ctx.intents.append(
            RecordManualMemoryIntent(
                channel=ctx.event.channel,
                chat_id=ctx.event.chat_id,
                sender_id=ctx.event.sender_id,
                content=canonical,
                entry_kind="backlog" if capture_kind == "backlog" else "idea",
            )
        )
        ctx.metric(
            "idea_capture_saved",
            labels=(("channel", ctx.event.channel), ("kind", capture_kind)),
        )

        # React with emoji.
        if ctx.event.message_id:
            emoji = "📌" if capture_kind == "backlog" else "💡"
            ctx.intents.append(
                SendOutboundIntent(
                    event=OutboundEvent(
                        channel=ctx.event.channel,
                        chat_id=ctx.event.chat_id,
                        content="",
                        metadata={
                            "reaction_only": True,
                            "reaction": {
                                "message_id": ctx.event.message_id,
                                "emoji": emoji,
                                "participant_jid": ctx.event.participant,
                                "from_me": False,
                            },
                        },
                    )
                )
            )
            ctx.metric(
                "idea_capture_reacted",
                labels=(("channel", ctx.event.channel), ("kind", capture_kind)),
            )
        else:
            ctx.metric(
                "idea_capture_no_message_id",
                labels=(("channel", ctx.event.channel), ("kind", capture_kind)),
            )

        ctx.halt()


# ── Parsing helpers (ported from Orchestrator) ───────────────────────


def _fold_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _capture_kind_and_body(content: str) -> tuple[str, str] | None:
    """Classify explicit idea/backlog capture intent and return normalized body."""
    text = str(content or "").strip()
    if not text:
        return None

    lowered = text.lower()
    for marker in _BACKLOG_MARKERS:
        if lowered.startswith(marker):
            body = text[len(marker):].lstrip(" \t:;.,-")
            return "backlog", (body or text)
    for marker in _IDEA_MARKERS:
        if lowered.startswith(marker):
            body = text[len(marker):].lstrip(" \t:;.,-")
            return "idea", (body or text)

    tokens = list(re.finditer(r"[^\W_]+", text, flags=re.UNICODE))
    if not tokens:
        return None

    first = _fold_accents(tokens[0].group(0)).lower()
    first_two = first
    first_three = first
    if len(tokens) >= 2:
        second = _fold_accents(tokens[1].group(0)).lower()
        first_two = f"{first} {second}"
        first_three = first_two
    if len(tokens) >= 3:
        third = _fold_accents(tokens[2].group(0)).lower()
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
