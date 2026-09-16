"""Hard tool boundary for Phase 1 proactive speakups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from yeoman_shared.config.schema import Config

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.approval import PendingSpeakupApproval, SpeakupApprovalStore
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.persona import load_persona_text
from yeoman_gateway.storage.inbound_archive import InboundArchive

DEFAULT_HELPFUL_ACTIONS = {
    "answer_open_question",
    "surface_memory",
    "correct_error",
    "observation",
}
DEFAULT_BALANCED_ACTIONS = DEFAULT_HELPFUL_ACTIONS | {
    "share_opinion",
    "light_humor",
}
DEFAULT_PERMISSIVE_ACTIONS = DEFAULT_BALANCED_ACTIONS | {
    "cold_joke",
    "contrarian",
}
MIN_CONFIDENCE = 0.75

#: Ledger proposal states that mean "this proposal can no longer be submitted".
#: A durable row in one of these states is never re-sent, even when an in-memory
#: copy was evicted by a restart or by ``begin_run``.
TERMINAL_PROPOSAL_STATES: frozenset[str] = frozenset(
    {
        "sent",
        "transport_accepted",
        "delivered",
        "delivery_unknown",
        "failed",
        "cancelled",
        "expired",
        "denied",
        "rejected",
    }
)

#: Fallback reservation dimensions for a proposal when no explicit participation
#: policy resolves for the chat. Phase 02 replaces these with validated
#: ``processing.participation`` values from the effective policy; the ledger
#: transaction and the accounting semantics are already final.
DEFAULT_PROPOSAL_RESERVATION_LIMITS: dict[str, tuple[int, int]] = {
    "initiation": (3, 86_400_000),
    "comment": (3, 1_800_000),
    "reaction": (6, 1_800_000),
}


def canonical_payload_hash(
    *,
    channel: str,
    chat_id: str,
    content: str,
    action_type: str,
    reply_to_message_id: str | None,
    revision: int = 1,
) -> str:
    """Canonical hash over the exact normalized payload an approval authorizes.

    A changed draft, quote, target, action or proposal revision produces a
    different hash, so the old approval can never submit the new payload
    (spec section 9).
    """
    material = json.dumps(
        {
            "channel": str(channel),
            "chat_id": str(chat_id),
            "content": str(content),
            "action_type": str(action_type),
            "reply_to_message_id": (
                None if reply_to_message_id is None else str(reply_to_message_id)
            ),
            "revision": int(revision),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SpeakupProposal:
    proposal_id: str
    channel: str
    chat_id: str
    message: str
    action_type: str
    profile: str
    confidence: float
    trigger: str
    context_snapshot: dict[str, object]
    reply_to_message_id: str | None = None
    proposal_revision: int = 1
    payload_hash: str = ""


def with_proposal_revision(proposal: SpeakupProposal, revision: int) -> SpeakupProposal:
    """Return the proposal at a new revision with its payload hash recomputed."""
    updated = replace(proposal, proposal_revision=int(revision))
    return replace(
        updated,
        payload_hash=canonical_payload_hash(
            channel=updated.channel,
            chat_id=updated.chat_id,
            content=updated.message,
            action_type=updated.action_type,
            reply_to_message_id=updated.reply_to_message_id,
            revision=updated.proposal_revision,
        ),
    )


@dataclass(frozen=True, slots=True)
class EligibleChat:
    channel: str
    chat_id: str
    profile: str
    daily_cap: int
    allowed_actions: frozenset[str]
    owner_channel: str
    owner_chat_id: str
    preview: str
    is_group: bool


def _receipt_provider_message_id(receipt: object) -> str | None:
    """Provider message id from a transport receipt, when the transport reported one."""
    transport = getattr(receipt, "transport_receipt", None)
    value = getattr(transport, "provider_message_id", None) if transport else None
    text = str(value or "").strip()
    return text or None


class ConsciousnessTools:
    """Phase 1 tools with hard rails independent of model behavior."""

    def __init__(
        self,
        *,
        config: Config,
        policy_engine: PolicyEngine,
        bus: MessageBus,
        log: SpeakupLog,
        inbound_archive: InboundArchive,
        memory: object | None,
        security: object,
        approval_store: SpeakupApprovalStore | None = None,
        service_effects: object | None = None,
        now: Callable[[], datetime] | None = None,
        activation_provider: Callable[[str, str], object | None] | None = None,
    ) -> None:
        self.config = config
        self.policy_engine = policy_engine
        self.bus = bus
        self._service_effects = service_effects
        self.log = log
        self.inbound_archive = inbound_archive
        self.memory = memory
        self.security = security
        self.approval_store = approval_store
        self._now = now or (lambda: datetime.now(UTC))
        self._activation_provider = activation_provider
        self._participation_submission: object | None = None
        self._proposals: dict[str, SpeakupProposal] = {}
        self._commit_lock = asyncio.Lock()
        self._trigger = "cron"

    def begin_run(self, *, trigger: str) -> None:
        self._trigger = trigger
        self._proposals.clear()

    def current_trigger(self) -> str:
        return self._trigger

    def set_participation_submission(self, submission: object) -> None:
        """Bind approvals to the same admission-aware submission used by runtime."""
        self._participation_submission = submission

    async def is_chat_within_opportunity_budget(
        self,
        channel: str,
        chat_id: str,
        *,
        trigger: str | None = None,
    ) -> bool:
        activation = self._participation_snapshot(channel, chat_id)
        if activation is not None and (activation.live or activation.observing):
            # Observer admission is independent of the legacy planner list. Silence is
            # always available, but it is not an actionable opportunity.
            actions = await self.available_actions_for(
                channel=channel,
                chat_id=chat_id,
                now_ms=int(self._now().timestamp() * 1000),
            )
            return any(action != "silence" for action in actions)
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if not isinstance(eligible, EligibleChat):
            return False
        budget = await self._evaluate_opportunity_budget(
            eligible,
            trigger=trigger or self._trigger,
        )
        return bool(budget["allowed"])

    def _chat_window_since_for_trigger(self, *, trigger: str | None = None) -> datetime:
        now = self._now()
        active_trigger = trigger or self._trigger
        if active_trigger == "burst":
            return now - timedelta(
                minutes=max(1, int(self.config.consciousness.burst_window_minutes))
            )
        return now - timedelta(
            minutes=max(1, int(self.config.consciousness.lull_activity_window_minutes))
        )

    @staticmethod
    def _message_observed_at(row: dict[str, object]) -> datetime | None:
        raw_timestamp = row.get("timestamp")
        if raw_timestamp is not None:
            try:
                return datetime.fromtimestamp(float(raw_timestamp), UTC)
            except (TypeError, ValueError, OSError):
                pass
        raw_created_at = str(row.get("created_at") or "").strip()
        if not raw_created_at:
            return None
        try:
            parsed = datetime.fromisoformat(raw_created_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _reply_to_is_fresh_for_trigger(self, row: dict[str, object]) -> bool:
        observed_at = self._message_observed_at(row)
        if observed_at is None:
            return False
        return observed_at >= self._chat_window_since_for_trigger()

    async def read_eligible_chats(self) -> list[dict[str, object]]:
        return [
            {
                "channel": chat.channel,
                "chat_id": chat.chat_id,
                "profile": chat.profile,
                "daily_cap": chat.daily_cap,
                "allowed_actions": sorted(chat.allowed_actions),
                "preview": chat.preview,
                "is_group": chat.is_group,
            }
            for chat in self._eligible_chats()
        ]

    def is_chat_eligible(self, channel: str, chat_id: str) -> bool:
        return any(
            chat.channel == channel and chat.chat_id == chat_id
            for chat in self._eligible_chats()
        )

    async def read_chat_window(
        self,
        chat_id: str,
        n: int = 20,
        *,
        channel: str | None = None,
        trigger: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id", "messages": []}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "messages": []}
        now = self._now()
        since = self._chat_window_since_for_trigger(trigger=trigger)
        rows = self.inbound_archive.lookup_messages_in_range(
            eligible.channel,
            eligible.chat_id,
            since,
            now,
            limit=max(1, min(int(n), 50)),
            latest=True,
        )
        return {"status": "ok", "messages": rows}

    async def search_memory(
        self,
        query: str,
        chat_id: str,
        limit: int = 5,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id", "hits": []}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "hits": []}
        if self.memory is None or not hasattr(self.memory, "search"):
            return {"status": "ok", "hits": []}
        hits = self.memory.search(
            query=query,
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            scope="chat",
            limit=max(1, min(int(limit), 10)),
        )
        rendered: list[dict[str, object]] = []
        for hit in hits:
            entry = getattr(hit, "entry", None)
            content = getattr(entry, "content", str(hit))
            rendered.append({"content": str(content)})
        return {"status": "ok", "hits": rendered}

    async def read_learned_chat_taste(
        self,
        chat_id: str,
        limit: int = 5,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id", "patterns": []}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "patterns": []}
        if self.memory is None or not hasattr(self.memory, "learned_chat_taste"):
            return {"status": "ok", "patterns": []}
        hits = self.memory.learned_chat_taste(
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            limit=max(1, min(int(limit), 10)),
        )
        patterns: list[dict[str, object]] = []
        for hit in hits:
            entry = getattr(hit, "entry", None)
            content = getattr(entry, "content", str(hit))
            patterns.append(
                {
                    "content": str(content),
                    "confidence": getattr(entry, "confidence", None),
                    "updated_at": getattr(entry, "updated_at", None),
                }
            )
        return {"status": "ok", "patterns": patterns}

    async def read_speakup_history(
        self,
        chat_id: str,
        n: int = 20,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id", "history": []}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "history": []}
        history = await self.log.history(
            eligible.channel,
            eligible.chat_id,
            limit=max(1, min(int(n), 50)),
        )
        return {"status": "ok", "history": history}

    async def read_persona_for_chat(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id"}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible"}
        resolved = self.policy_engine.resolve_policy(eligible.channel, eligible.chat_id)
        try:
            text = load_persona_text(resolved.persona_file, self.policy_engine.workspace)
        except Exception:
            return {"status": "ok", "persona": None}
        return {"status": "ok", "persona": text}

    async def read_daily_usage(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id"}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible"}
        budget = await self._evaluate_opportunity_budget(
            eligible,
            trigger=self._trigger,
        )
        return {
            "status": "ok",
            "daily_cap": budget["effective_daily_cap"],
            "base_daily_cap": budget["base_daily_cap"],
            "max_daily_cap": budget["max_daily_cap"],
            "sent_today": budget["sent_today"],
            "daily_remaining": budget["daily_remaining"],
            "budget_allowed": budget["allowed"],
            "budget_reason": budget["reason"],
        }

    async def propose_speakup(
        self,
        *,
        chat_id: str,
        message: str,
        action_type: str,
        confidence: float,
        channel: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "rejected", "reason": "ambiguous_chat_id"}
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible"}
        content = str(message or "").strip()
        if not content:
            return {"status": "rejected", "reason": "empty_message"}
        if len(content) > int(self.config.consciousness.max_speakup_length_chars):
            return {"status": "rejected", "reason": "message_too_long"}
        if action_type not in eligible.allowed_actions:
            return {"status": "rejected", "reason": "action_not_allowed"}
        if float(confidence) < MIN_CONFIDENCE:
            return {"status": "rejected", "reason": "low_confidence"}

        validated_reply_to: str | None = None
        if reply_to_message_id:
            candidate = str(reply_to_message_id).strip()
            if candidate:
                row = self.inbound_archive.lookup_message(
                    eligible.channel, eligible.chat_id, candidate
                )
                if row is not None:
                    if not self._reply_to_is_fresh_for_trigger(row):
                        return {
                            "status": "rejected",
                            "reason": "stale_reply_to_message",
                        }
                    validated_reply_to = candidate

        proposal_id = uuid.uuid4().hex
        payload_hash = canonical_payload_hash(
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            content=content,
            action_type=action_type,
            reply_to_message_id=validated_reply_to,
            revision=1,
        )
        proposal = SpeakupProposal(
            proposal_id=proposal_id,
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            message=content,
            action_type=action_type,
            profile=eligible.profile,
            confidence=float(confidence),
            trigger=self._trigger,
            context_snapshot={
                "confidence": float(confidence),
                "reply_to_message_id": validated_reply_to,
                "payload_hash": payload_hash,
                "proposal_revision": 1,
            },
            reply_to_message_id=validated_reply_to,
            proposal_revision=1,
            payload_hash=payload_hash,
        )
        self._proposals[proposal_id] = proposal
        await self.log.record_proposed(
            proposal_id=proposal_id,
            channel=proposal.channel,
            chat_id=proposal.chat_id,
            action_type=proposal.action_type,
            profile=proposal.profile,
            message=proposal.message,
            trigger=proposal.trigger,
            context_snapshot=proposal.context_snapshot,
            now=self._now().timestamp(),
        )
        return {"status": "proposed", "proposal_id": proposal_id}

    async def stage_participation_approval(
        self,
        *,
        opportunity: object,
        decision: object,
        admission: object,
        effect_id: str,
        content: str,
        snapshot: object,
    ) -> dict[str, object]:
        """Persist and preview one exact Participation draft for owner approval."""
        del decision, snapshot
        from yeoman_gateway.processing.models import (
            TextPayload,
            canonical_hash,
            payload_to_mapping,
        )
        from yeoman_gateway.processing.participation_runtime import (
            ParticipationAdmission,
        )

        if not isinstance(admission, ParticipationAdmission):
            return {"status": "approval_queue_failed", "reason": "invalid_admission"}
        proposal_id = str(getattr(opportunity, "opportunity_id", "") or "")
        channel = str(getattr(opportunity, "channel", "") or "")
        chat_id = str(getattr(opportunity, "chat_id", "") or "")
        expected_effect_id = deterministic_effect_id(
            channel=channel,
            chat_id=chat_id,
            operation="comment",
            proposal_id=proposal_id,
        )
        if not proposal_id or str(effect_id) != expected_effect_id:
            return {"status": "approval_queue_failed", "reason": "effect_id_mismatch"}
        if self._service_effects is None:
            return {
                "status": "approval_queue_failed",
                "reason": "approval_path_unavailable",
            }

        output = self.security.check_output(
            str(content),
            context={
                "path": "participation.approval_preview",
                "channel": channel,
                "chat_id": chat_id,
            },
        )
        if output.decision.action == "block":
            return {"status": "approval_queue_failed", "reason": "security_output_blocked"}
        normalized = (
            output.sanitized_text
            if output.decision.action == "sanitize" and output.sanitized_text
            else str(content)
        )
        normalized = str(normalized or "").strip()
        if not normalized:
            return {"status": "approval_queue_failed", "reason": "empty_message"}
        payload_hash = canonical_hash(payload_to_mapping(TextPayload(text=normalized)))
        prepared_admission = replace(
            admission,
            payload_hash=payload_hash,
            approval_revision=max(1, int(admission.approval_revision or 0)),
        )

        try:
            resolved = self.policy_engine.resolve_policy(channel, chat_id)
        except Exception:
            return {"status": "approval_queue_failed", "reason": "policy_unavailable"}
        if str(resolved.spontaneity_preview or "") != "owner_dm":
            return {"status": "approval_queue_failed", "reason": "approval_not_required"}
        owner_chat_id = next(
            (
                candidate
                for owner in self.policy_engine.policy.owners.get(channel, [])
                if (candidate := self._owner_dm_chat_id(channel, owner))
                and not self._is_group_chat(channel, candidate)
            ),
            "",
        )
        if not owner_chat_id:
            return {"status": "approval_queue_failed", "reason": "owner_unavailable"}

        revision = max(1, int(prepared_admission.approval_revision))
        approval_hash = canonical_payload_hash(
            channel=channel,
            chat_id=chat_id,
            content=normalized,
            action_type="comment",
            reply_to_message_id=None,
            revision=revision,
        )
        now = self._now()
        expires_at = now.timestamp() + float(
            self.config.consciousness.approval_timeout_seconds
        )
        context_snapshot: dict[str, object] = {
            "origin": "participation",
            "effect_id": str(effect_id),
            "payload_hash": approval_hash,
            "proposal_revision": revision,
            "participation_admission": asdict(prepared_admission),
            "approval_expires_at_ms": int(expires_at * 1000),
            "approval_owner_channel": channel,
            "approval_owner_chat_id": owner_chat_id,
        }
        await self.log.record_proposed(
            proposal_id=proposal_id,
            channel=channel,
            chat_id=chat_id,
            action_type="comment",
            profile=str(resolved.spontaneity_profile or "helpful"),
            message=normalized,
            trigger=str(getattr(opportunity, "trigger", "inbound") or "inbound"),
            context_snapshot=context_snapshot,
            now=now.timestamp(),
        )
        approval = PendingSpeakupApproval(
            proposal_id=proposal_id,
            target_channel=channel,
            target_chat_id=chat_id,
            owner_channel=channel,
            owner_chat_id=owner_chat_id,
            message=normalized,
            action_type="comment",
            profile=str(resolved.spontaneity_profile or "helpful"),
            created_at=now.timestamp(),
            expires_at=expires_at,
            context_snapshot=context_snapshot,
            trigger=str(getattr(opportunity, "trigger", "inbound") or "inbound"),
            daily_cap=max(0, int(resolved.spontaneity_daily_cap or 0)),
            payload_hash=approval_hash,
            proposal_revision=revision,
        )
        preview_content = "\n".join(
            (
                f"Proposed spontaneous message for {chat_id}",
                f"Message: {normalized}",
                f"Approve: {approval.approve_code}",
                f"Deny: {approval.deny_code}",
            )
        )
        preview_ref = f"participation-preview:{proposal_id}"
        preview_effect_id = deterministic_effect_id(
            channel=channel,
            chat_id=owner_chat_id,
            operation="preview",
            proposal_id=proposal_id,
        )
        try:
            receipt = await self._service_effects.send(
                source="speakup",
                operation_ref=preview_ref,
                channel=channel,
                chat_id=owner_chat_id,
                content=preview_content,
                effect_id=preview_effect_id,
            )
        except Exception:
            await self.log.mark_rejected(proposal_id, reason="preview_failed")
            return {"status": "approval_queue_failed", "reason": "preview_failed"}
        await self.log.record_preview_effect(
            proposal_id,
            preview_effect_id=preview_effect_id,
            preview_operation_ref=preview_ref,
            accepted=bool(getattr(receipt, "accepted", False)),
            now=now.timestamp(),
        )
        return {"status": "awaiting_approval", "effect_id": str(effect_id)}

    async def commit_speakup(self, proposal_id: str) -> dict[str, object]:
        """Stage a proposal: preview it to the owner, or submit it through the one
        final validation path. Every submission decision lives in
        :meth:`submit_proposal`; this method only decides whether an owner approval
        is required first."""
        proposal = await self._load_proposal(proposal_id)
        if proposal is None:
            return {"status": "rejected", "reason": "proposal_not_found"}
        if not self.config.consciousness.enabled:
            return {"status": "rejected", "reason": "consciousness_disabled"}
        async with self._commit_lock:
            eligible = self._resolve_eligible(proposal.chat_id, channel=proposal.channel)
            if eligible is None:
                await self.log.mark_rejected(proposal.proposal_id, reason="chat_not_eligible")
                return {"status": "rejected", "reason": "chat_not_eligible"}
            budget = await self._evaluate_opportunity_budget(
                eligible,
                trigger=proposal.trigger,
                confidence=proposal.confidence,
            )
            if not budget["allowed"]:
                reason = str(budget["reason"])
                await self.log.mark_rejected(proposal.proposal_id, reason=reason)
                return {"status": "rejected", "reason": reason}

            if eligible.is_group and eligible.preview == "owner_dm":
                if self.approval_store is None:
                    await self.log.mark_rejected(
                        proposal.proposal_id,
                        reason="approval_store_unavailable",
                    )
                    return {"status": "rejected", "reason": "approval_store_unavailable"}
                approval = PendingSpeakupApproval(
                    proposal_id=proposal.proposal_id,
                    target_channel=proposal.channel,
                    target_chat_id=proposal.chat_id,
                    owner_channel=eligible.owner_channel,
                    owner_chat_id=eligible.owner_chat_id,
                    message=proposal.message,
                    action_type=proposal.action_type,
                    profile=proposal.profile,
                    created_at=self._now().timestamp(),
                    expires_at=(
                        self._now().timestamp()
                        + float(self.config.consciousness.approval_timeout_seconds)
                    ),
                    context_snapshot=dict(proposal.context_snapshot),
                    trigger=proposal.trigger,
                    daily_cap=eligible.daily_cap,
                    reply_to_message_id=proposal.reply_to_message_id,
                    payload_hash=proposal.payload_hash,
                    proposal_revision=proposal.proposal_revision,
                )
                await self.approval_store.add(approval)
                await self.log.mark_status(
                    proposal.proposal_id,
                    status="queued_for_approval",
                )
                preview_lines = [
                    f"Proposed spontaneous message for {approval.target_chat_id}",
                ]
                quoted = self._render_quoted_preview(proposal)
                if quoted:
                    preview_lines.append(quoted)
                preview_lines.extend(
                    [
                        f"Message: {proposal.message}",
                        f"Approve: {approval.approve_code}",
                        f"Deny: {approval.deny_code}",
                    ]
                )
                preview_content = "\n".join(preview_lines)
                if self._service_effects is not None:
                    preview_ref = f"speakup-preview:{proposal.proposal_id}"
                    preview_effect_id = deterministic_effect_id(
                        channel=approval.owner_channel,
                        chat_id=approval.owner_chat_id,
                        operation="preview",
                        proposal_id=proposal.proposal_id,
                    )
                    receipt = await self._service_effects.send(
                        source="speakup",
                        operation_ref=preview_ref,
                        channel=approval.owner_channel,
                        chat_id=approval.owner_chat_id,
                        content=preview_content,
                        effect_id=preview_effect_id,
                    )
                    # A preview is an owner-destination effect. It never touches the
                    # target chat and therefore never consumes a target send allowance
                    # (spec section 9). The proposal stays queued for approval; the
                    # owner-destination effect is recorded separately from target truth.
                    await self.log.record_preview_effect(
                        proposal.proposal_id,
                        preview_effect_id=preview_effect_id,
                        preview_operation_ref=preview_ref,
                        accepted=bool(getattr(receipt, "accepted", False)),
                        now=self._now().timestamp(),
                    )
                    await self.log.mark_status(
                        proposal.proposal_id, status="awaiting_approval"
                    )
                    return {
                        "status": "queued_for_approval",
                        "proposal_id": proposal.proposal_id,
                    }
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=approval.owner_channel,
                        chat_id=approval.owner_chat_id,
                        content=preview_content,
                        metadata={
                            "spontaneous": True,
                            "preview": True,
                            "proposal_id": proposal.proposal_id,
                            "target_chat_id": approval.target_chat_id,
                            "action_type": proposal.action_type,
                            "profile": proposal.profile,
                            "trigger": proposal.trigger,
                            "reply_to_message_id": proposal.reply_to_message_id,
                        },
                    )
                )
                self._proposals.pop(proposal.proposal_id, None)
                return {"status": "queued_for_approval", "proposal_id": proposal.proposal_id}

        return await self.submit_proposal(proposal_id)

    async def submit_proposal(self, proposal_id: str) -> dict[str, object]:
        """The single final validation and submission path for a proposal.

        Order (spec section 9): load durable proposal/approval -> current
        eligibility, access and pause -> approval validity -> freshness/quote ->
        action/budget reservation -> output security -> stable managed effect ->
        record the evidenced ledger state. A caller-supplied boolean is never
        approval authority, and a changed payload under an old approval is refused.
        """
        async with self._commit_lock:
            key = str(proposal_id)
            # A repeated code for an already-submitted proposal returns the same
            # claim/effect status instead of "not found" or a second send.
            repeated = await self.log.approval_claim(key)
            if repeated is not None and str(repeated["state"]) == "terminal":
                resolution = str(repeated["resolution"] or "")
                if resolution in {"submitted", "legacy_passthrough"}:
                    return {
                        "status": "transport_accepted",
                        "proposal_id": key,
                        "effect_id": str(repeated["target_effect_id"] or ""),
                        "duplicate": True,
                    }
                return {"status": "rejected", "reason": f"approval_{resolution}"}

            proposal = await self._load_proposal(key)
            if proposal is None:
                return {"status": "rejected", "reason": "proposal_not_found"}
            if str(proposal.context_snapshot.get("origin") or "") == "participation":
                return await self._submit_participation_proposal(
                    proposal, approval_claim=repeated
                )
            if not self.config.consciousness.enabled:
                return {"status": "rejected", "reason": "consciousness_disabled"}

            approval_claim = repeated

            eligible = self._resolve_eligible(proposal.chat_id, channel=proposal.channel)
            if eligible is None:
                await self.log.mark_rejected(proposal.proposal_id, reason="chat_not_eligible")
                return {"status": "rejected", "reason": "chat_not_eligible"}
            if eligible.is_group and eligible.preview == "owner_dm":
                needs_approval = True
            else:
                needs_approval = False
            if needs_approval and approval_claim is None:
                return {"status": "rejected", "reason": "approval_required"}

            # A durable approval binds one exact payload and revision.
            effect_id = deterministic_effect_id(
                channel=proposal.channel,
                chat_id=proposal.chat_id,
                operation=proposal.action_type,
                proposal_id=proposal.proposal_id,
                revision=proposal.proposal_revision,
            )
            if needs_approval and approval_claim is not None:
                if str(approval_claim["payload_hash"]) != proposal.payload_hash:
                    await self.log.mark_rejected(
                        proposal.proposal_id, reason="approval_payload_changed"
                    )
                    return {"status": "rejected", "reason": "approval_payload_changed"}
                if int(approval_claim["proposal_revision"]) != proposal.proposal_revision:
                    await self.log.mark_rejected(
                        proposal.proposal_id, reason="approval_revision_changed"
                    )
                    return {"status": "rejected", "reason": "approval_revision_changed"}
                if str(approval_claim["target_effect_id"]) not in {"", effect_id}:
                    await self.log.mark_rejected(
                        proposal.proposal_id, reason="approval_target_changed"
                    )
                    return {"status": "rejected", "reason": "approval_target_changed"}

            budget = await self._evaluate_opportunity_budget(
                eligible,
                trigger=proposal.trigger,
                confidence=proposal.confidence,
            )
            if not budget["allowed"]:
                reason = str(budget["reason"])
                await self.log.mark_rejected(proposal.proposal_id, reason=reason)
                return {"status": "rejected", "reason": reason}

            if proposal.reply_to_message_id:
                row = self.inbound_archive.lookup_message(
                    proposal.channel, proposal.chat_id, proposal.reply_to_message_id
                )
                if row is None:
                    await self.log.mark_rejected(
                        proposal.proposal_id, reason="stale_quote"
                    )
                    return {"status": "rejected", "reason": "stale_quote"}

            reservation_limits = self._reservation_limits(proposal)
            reserved = await self.log.reserve_delivery(
                proposal_id=proposal.proposal_id,
                effect_id=effect_id,
                channel=proposal.channel,
                chat_id=proposal.chat_id,
                now_ms=int(self._now().timestamp() * 1000),
                limits=reservation_limits,
                proposal_revision=proposal.proposal_revision,
            )
            if not reserved:
                await self.log.mark_rejected(
                    proposal.proposal_id, reason="delivery_budget_exhausted"
                )
                return {"status": "rejected", "reason": "delivery_budget_exhausted"}
            output = self.security.check_output(
                proposal.message,
                context={
                    "path": "consciousness.submit_proposal",
                    "channel": proposal.channel,
                    "chat_id": proposal.chat_id,
                },
            )
            if output.decision.action == "block":
                await self.log.release_delivery(
                    proposal.proposal_id,
                    effect_id=effect_id,
                    state="failed",
                    reason="security_output_blocked",
                    now_ms=int(self._now().timestamp() * 1000),
                )
                await self.log.mark_rejected(
                    proposal.proposal_id,
                    reason="security_output_blocked",
                )
                return {"status": "rejected", "reason": "security_output_blocked"}
            content = (
                output.sanitized_text
                if output.decision.action == "sanitize" and output.sanitized_text
                else proposal.message
            )
            if content != proposal.message:
                # A sanitizer that changes the payload invalidates the approval.
                await self.log.release_delivery(
                    proposal.proposal_id,
                    effect_id=effect_id,
                    state="failed",
                    reason="sanitized_payload_changed",
                    now_ms=int(self._now().timestamp() * 1000),
                )
                await self.log.mark_rejected(
                    proposal.proposal_id, reason="sanitized_payload_changed"
                )
                return {"status": "rejected", "reason": "sanitized_payload_changed"}

            await self.log.record_send_attempt(
                proposal.proposal_id,
                effect_id=effect_id,
                now_ms=int(self._now().timestamp() * 1000),
            )

            if self._service_effects is None:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=proposal.channel,
                        chat_id=proposal.chat_id,
                        content=content,
                        reply_to=proposal.reply_to_message_id,
                        metadata={
                            "spontaneous": True,
                            "proposal_id": proposal.proposal_id,
                            "action_type": proposal.action_type,
                            "profile": proposal.profile,
                            "trigger": proposal.trigger,
                        },
                    )
                )
                await self.log.mark_sent(
                    proposal.proposal_id, now=self._now().timestamp()
                )
                self._proposals.pop(proposal.proposal_id, None)
                return {"status": "sent", "proposal_id": proposal.proposal_id}

            # Exactly one target effect per proposal revision, fixed target,
            # managed delivery only: no raw outbound fallback and no None receipt
            # accepted as success.
            try:
                receipt = await self._service_effects.send(
                    source="speakup",
                    operation_ref=f"speakup:{proposal.proposal_id}:{proposal.proposal_revision}",
                    channel=proposal.channel,
                    chat_id=proposal.chat_id,
                    content=content,
                    reply_to=proposal.reply_to_message_id,
                    effect_id=effect_id,
                    require_managed=True,
                )
            except Exception:
                # A refused/errored transport leaves the pending authorization
                # recoverable: the claim stays claimed and can be retried.
                await self.log.mark_status(proposal.proposal_id, status="submitted")
                raise
            if receipt is None:
                await self.log.mark_status(proposal.proposal_id, status="submitted")
                return {"status": "rejected", "reason": "managed_delivery_required"}

            state = str(getattr(receipt, "state", "") or "")
            if state == "sent":
                await self.log.project_transport_accepted(
                    proposal.proposal_id,
                    effect_id=effect_id,
                    provider_message_id=_receipt_provider_message_id(receipt),
                    evidence_kind="transport_receipt",
                    evidence_ref=str(getattr(receipt, "attempt_id", "") or effect_id),
                    now_ms=int(self._now().timestamp() * 1000),
                )
                await self.log.resolve_approval_claim(
                    proposal.proposal_id,
                    resolution="submitted",
                    now_ms=int(self._now().timestamp() * 1000),
                )
                self._proposals.pop(proposal.proposal_id, None)
                return {
                    "status": "transport_accepted",
                    "proposal_id": proposal.proposal_id,
                    "effect_id": effect_id,
                }
            if state in {"failed", "cancelled", "expired", "blocked"}:
                await self.log.mark_status(proposal.proposal_id, status=state)
                self._proposals.pop(proposal.proposal_id, None)
                return {"status": state, "proposal_id": proposal.proposal_id}
            await self.log.mark_status(proposal.proposal_id, status="submitted")
            return {
                "status": "submitted",
                "proposal_id": proposal.proposal_id,
                "effect_id": effect_id,
            }

    async def _submit_participation_proposal(
        self,
        proposal: SpeakupProposal,
        *,
        approval_claim: dict[str, Any] | None,
    ) -> dict[str, object]:
        """Submit one approved new-lane proposal through its bound managed effect."""
        from yeoman_gateway.processing.models import (
            TextPayload,
            canonical_hash,
            payload_to_mapping,
        )
        from yeoman_gateway.processing.participation_runtime import (
            ParticipationAdmission,
        )

        if approval_claim is None:
            return {"status": "rejected", "reason": "approval_required"}
        snapshot = proposal.context_snapshot
        effect_id = str(snapshot.get("effect_id") or "")
        now_ms = int(self._now().timestamp() * 1000)
        if int(snapshot.get("approval_expires_at_ms") or 0) <= now_ms:
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="expired",
                reason="approval_expired",
                now_ms=now_ms,
            )
            await self.log.resolve_approval_claim(
                proposal.proposal_id,
                resolution="expired",
                now_ms=now_ms,
            )
            await self.log.mark_status(proposal.proposal_id, status="expired")
            return {"status": "expired", "reason": "approval_expired"}
        if (
            str(approval_claim.get("state") or "") != "claimed"
            or str(approval_claim.get("payload_hash") or "") != proposal.payload_hash
            or int(approval_claim.get("proposal_revision") or 0)
            != int(proposal.proposal_revision)
            or str(approval_claim.get("target_effect_id") or "") != effect_id
            or str(approval_claim.get("owner_channel") or "")
            != str(snapshot.get("approval_owner_channel") or "")
            or str(approval_claim.get("owner_chat_id") or "")
            != str(snapshot.get("approval_owner_chat_id") or "")
        ):
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="failed",
                reason="approval_binding_changed",
                now_ms=now_ms,
            )
            await self.log.mark_rejected(
                proposal.proposal_id, reason="approval_binding_changed"
            )
            return {"status": "rejected", "reason": "approval_binding_changed"}
        raw_admission = snapshot.get("participation_admission")
        if not isinstance(raw_admission, dict):
            return {"status": "rejected", "reason": "approval_admission_missing"}
        values = dict(raw_admission)
        values["source_event_ids"] = tuple(values.get("source_event_ids") or ())
        values["source_principals"] = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in (values.get("source_principals") or ())
            if isinstance(pair, (tuple, list)) and len(pair) == 2
        )
        try:
            admission = ParticipationAdmission(**values)
        except (TypeError, ValueError):
            return {"status": "rejected", "reason": "approval_admission_invalid"}
        expected_effect_id = deterministic_effect_id(
            channel=proposal.channel,
            chat_id=proposal.chat_id,
            operation="comment",
            proposal_id=proposal.proposal_id,
        )
        expected_payload_hash = canonical_hash(
            payload_to_mapping(TextPayload(text=proposal.message))
        )
        if (
            effect_id != expected_effect_id
            or admission.opportunity_id != proposal.proposal_id
            or admission.channel != proposal.channel
            or admission.chat_id != proposal.chat_id
            or admission.payload_hash != expected_payload_hash
            or int(admission.approval_revision) != int(proposal.proposal_revision)
        ):
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="failed",
                reason="approval_admission_changed",
                now_ms=int(self._now().timestamp() * 1000),
            )
            return {"status": "rejected", "reason": "approval_admission_changed"}

        output = self.security.check_output(
            proposal.message,
            context={
                "path": "participation.approval_submit",
                "channel": proposal.channel,
                "chat_id": proposal.chat_id,
            },
        )
        content = (
            output.sanitized_text
            if output.decision.action == "sanitize" and output.sanitized_text
            else proposal.message
        )
        if output.decision.action == "block" or content != proposal.message:
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="failed",
                reason="approved_payload_changed",
                now_ms=int(self._now().timestamp() * 1000),
            )
            await self.log.mark_rejected(
                proposal.proposal_id, reason="approved_payload_changed"
            )
            return {"status": "rejected", "reason": "approved_payload_changed"}
        submit = getattr(self._participation_submission, "submit", None)
        if not callable(submit):
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="failed",
                reason="participation_submission_unavailable",
                now_ms=int(self._now().timestamp() * 1000),
            )
            return {
                "status": "rejected",
                "reason": "participation_submission_unavailable",
            }

        try:
            outcome = await submit(
                admission=admission,
                effect_id=effect_id,
                content=proposal.message,
                payload_hash=admission.payload_hash,
            )
        except Exception:
            # The effect path owns possible-dispatch classification. Keep the hold
            # until the same deterministic effect is reconciled.
            await self.log.mark_status(proposal.proposal_id, status="submitted")
            raise
        state = str(getattr(outcome, "status", "") or "submitted")
        if state == "sent":
            await self.log.resolve_approval_claim(
                proposal.proposal_id,
                resolution="submitted",
                now_ms=now_ms,
            )
            await self.log.mark_status(
                proposal.proposal_id, status="transport_accepted"
            )
            self._proposals.pop(proposal.proposal_id, None)
            return {
                "status": "transport_accepted",
                "proposal_id": proposal.proposal_id,
                "effect_id": effect_id,
            }
        if state in {"failed", "cancelled", "expired", "blocked", "not_executed"}:
            await self.log.release_delivery(
                proposal.proposal_id,
                effect_id=effect_id,
                state="failed",
                reason=f"effect_{state}",
                now_ms=now_ms,
            )
            await self.log.mark_status(proposal.proposal_id, status=state)
            return {"status": state, "proposal_id": proposal.proposal_id}
        await self.log.mark_status(proposal.proposal_id, status="submitted")
        return {
            "status": "submitted",
            "proposal_id": proposal.proposal_id,
            "effect_id": effect_id,
        }

    def _reservation_limits(
        self, proposal: SpeakupProposal
    ) -> tuple[tuple[str, int, int, str], ...]:
        """Every capacity dimension one proposal must acquire before sending.

        An unsolicited initiating comment consumes both initiation and comment
        capacity; a reaction consumes its own dimension. Authority to *try* is
        enforced by the ledger transaction, so a zero limit denies the action
        rather than silently skipping the check.
        """
        categories = self._reservation_categories(proposal)
        limits: list[tuple[str, int, int, str]] = []
        for category in categories:
            window_limit, window_ms, window_kind = self.participation_limits_for(
                channel=proposal.channel,
                chat_id=proposal.chat_id,
                category=category,
            )
            limits.append((category, window_limit, window_ms, window_kind))
        return tuple(limits)

    @staticmethod
    def _reservation_categories(proposal: SpeakupProposal) -> list[str]:
        if proposal.action_type in {"reaction", "react"}:
            return ["reaction"]
        if proposal.trigger in {"continuation", "reply"}:
            return ["comment"]
        return ["initiation", "comment"]

    def participation_limits_for(
        self, *, channel: str, chat_id: str, category: str
    ) -> tuple[int, int, str]:
        """Effective reservation limits for one chat, from resolved policy.

        The values come from the chat's participation policy; the module fallbacks
        keep the legacy single-owner path working while participation is disabled.
        """
        try:
            resolved = self.policy_engine.resolve_participation(channel, chat_id)
        except Exception:
            resolved = None
        if resolved is not None:
            if category == "reaction":
                return (
                    int(resolved.max_reactions_per_window),
                    int(resolved.comment_window_minutes) * 60_000,
                    "rolling",
                )
            return (
                int(resolved.max_unsolicited_comments_per_window),
                int(resolved.comment_window_minutes) * 60_000,
                "rolling",
            )
        fallback_limit, fallback_window = DEFAULT_PROPOSAL_RESERVATION_LIMITS[category]
        window_kind = "calendar_day" if category == "initiation" else "rolling"
        return (fallback_limit, fallback_window, window_kind)

    async def available_actions_for(
        self, *, channel: str, chat_id: str, now_ms: int
    ) -> tuple[str, ...]:
        """The action set the judge may choose from, after hard policy and budgets.

        Silence is always possible. An action with zero remaining capacity is not
        offered, so the provider is never asked a question whose answer is already
        refused. This is a preflight, not an authorization: the ledger transaction
        still decides at reservation time.
        """
        resolved = self.policy_engine.resolve_participation(channel, chat_id)
        actions: list[str] = ["silence"]
        if resolved.allow_reactions:
            limit, window_ms, window_kind = self.participation_limits_for(
                channel=channel, chat_id=chat_id, category="reaction"
            )
            if limit > 0 and await self._has_capacity(
                channel=channel,
                chat_id=chat_id,
                category="reaction",
                limit=limit,
                window_ms=window_ms,
                window_kind=window_kind,
                now_ms=now_ms,
            ):
                actions.append("react")
        if resolved.allow_continuation or resolved.allow_initiation:
            limit, window_ms, window_kind = self.participation_limits_for(
                channel=channel, chat_id=chat_id, category="comment"
            )
            if limit > 0 and await self._has_capacity(
                channel=channel,
                chat_id=chat_id,
                category="comment",
                limit=limit,
                window_ms=window_ms,
                window_kind=window_kind,
                now_ms=now_ms,
            ):
                actions.append("comment")
        return tuple(actions)

    async def _has_capacity(
        self,
        *,
        channel: str,
        chat_id: str,
        category: str,
        limit: int,
        window_ms: int,
        window_kind: str,
        now_ms: int,
    ) -> bool:
        used = await self.log.consumed_slots(
            channel=channel,
            chat_id=chat_id,
            category=category,
            now_ms=now_ms,
            window_ms=window_ms,
            window_kind=window_kind,
        )
        return used < int(limit)

    async def _load_proposal(self, proposal_id: str) -> SpeakupProposal | None:
        """Rehydrate a proposal from durable storage (survives a restart).

        A proposal whose durable row is already terminal is not reloaded as if it
        were fresh: without this check a repeated commit of a long-sent proposal
        would publish again from the ledger copy.
        """
        key = str(proposal_id)
        cached = self._proposals.get(key)
        if cached is not None:
            return cached
        row = await self.log.proposal_row(key)
        if row is None:
            return None
        if str(row.get("status") or "") in TERMINAL_PROPOSAL_STATES:
            return None
        snapshot = row.get("context_snapshot_json") or "{}"
        try:
            parsed = json.loads(str(snapshot))
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        message = str(row.get("message") or "")
        action_type = str(row.get("action_type") or "")
        channel = str(row.get("channel") or "")
        chat_id = str(row.get("chat_id") or "")
        reply_to = parsed.get("reply_to_message_id")
        revision = int(parsed.get("proposal_revision") or 1)
        proposal = SpeakupProposal(
            proposal_id=key,
            channel=channel,
            chat_id=chat_id,
            message=message,
            action_type=action_type,
            profile=str(row.get("profile") or ""),
            confidence=float(parsed.get("confidence") or 0.0),
            trigger=str(row.get("trigger") or ""),
            context_snapshot=parsed,
            reply_to_message_id=str(reply_to) if reply_to else None,
            proposal_revision=revision,
            payload_hash=str(
                parsed.get("payload_hash")
                or canonical_payload_hash(
                    channel=channel,
                    chat_id=chat_id,
                    content=message,
                    action_type=action_type,
                    reply_to_message_id=str(reply_to) if reply_to else None,
                    revision=revision,
                )
            ),
        )
        self._proposals[key] = proposal
        return proposal

    async def record_silent_pass(
        self,
        *,
        chat_id: str,
        reason: str,
        trigger: str,
        channel: str | None = None,
    ) -> dict[str, object]:
        eligible = self._resolve_eligible(chat_id, channel=channel)
        if eligible == "ambiguous_chat_id":
            return {"status": "silent_pass", "reason": "ambiguous_chat_id"}
        if eligible is None:
            return {"status": "silent_pass", "reason": reason}
        entry_id = await self.log.record_silent_pass(
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            profile=eligible.profile,
            trigger=trigger,
            reason=reason,
            now=self._now().timestamp(),
        )
        return {"status": "silent_pass", "entry_id": entry_id, "reason": reason}

    async def _evaluate_opportunity_budget(
        self,
        eligible: EligibleChat,
        *,
        trigger: str,
        confidence: float | None = None,
    ) -> dict[str, object]:
        sent_today = await self.log.count_sent_today(
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            now=self._now(),
        )
        base_cap = max(0, int(eligible.daily_cap))
        cfg = self.config.consciousness
        dynamic_enabled = bool(cfg.dynamic_daily_cap_enabled)
        max_cap = base_cap
        if dynamic_enabled:
            max_cap = max(base_cap, int(cfg.dynamic_daily_cap_max))
        effective_cap = max_cap if dynamic_enabled else base_cap

        def result(allowed: bool, reason: str, cap: int = effective_cap) -> dict[str, object]:
            return {
                "allowed": allowed,
                "reason": reason,
                "sent_today": sent_today,
                "base_daily_cap": base_cap,
                "max_daily_cap": max_cap,
                "effective_daily_cap": cap,
                "daily_remaining": max(0, cap - sent_today),
            }

        if max_cap <= 0:
            return result(False, "daily_cap_disabled", cap=0)
        if sent_today >= max_cap:
            reason = "dynamic_daily_cap_reached" if dynamic_enabled else "daily_cap_reached"
            return result(False, reason)

        gap_minutes = int(cfg.min_speakup_gap_minutes)
        if gap_minutes > 0:
            last_sent_at = await self.log.last_sent_at(
                channel=eligible.channel,
                chat_id=eligible.chat_id,
            )
            if last_sent_at is not None:
                age_seconds = self._now().timestamp() - last_sent_at
                if age_seconds < gap_minutes * 60:
                    return result(False, "recent_speakup_cooldown")

        if trigger == "burst" and int(cfg.burst_max_per_window) > 0:
            recent_sent = await self.log.count_sent_since(
                channel=eligible.channel,
                chat_id=eligible.chat_id,
                since=self._now()
                - timedelta(minutes=max(1, int(cfg.burst_window_minutes))),
            )
            if recent_sent >= int(cfg.burst_max_per_window):
                return result(False, "burst_window_cap_reached")

        if not dynamic_enabled:
            if sent_today >= base_cap:
                return result(False, "daily_cap_reached")
            return result(True, "base_daily_cap_available")

        reserved_slots = min(max(0, int(cfg.reserved_daily_slots)), base_cap)
        reserve_until_hour = int(cfg.reserve_daily_slots_until_hour)
        if (
            reserved_slots
            and self._now().hour < reserve_until_hour
            and sent_today >= max(0, base_cap - reserved_slots)
        ):
            return result(False, "reserved_daily_slot", cap=max(0, base_cap - reserved_slots))

        if sent_today < base_cap:
            return result(True, "base_daily_cap_available")

        if confidence is not None and confidence < float(cfg.dynamic_daily_cap_min_confidence):
            return result(False, "dynamic_confidence_too_low")

        if trigger == "burst":
            window = await self.read_chat_window(
                eligible.chat_id,
                n=max(1, int(cfg.dynamic_daily_cap_min_activity_messages)),
                channel=eligible.channel,
                trigger=trigger,
            )
            messages = window.get("messages")
            recent_activity = len(messages) if isinstance(messages, list) else 0
            if recent_activity < int(cfg.dynamic_daily_cap_min_activity_messages):
                return result(False, "dynamic_activity_too_low")

        return result(True, "dynamic_extra_slot_available")

    def _render_quoted_preview(self, proposal: SpeakupProposal) -> str | None:
        if not proposal.reply_to_message_id:
            return None
        row = self.inbound_archive.lookup_message(
            proposal.channel, proposal.chat_id, proposal.reply_to_message_id
        )
        if row is None:
            return None
        sender = str(row.get("sender_name") or row.get("sender_id") or "?").strip() or "?"
        text = str(row.get("text") or "").strip().replace("\n", " ")
        if len(text) > 140:
            text = text[:137] + "..."
        return f'In reply to {sender}: "{text}"'

    def _eligible_by_chat(self) -> dict[tuple[str, str], EligibleChat]:
        return {(chat.channel, chat.chat_id): chat for chat in self._eligible_chats()}

    def _resolve_eligible(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> EligibleChat | None | str:
        chat_id = str(chat_id or "").strip()
        channel = str(channel or "").strip() or None
        if not chat_id:
            return None
        by_key = self._eligible_by_chat()
        if channel is not None:
            return by_key.get((channel, chat_id))
        matches = [
            chat
            for (_candidate_channel, candidate_chat_id), chat in by_key.items()
            if candidate_chat_id == chat_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous_chat_id"
        return None

    def _eligible_chats(self) -> list[EligibleChat]:
        if not self.config.consciousness.enabled:
            return []
        eligible: list[EligibleChat] = []
        for channel, owners in self.policy_engine.policy.owners.items():
            if channel not in self.policy_engine.apply_channels:
                continue
            owner_chat_id = ""
            for owner in owners:
                owner_chat_id = self._owner_dm_chat_id(channel, owner)
                if owner_chat_id:
                    break
            if not owner_chat_id:
                continue
            for owner in owners:
                chat_id = self._owner_dm_chat_id(channel, owner)
                if not chat_id or self._is_group_chat(channel, chat_id):
                    continue
                if self._participation_owns(channel, chat_id):
                    continue
                resolved = self.policy_engine.resolve_policy(channel, chat_id)
                if self._explicit_chat_disabled(channel, chat_id):
                    continue
                if not resolved.spontaneity_enabled and not self.config.consciousness.owner_dm_default_enabled:
                    continue
                profile = resolved.spontaneity_profile
                if profile in {"", "off"}:
                    profile = "helpful"
                if profile != "helpful":
                    continue
                if self._in_quiet_hours(
                    resolved.spontaneity_quiet_hours_start,
                    resolved.spontaneity_quiet_hours_end,
                ):
                    continue
                daily_cap = (
                    resolved.spontaneity_daily_cap
                    if resolved.spontaneity_daily_cap is not None
                    else self.config.consciousness.default_daily_cap
                )
                if daily_cap <= 0:
                    continue
                allowed = (
                    frozenset(resolved.spontaneity_allowed_actions)
                    if resolved.spontaneity_allowed_actions is not None
                    else self._default_allowed_actions(profile)
                )
                eligible.append(
                    EligibleChat(
                        channel=channel,
                        chat_id=chat_id,
                        profile=profile,
                        daily_cap=int(daily_cap),
                        allowed_actions=allowed,
                        owner_channel=channel,
                        owner_chat_id=owner_chat_id,
                        preview=resolved.spontaneity_preview or "none",
                        is_group=False,
                    )
                )
            channel_policy = self.policy_engine.policy.channels.get(channel)
            if channel_policy is None:
                continue
            for chat_id, override in channel_policy.chats.items():
                if not self._is_group_chat(channel, chat_id):
                    continue
                if override.spontaneity is None or override.spontaneity.enabled is not True:
                    continue
                if self._participation_owns(channel, chat_id):
                    # The participation lane is this chat's production owner for social
                    # decisions, so the independent full-draft planner stands down rather
                    # than producing a second answer to the same material (spec 3.1).
                    continue
                resolved = self.policy_engine.resolve_policy(channel, chat_id)
                if self._in_quiet_hours(
                    resolved.spontaneity_quiet_hours_start,
                    resolved.spontaneity_quiet_hours_end,
                ):
                    continue
                profile = resolved.spontaneity_profile
                if profile in {"", "off"}:
                    continue
                daily_cap = (
                    resolved.spontaneity_daily_cap
                    if resolved.spontaneity_daily_cap is not None
                    else self.config.consciousness.default_daily_cap
                )
                if daily_cap <= 0:
                    continue
                preview = resolved.spontaneity_preview or "owner_dm"
                allowed = (
                    frozenset(resolved.spontaneity_allowed_actions)
                    if resolved.spontaneity_allowed_actions is not None
                    else self._default_allowed_actions(profile)
                )
                eligible.append(
                    EligibleChat(
                        channel=channel,
                        chat_id=chat_id,
                        profile=profile,
                        daily_cap=int(daily_cap),
                        allowed_actions=allowed,
                        owner_channel=channel,
                        owner_chat_id=owner_chat_id,
                        preview=preview,
                        is_group=True,
                    )
                )
        return eligible

    def _participation_owns(self, channel: str, chat_id: str) -> bool:
        """Whether the participation lane owns this chat's social decisions.

        The policy engine's snapshot is the shared activation matrix used by the adapter
        and the observer. Any resolution failure leaves production ownership with legacy.
        """
        snapshot = self._participation_snapshot(channel, chat_id)
        return bool(snapshot is not None and snapshot.live)

    def _participation_snapshot(self, channel: str, chat_id: str):
        """Resolve the canonical activation matrix from the real processing config."""
        current = self._activation_provider
        if current is None:
            current = getattr(self.policy_engine, "current_activation", None)
        if current is not None:
            try:
                return current(channel, chat_id)
            except Exception:  # noqa: BLE001 - an unreadable activation cannot create work
                return None
        processing = getattr(self.config, "processing", None)
        resolver = getattr(self.policy_engine, "resolve_participation_snapshot", None)
        if processing is None or resolver is None:
            return None
        managed_checker = getattr(processing, "is_chat_enabled", None)
        shadow_checker = getattr(processing, "is_chat_shadowed", None)
        try:
            managed = bool(managed_checker(channel, chat_id)) if managed_checker else False
            processing_shadowed = (
                bool(shadow_checker(channel, chat_id)) if shadow_checker else False
            )
            return resolver(
                channel,
                chat_id,
                processing_config=processing,
                managed=managed,
                processing_shadowed=processing_shadowed,
            )
        except Exception:  # noqa: BLE001 - an unreadable policy cannot create new work
            return None

    def _explicit_chat_disabled(self, channel: str, chat_id: str) -> bool:
        channel_policy = self.policy_engine.policy.channels.get(channel)
        if channel_policy is None:
            return False
        override = channel_policy.chats.get(chat_id)
        if override is None or override.spontaneity is None:
            return False
        return override.spontaneity.enabled is False

    @staticmethod
    def _owner_dm_chat_id(channel: str, owner: str) -> str:
        value = str(owner or "").strip()
        if not value:
            return ""
        if channel == "whatsapp" and "@" not in value:
            phone = value[1:] if value.startswith("+") else value
            if phone.isdigit():
                return f"{phone}@s.whatsapp.net"
        return value

    @staticmethod
    def _is_group_chat(channel: str, chat_id: str) -> bool:
        return channel == "whatsapp" and chat_id.endswith("@g.us")

    @staticmethod
    def _default_allowed_actions(profile: str) -> frozenset[str]:
        if profile == "permissive":
            return frozenset(DEFAULT_PERMISSIVE_ACTIONS)
        if profile == "balanced":
            return frozenset(DEFAULT_BALANCED_ACTIONS)
        return frozenset(DEFAULT_HELPFUL_ACTIONS)

    def _in_quiet_hours(self, start: str | None, end: str | None) -> bool:
        if not start or not end:
            return False
        start_min = self._parse_hhmm(start)
        end_min = self._parse_hhmm(end)
        if start_min is None or end_min is None:
            return False
        now = self._now()
        current = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= current < end_min
        return current >= start_min or current < end_min

    @staticmethod
    def _parse_hhmm(value: str) -> int | None:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour * 60 + minute
