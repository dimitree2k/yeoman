"""Policy adapter backed directly by PolicyEngine."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, override

from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import PolicyPort
from nanobot.policy.engine import ActorContext, PolicyEngine
from nanobot.policy.identity import resolve_actor_identity
from nanobot.policy.loader import load_policy


def _to_actor(event: InboundEvent) -> ActorContext:
    identity = resolve_actor_identity(
        event.channel,
        event.sender_id,
        {
            "sender_id": event.sender_id,
            "sender": event.sender_id,
            "participant": event.participant,
            "participant_jid": event.participant,
        },
    )
    return ActorContext(
        channel=event.channel,
        chat_id=event.chat_id,
        sender_primary=identity.primary,
        sender_aliases=list(identity.aliases),
        is_group=event.is_group,
        mentioned_bot=event.mentioned_bot,
        reply_to_bot=event.reply_to_bot,
    )


class EnginePolicyAdapter(PolicyPort):
    """PolicyPort implementation using the typed `PolicyEngine` directly."""

    def __init__(
        self,
        *,
        engine: PolicyEngine | None,
        known_tools: set[str],
        policy_path: Path | None = None,
        reload_on_change: bool | None = None,
        reload_check_interval_seconds: float | None = None,
    ) -> None:
        self._engine = engine
        self._known_tools = set(known_tools)
        self._policy_path = policy_path
        self._last_reload_check = 0.0
        self._last_mtime_ns = self._stat_mtime_ns()

        if engine is None:
            self._reload_on_change = False
            self._reload_check_interval_seconds = 30.0
        else:
            runtime = engine.policy.runtime
            self._reload_on_change = runtime.reload_on_change if reload_on_change is None else reload_on_change
            self._reload_check_interval_seconds = (
                runtime.reload_check_interval_seconds
                if reload_check_interval_seconds is None
                else reload_check_interval_seconds
            )

    @property
    def known_tools(self) -> frozenset[str]:
        return frozenset(self._known_tools)

    def _stat_mtime_ns(self) -> int | None:
        if self._policy_path is None:
            return None
        try:
            return self._policy_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _maybe_reload(self) -> None:
        if self._engine is None:
            return
        if not self._reload_on_change or self._policy_path is None:
            return

        now = time.monotonic()
        if now - self._last_reload_check < self._reload_check_interval_seconds:
            return
        self._last_reload_check = now

        current_mtime = self._stat_mtime_ns()
        if current_mtime == self._last_mtime_ns:
            return

        new_policy = load_policy(self._policy_path)
        new_engine = PolicyEngine(
            policy=new_policy,
            workspace=self._engine.workspace,
            apply_channels=self._engine.apply_channels,
        )
        new_engine.validate(self._known_tools)
        self._engine = new_engine
        self._last_mtime_ns = current_mtime

    @override
    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        if self._engine is None:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset(self._known_tools),
                reason="policy_disabled",
                source="disabled",
            )

        self._maybe_reload()
        actor = _to_actor(event)
        decision = self._engine.evaluate(actor, self._known_tools)
        return PolicyDecision(
            accept_message=decision.accept_message,
            should_respond=decision.should_respond,
            allowed_tools=frozenset(decision.allowed_tools),
            reason=decision.reason,
            persona_file=decision.persona_file,
            persona_text=self._engine.persona_text(decision.persona_file),
            source=str(self._policy_path) if self._policy_path else "in-memory",
        )

    def explain(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        is_group: bool = False,
        mentioned_bot: bool = False,
        reply_to_bot: bool = False,
    ) -> dict[str, Any]:
        """Return merged policy and decision snapshot for diagnostics."""
        event = InboundEvent(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            content="policy explain",
            is_group=is_group,
            mentioned_bot=mentioned_bot,
            reply_to_bot=reply_to_bot,
        )
        actor = _to_actor(event)
        decision = self.evaluate(event)

        effective = None
        if self._engine is not None and channel in self._engine.apply_channels:
            effective = self._engine.resolve_policy(channel, chat_id)

        return {
            "policySource": decision.source,
            "channel": channel,
            "chatId": chat_id,
            "sender": {
                "primary": actor.sender_primary,
                "aliases": actor.sender_aliases,
            },
            "effectivePolicy": (
                {
                    "whoCanTalk": {
                        "mode": effective.who_can_talk_mode,
                        "senders": effective.who_can_talk_senders,
                    },
                    "whenToReply": {
                        "mode": effective.when_to_reply_mode,
                        "senders": effective.when_to_reply_senders,
                    },
                    "allowedTools": {
                        "mode": effective.allowed_tools_mode,
                        "tools": effective.allowed_tools_tools,
                        "deny": effective.allowed_tools_deny,
                    },
                    "toolAccess": effective.tool_access,
                    "personaFile": effective.persona_file,
                }
                if effective is not None
                else None
            ),
            "decision": {
                "acceptMessage": decision.accept_message,
                "shouldRespond": decision.should_respond,
                "reason": decision.reason,
                "allowedTools": sorted(decision.allowed_tools),
                "personaFile": decision.persona_file,
            },
        }
