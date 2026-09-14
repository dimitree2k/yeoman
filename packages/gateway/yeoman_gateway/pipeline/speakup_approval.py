"""Middleware to intercept owner approval codes for queued speakups.

An owner code is authenticated into durable CAS state (owner identity, payload
hash, target effect id, proposal revision) and then handed to the shared
``ConsciousnessTools.submit_proposal`` path. The middleware never sends on its
own: a boolean passed by a caller is not approval authority, and a proposal whose
payload changed since the preview is refused.
"""

from __future__ import annotations

from loguru import logger

from yeoman_gateway.bus.events import OutboundMessage  # noqa: F401  (public re-export)
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.tools import ConsciousnessTools, deterministic_effect_id
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
        service_effects: object | None = None,
        tools: ConsciousnessTools | None = None,
    ) -> None:
        self._store = approval_store
        self._bus = bus
        self._log = log
        self._security = security
        self._service_effects = service_effects
        self._tools = tools

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if not getattr(ctx.decision, "is_owner", False):
            await next(ctx)
            return

        content = ctx.event.content.strip()
        is_speakup_code = (
            content.startswith("spk-approve-")
            or content.startswith("spk-deny-")
        )

        if not is_speakup_code:
            expired = await self._store.purge_expired()
            for approval in expired:
                await self._log.mark_status(approval.proposal_id, status="expired")
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
            ctx.halt()
            return

        action = matched.action
        approval = matched.approval
        if matched.expired:
            await self._log.mark_status(approval.proposal_id, status="expired")
            ctx.halt()
            return

        if action == "approve":
            logger.info(
                "Speakup approval matched: {} -> {}",
                approval.proposal_id,
                approval.target_chat_id,
            )
            await self._approve(ctx, approval)
        else:
            logger.info("Speakup denied: {}", approval.proposal_id)
            await self._log.mark_status(approval.proposal_id, status="denied")

        ctx.halt()

    async def _approve(self, ctx: PipelineContext, approval: object) -> None:
        """Authenticate the owner code into durable CAS state, then submit once.

        The claim binds owner identity, payload hash, proposal revision and the
        target effect id. Concurrent or repeated codes converge on the same claim
        and the same effect; a changed payload is refused by ``submit_proposal``.
        """
        proposal_id = str(getattr(approval, "proposal_id"))
        target_channel = str(getattr(approval, "target_channel"))
        target_chat_id = str(getattr(approval, "target_chat_id"))
        payload_hash = str(getattr(approval, "payload_hash", "") or "")
        revision = int(getattr(approval, "proposal_revision", 1) or 1)
        target_effect_id = deterministic_effect_id(
            channel=target_channel,
            chat_id=target_chat_id,
            operation=str(getattr(approval, "action_type", "") or "comment"),
            proposal_id=proposal_id,
            revision=revision,
        )
        owner_id = str(
            getattr(ctx.event, "sender_id", "") or getattr(ctx.event, "participant", "") or ""
        )
        claimed = await self._log.record_approval_claim(
            proposal_id,
            owner_channel=ctx.event.channel,
            owner_chat_id=ctx.event.chat_id,
            owner_id=owner_id,
            payload_hash=payload_hash,
            proposal_revision=revision,
            target_effect_id=target_effect_id,
            now_ms=self._now_ms(),
        )
        if not claimed:
            logger.info("Speakup approval claim refused: {}", proposal_id)
            return
        if self._tools is None:
            # Compatibility path for callers that have not migrated to the shared
            # submission routine yet. The claim above already bound owner, payload
            # and target effect id, so a repeated code still converges on one send.
            await self._legacy_send(approval)
            await self._log.resolve_approval_claim(
                proposal_id,
                resolution="legacy_passthrough",
                now_ms=self._now_ms(),
            )
            return
        result = await self._tools.submit_proposal(proposal_id)
        status = str(result.get("status") or "")
        if status in {"rejected", "failed", "expired", "cancelled", "blocked"}:
            await self._log.resolve_approval_claim(
                proposal_id,
                resolution=str(result.get("reason") or status),
                now_ms=self._now_ms(),
            )

    async def _legacy_send(self, approval: object) -> None:
        """Deprecated pre-migration delivery for approvals created without tools."""
        target_channel = str(getattr(approval, "target_channel"))
        target_chat_id = str(getattr(approval, "target_chat_id"))
        message = str(getattr(approval, "message"))
        reply_to = getattr(approval, "reply_to_message_id", None)
        output = self._security.check_output(
            message,
            context={
                "path": "consciousness.approval",
                "channel": target_channel,
                "chat_id": target_chat_id,
            },
        )
        if output.decision.action == "block":
            await self._log.mark_status(
                str(getattr(approval, "proposal_id")), status="rejected",
                reason="security_output_blocked",
            )
            return
        content = (
            output.sanitized_text
            if output.decision.action == "sanitize" and output.sanitized_text
            else message
        )
        if self._service_effects is not None:
            await self._service_effects.send(
                source="speakup",
                operation_ref=(
                    f"speakup-approval:{getattr(approval, 'proposal_id')}:{target_chat_id}"
                ),
                channel=target_channel,
                chat_id=target_chat_id,
                content=content,
                reply_to=reply_to,
            )
            await self._log.mark_sent(str(getattr(approval, "proposal_id")))
            return
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=target_channel,
                chat_id=target_chat_id,
                content=content,
                reply_to=reply_to,
                metadata={
                    "spontaneous": True,
                    "approved": True,
                    "proposal_id": getattr(approval, "proposal_id"),
                    "action_type": getattr(approval, "action_type", ""),
                    "profile": getattr(approval, "profile", ""),
                    "trigger": getattr(approval, "trigger", ""),
                },
            )
        )
        await self._log.mark_sent(str(getattr(approval, "proposal_id")))

    @staticmethod
    def _now_ms() -> int:
        import time

        return int(time.time() * 1000)

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
