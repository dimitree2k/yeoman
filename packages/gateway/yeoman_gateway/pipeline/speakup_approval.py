"""Middleware to intercept owner approval codes for queued speakups."""

from __future__ import annotations

from loguru import logger

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.core.ports import SecurityPort


class SpeakupApprovalMiddleware:
    """Consume `spk-approve-*` and `spk-deny-*` owner replies."""

    def __init__(
        self,
        *,
        approval_store: SpeakupApprovalStore,
        bus: MessageBus,
        log: SpeakupLog,
        security: SecurityPort,
    ) -> None:
        self._store = approval_store
        self._bus = bus
        self._log = log
        self._security = security

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if not getattr(ctx.decision, "is_owner", False):
            await next(ctx)
            return

        expired = await self._store.purge_expired()
        for approval in expired:
            await self._log.mark_status(approval.proposal_id, status="expired")

        content = ctx.event.content.strip()
        if not (content.startswith("spk-approve-") or content.startswith("spk-deny-")):
            await next(ctx)
            return

        matched = None
        for owner_chat_id in self._owner_chat_candidates(ctx):
            matched = await self._store.match_and_consume(
                content,
                owner_channel=ctx.event.channel,
                owner_chat_id=owner_chat_id,
            )
            if matched is not None:
                break
        if matched is None:
            await next(ctx)
            return

        action, approval = matched
        if action == "approve":
            sent_today = await self._log.count_sent_today(
                channel=approval.target_channel,
                chat_id=approval.target_chat_id,
            )
            if sent_today >= approval.daily_cap:
                logger.info("Speakup approval rejected by daily cap: {}", approval.proposal_id)
                await self._log.mark_status(
                    approval.proposal_id,
                    status="rejected",
                    reason="daily_cap_reached",
                )
                ctx.halt()
                return
            output = self._security.check_output(
                approval.message,
                context={
                    "path": "consciousness.approval",
                    "channel": approval.target_channel,
                    "chat_id": approval.target_chat_id,
                },
            )
            if output.decision.action == "block":
                logger.info("Speakup approval blocked by output security: {}", approval.proposal_id)
                await self._log.mark_status(
                    approval.proposal_id,
                    status="rejected",
                    reason="security_output_blocked",
                )
                ctx.halt()
                return
            content = (
                output.sanitized_text
                if output.decision.action == "sanitize" and output.sanitized_text
                else approval.message
            )
            logger.info(
                "Speakup approval matched: {} -> {}",
                approval.proposal_id,
                approval.target_chat_id,
            )
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=approval.target_channel,
                    chat_id=approval.target_chat_id,
                    content=content,
                    metadata={
                        "spontaneous": True,
                        "approved": True,
                        "proposal_id": approval.proposal_id,
                        "action_type": approval.action_type,
                        "profile": approval.profile,
                        "trigger": approval.trigger,
                    },
                )
            )
            await self._log.mark_sent(approval.proposal_id)
        else:
            logger.info("Speakup denied: {}", approval.proposal_id)
            await self._log.mark_status(approval.proposal_id, status="denied")

        ctx.halt()

    @staticmethod
    def _owner_chat_candidates(ctx: PipelineContext) -> list[str]:
        candidates: list[str] = []
        for value in (ctx.event.chat_id, ctx.event.participant, ctx.event.sender_id):
            text = str(value or "").strip()
            if not text:
                continue
            candidates.append(text)
            if ctx.event.channel == "whatsapp":
                normalized = text[1:] if text.startswith("+") else text
                candidates.append(normalized)
                if "@" not in normalized:
                    candidates.append(f"{normalized}@s.whatsapp.net")

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                deduped.append(candidate)
        return deduped
