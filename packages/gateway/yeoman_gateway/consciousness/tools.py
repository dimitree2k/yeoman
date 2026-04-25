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
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.storage.inbound_archive import InboundArchive

DEFAULT_HELPFUL_ACTIONS = {
    "answer_open_question",
    "surface_memory",
    "correct_error",
    "observation",
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


@dataclass(frozen=True, slots=True)
class EligibleChat:
    channel: str
    chat_id: str
    profile: str
    daily_cap: int
    allowed_actions: frozenset[str]


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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.policy_engine = policy_engine
        self.bus = bus
        self.log = log
        self.inbound_archive = inbound_archive
        self.memory = memory
        self.security = security
        self._now = now or (lambda: datetime.now(UTC))
        self._proposals: dict[str, SpeakupProposal] = {}
        self._commit_lock = asyncio.Lock()
        self._trigger = "cron"

    def begin_run(self, *, trigger: str) -> None:
        self._trigger = trigger
        self._proposals.clear()

    async def read_eligible_chats(self) -> list[dict[str, object]]:
        return [
            {
                "channel": chat.channel,
                "chat_id": chat.chat_id,
                "profile": chat.profile,
                "daily_cap": chat.daily_cap,
                "allowed_actions": sorted(chat.allowed_actions),
            }
            for chat in self._eligible_chats()
        ]

    async def read_chat_window(self, chat_id: str, n: int = 20) -> dict[str, object]:
        eligible = self._eligible_by_chat().get(chat_id)
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "messages": []}
        now = self._now()
        since = now - timedelta(days=7)
        rows = self.inbound_archive.lookup_messages_in_range(
            eligible.channel,
            eligible.chat_id,
            since,
            now,
            limit=max(1, min(int(n), 50)),
        )
        return {"status": "ok", "messages": rows}

    async def search_memory(self, query: str, chat_id: str, limit: int = 5) -> dict[str, object]:
        eligible = self._eligible_by_chat().get(chat_id)
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

    async def read_speakup_history(self, chat_id: str, n: int = 20) -> dict[str, object]:
        eligible = self._eligible_by_chat().get(chat_id)
        if eligible is None:
            return {"status": "rejected", "reason": "chat_not_eligible", "history": []}
        history = await self.log.history(
            eligible.channel,
            eligible.chat_id,
            limit=max(1, min(int(n), 50)),
        )
        return {"status": "ok", "history": history}

    async def propose_speakup(
        self,
        *,
        chat_id: str,
        message: str,
        action_type: str,
        confidence: float,
    ) -> dict[str, object]:
        eligible = self._eligible_by_chat().get(chat_id)
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
            eligible = self._eligible_by_chat().get(proposal.chat_id)
            if eligible is None:
                await self.log.mark_rejected(proposal.proposal_id, reason="chat_not_eligible")
                return {"status": "rejected", "reason": "chat_not_eligible"}
            sent_today = await self.log.count_sent_today(
                channel=proposal.channel,
                chat_id=proposal.chat_id,
                now=self._now(),
            )
            if sent_today >= eligible.daily_cap:
                await self.log.mark_rejected(proposal.proposal_id, reason="daily_cap_reached")
                return {"status": "rejected", "reason": "daily_cap_reached"}

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
    ) -> dict[str, object]:
        eligible = self._eligible_by_chat().get(chat_id)
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

    def _eligible_by_chat(self) -> dict[str, EligibleChat]:
        return {chat.chat_id: chat for chat in self._eligible_chats()}

    def _eligible_chats(self) -> list[EligibleChat]:
        if not self.config.consciousness.enabled:
            return []
        eligible: list[EligibleChat] = []
        for channel, owners in self.policy_engine.policy.owners.items():
            if channel not in self.policy_engine.apply_channels:
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
                    else frozenset(DEFAULT_HELPFUL_ACTIONS)
                )
                eligible.append(
                    EligibleChat(
                        channel=channel,
                        chat_id=chat_id,
                        profile=profile,
                        daily_cap=int(daily_cap),
                        allowed_actions=allowed,
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
            return f"{value}@s.whatsapp.net"
        return value

    @staticmethod
    def _is_group_chat(channel: str, chat_id: str) -> bool:
        return channel == "whatsapp" and chat_id.endswith("@g.us")

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

