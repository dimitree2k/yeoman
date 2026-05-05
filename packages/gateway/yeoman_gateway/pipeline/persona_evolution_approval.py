"""Middleware to intercept owner approval codes for persona evolution."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.persona_evolution import (
    apply_persona_evolution_proposal,
    deny_persona_evolution_proposal,
)

_APPROVAL_CODE_RE = re.compile(r"\bpe-(approve|deny)-([A-Za-z0-9][A-Za-z0-9_-]*)\b")
_YES_REPLIES = {"yes", "y", "ja", "j", "approve", "approved", "ok", "okay"}
_NO_REPLIES = {"no", "n", "nein", "deny", "denied", "reject", "rejected"}


class PersonaEvolutionApprovalMiddleware:
    """Consume `pe-approve-*` and `pe-deny-*` owner replies."""

    def __init__(
        self,
        *,
        workspace: Path,
        state_db_path: Path,
        bus: MessageBus,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._workspace = workspace
        self._state_db_path = state_db_path
        self._bus = bus
        self._now = now or (lambda: datetime.now(UTC))

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if not getattr(ctx.decision, "is_owner", False):
            await next(ctx)
            return

        parsed = self._parse_code(ctx.event.content)
        if parsed is None and ctx.event.reply_to_bot:
            parsed = self._parse_reply_decision(ctx.event.content, ctx.event.reply_to_text)
        if parsed is None:
            await next(ctx)
            return

        action, proposal_id = parsed
        if action == "approve":
            result = apply_persona_evolution_proposal(
                workspace=self._workspace,
                state_db_path=self._state_db_path,
                proposal_id=proposal_id,
                approved_by_channel=ctx.event.channel,
                approved_by_chat_id=ctx.event.chat_id,
                now=self._now(),
            )
        else:
            result = deny_persona_evolution_proposal(
                state_db_path=self._state_db_path,
                proposal_id=proposal_id,
                denied_by_channel=ctx.event.channel,
                denied_by_chat_id=ctx.event.chat_id,
                now=self._now(),
            )

        logger.info(
            "Persona evolution approval code consumed: proposal={} action={} status={}",
            proposal_id,
            action,
            result.status,
        )
        if result.status in {"applied", "denied", "blocked", "not_proposed"}:
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=ctx.event.channel,
                    chat_id=ctx.event.chat_id,
                    content=self._confirmation_text(result.status, result.message, result.persona_file),
                )
            )
        ctx.halt()

    @staticmethod
    def _parse_code(text: str) -> tuple[str, str] | None:
        stripped = str(text or "").strip()
        if stripped.startswith("pe-approve-"):
            proposal_id = stripped.removeprefix("pe-approve-").strip()
            return ("approve", proposal_id) if proposal_id else None
        if stripped.startswith("pe-deny-"):
            proposal_id = stripped.removeprefix("pe-deny-").strip()
            return ("deny", proposal_id) if proposal_id else None
        return None

    @classmethod
    def _parse_reply_decision(cls, text: str, reply_to_text: str | None) -> tuple[str, str] | None:
        action = cls._reply_action(text)
        if action is None:
            return None

        proposal_ids = {
            proposal_id
            for _code_action, proposal_id in _APPROVAL_CODE_RE.findall(str(reply_to_text or ""))
        }
        if len(proposal_ids) != 1:
            return None
        return action, next(iter(proposal_ids))

    @staticmethod
    def _reply_action(text: str) -> str | None:
        normalized = re.sub(r"[\s.!?,;:]+", " ", str(text or "").strip().lower()).strip()
        if normalized in _YES_REPLIES:
            return "approve"
        if normalized in _NO_REPLIES:
            return "deny"
        return None

    @staticmethod
    def _confirmation_text(status: str, message: str, persona_file: str | None) -> str:
        target = f" for {persona_file}" if persona_file else ""
        if status == "applied":
            return f"Persona evolution applied{target}."
        if status == "denied":
            return f"Persona evolution proposal denied{target}."
        return f"Persona evolution proposal {status}{target}: {message}"
