"""Hard tool boundary for Phase 1 proactive speakups."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from yeoman_shared.config.schema import Config

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.approval import PendingSpeakupApproval, SpeakupApprovalStore
from yeoman_gateway.consciousness.log import SpeakupLog
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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.policy_engine = policy_engine
        self.bus = bus
        self.log = log
        self.inbound_archive = inbound_archive
        self.memory = memory
        self.security = security
        self.approval_store = approval_store
        self._now = now or (lambda: datetime.now(UTC))
        self._proposals: dict[str, SpeakupProposal] = {}
        self._commit_lock = asyncio.Lock()
        self._trigger = "cron"

    def begin_run(self, *, trigger: str) -> None:
        self._trigger = trigger
        self._proposals.clear()

    def current_trigger(self) -> str:
        return self._trigger

    async def is_chat_within_opportunity_budget(
        self,
        channel: str,
        chat_id: str,
        *,
        trigger: str | None = None,
    ) -> bool:
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
        proposal = SpeakupProposal(
            proposal_id=proposal_id,
            channel=eligible.channel,
            chat_id=eligible.chat_id,
            message=content,
            action_type=action_type,
            profile=eligible.profile,
            confidence=float(confidence),
            trigger=self._trigger,
            context_snapshot={"confidence": float(confidence)},
            reply_to_message_id=validated_reply_to,
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

    async def commit_speakup(self, proposal_id: str) -> dict[str, object]:
        if not self.config.consciousness.enabled:
            return {"status": "rejected", "reason": "consciousness_disabled"}
        async with self._commit_lock:
            proposal = self._proposals.get(str(proposal_id))
            if proposal is None:
                return {"status": "rejected", "reason": "proposal_not_found"}
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
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=approval.owner_channel,
                        chat_id=approval.owner_chat_id,
                        content="\n".join(preview_lines),
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

            output = self.security.check_output(
                proposal.message,
                context={
                    "path": "consciousness.commit_speakup",
                    "channel": proposal.channel,
                    "chat_id": proposal.chat_id,
                },
            )
            if output.decision.action == "block":
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
            await self.log.mark_sent(proposal.proposal_id, now=self._now().timestamp())
            self._proposals.pop(proposal.proposal_id, None)
            return {"status": "sent", "proposal_id": proposal.proposal_id}

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
