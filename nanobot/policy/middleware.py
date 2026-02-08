"""Policy middleware that wraps policy evaluation and runtime reloads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.policy.engine import ActorContext, EffectivePolicy, PolicyDecision, PolicyEngine
from nanobot.policy.identity import resolve_actor_identity
from nanobot.policy.loader import load_policy


@dataclass(slots=True)
class MessagePolicyContext:
    """Policy context for one inbound message."""

    actor: ActorContext
    decision: PolicyDecision
    effective_policy: EffectivePolicy | None
    persona_text: str | None
    source: str


class PolicyMiddleware:
    """Single policy predicate pipeline for inbound messages."""

    def __init__(
        self,
        engine: PolicyEngine,
        known_tools: set[str],
        policy_path: Path | None = None,
        reload_on_change: bool | None = None,
        reload_check_interval_seconds: float | None = None,
    ):
        self._engine = engine
        self._known_tools = set(known_tools)
        self._policy_path = policy_path
        runtime = self._engine.policy.runtime
        self._reload_on_change = runtime.reload_on_change if reload_on_change is None else reload_on_change
        self._reload_check_interval_seconds = (
            runtime.reload_check_interval_seconds
            if reload_check_interval_seconds is None
            else reload_check_interval_seconds
        )
        self._last_reload_check = 0.0
        self._last_mtime_ns = self._stat_mtime_ns()
        self._warned_missing_wa_mention_meta: set[str] = set()

    @property
    def engine(self) -> PolicyEngine:
        return self._engine

    def _stat_mtime_ns(self) -> int | None:
        if self._policy_path is None:
            return None
        try:
            return self._policy_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _maybe_reload(self) -> None:
        if not self._reload_on_change or self._policy_path is None:
            return
        now = time.monotonic()
        if now - self._last_reload_check < self._reload_check_interval_seconds:
            return
        self._last_reload_check = now

        current_mtime = self._stat_mtime_ns()
        if current_mtime == self._last_mtime_ns:
            return

        try:
            new_policy = load_policy(self._policy_path)
            new_engine = PolicyEngine(
                policy=new_policy,
                workspace=self._engine.workspace,
                apply_channels=self._engine.apply_channels,
            )
            new_engine.validate(self._known_tools)
        except Exception as e:
            logger.error(f"policy reload failed, keeping previous policy: {e}")
            return

        self._engine = new_engine
        self._last_mtime_ns = current_mtime
        self._warned_missing_wa_mention_meta.clear()
        runtime = self._engine.policy.runtime
        self._reload_on_change = runtime.reload_on_change
        self._reload_check_interval_seconds = runtime.reload_check_interval_seconds
        logger.info(
            "policy reloaded from {} (version={}, channels={})",
            self._policy_path,
            self._engine.policy.version,
            ",".join(sorted(self._engine.apply_channels)),
        )

    def _build_actor_context(self, msg: InboundMessage) -> ActorContext:
        metadata = msg.metadata or {}
        identity = resolve_actor_identity(msg.channel, msg.sender_id, metadata)
        return ActorContext(
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_primary=identity.primary,
            sender_aliases=list(identity.aliases),
            is_group=bool(metadata.get("is_group", False)),
            mentioned_bot=bool(metadata.get("mentioned_bot", False)),
            reply_to_bot=bool(metadata.get("reply_to_bot", False)),
        )

    def evaluate_message(self, msg: InboundMessage) -> MessagePolicyContext:
        """Evaluate one inbound message."""
        self._maybe_reload()

        actor = self._build_actor_context(msg)
        decision = self._engine.evaluate(actor, self._known_tools)
        effective = (
            self._engine.resolve_policy(actor.channel, actor.chat_id)
            if actor.channel in self._engine.apply_channels
            else None
        )
        persona_text = self._engine.persona_text(decision.persona_file)

        metadata = msg.metadata or {}
        if (
            msg.channel == "whatsapp"
            and actor.is_group
            and "mentioned_bot" not in metadata
            and "reply_to_bot" not in metadata
            and decision.reason.endswith("when_to_reply:mention_only_group")
        ):
            warning_key = f"{msg.channel}:{msg.chat_id}"
            if warning_key not in self._warned_missing_wa_mention_meta:
                logger.warning(
                    "whatsapp mention metadata missing; mention_only groups fail-closed until bridge metadata is present"
                )
                self._warned_missing_wa_mention_meta.add(warning_key)

        source = str(self._policy_path) if self._policy_path else "in-memory"
        return MessagePolicyContext(
            actor=actor,
            decision=decision,
            effective_policy=effective,
            persona_text=persona_text,
            source=source,
        )

    @staticmethod
    def filter_tool_definitions(
        tool_definitions: list[dict[str, Any]],
        allowed_tools: set[str],
    ) -> list[dict[str, Any]]:
        """Filter OpenAI tool definitions using policy allow-set."""
        return [
            schema
            for schema in tool_definitions
            if schema.get("function", {}).get("name") in allowed_tools
        ]

    @staticmethod
    def is_tool_allowed(tool_name: str, context: MessagePolicyContext) -> bool:
        return tool_name in context.decision.allowed_tools

    @staticmethod
    def allows_capability(capability: str, context: MessagePolicyContext) -> bool:
        """Internal capability gating for non-tool shortcuts."""
        if capability == "weather_fastpath":
            return "web_fetch" in context.decision.allowed_tools
        return True

    def explain(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        is_group: bool = False,
        mentioned_bot: bool = False,
        reply_to_bot: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return merged policy and decision for diagnostics."""
        meta = dict(metadata or {})
        meta["is_group"] = is_group
        meta["mentioned_bot"] = mentioned_bot
        meta["reply_to_bot"] = reply_to_bot

        msg = InboundMessage(
            channel=channel,
            sender_id=sender_id,
            chat_id=chat_id,
            content="policy explain",
            metadata=meta,
        )
        context = self.evaluate_message(msg)

        return {
            "policySource": context.source,
            "channel": channel,
            "chatId": chat_id,
            "sender": {
                "primary": context.actor.sender_primary,
                "aliases": context.actor.sender_aliases,
            },
            "effectivePolicy": (
                {
                    "whoCanTalk": {
                        "mode": context.effective_policy.who_can_talk_mode,
                        "senders": context.effective_policy.who_can_talk_senders,
                    },
                    "whenToReply": {
                        "mode": context.effective_policy.when_to_reply_mode,
                        "senders": context.effective_policy.when_to_reply_senders,
                    },
                    "allowedTools": {
                        "mode": context.effective_policy.allowed_tools_mode,
                        "tools": context.effective_policy.allowed_tools_tools,
                        "deny": context.effective_policy.allowed_tools_deny,
                    },
                    "personaFile": context.effective_policy.persona_file,
                }
                if context.effective_policy
                else None
            ),
            "decision": {
                "acceptMessage": context.decision.accept_message,
                "shouldRespond": context.decision.should_respond,
                "reason": context.decision.reason,
                "allowedTools": sorted(context.decision.allowed_tools),
                "personaFile": context.decision.persona_file,
            },
        }

