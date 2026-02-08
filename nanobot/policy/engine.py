"""Policy evaluation engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.policy.persona import load_persona_text, resolve_persona_path
from nanobot.policy.schema import ChatPolicy, ChatPolicyOverride, PolicyConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base. Lists are replaced, not appended."""
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


@dataclass(slots=True)
class ActorContext:
    """Normalized actor/channel context for policy decisions."""

    channel: str
    chat_id: str
    sender_primary: str
    sender_aliases: list[str]
    is_group: bool
    mentioned_bot: bool
    reply_to_bot: bool


@dataclass(slots=True)
class EffectivePolicy:
    """Policy resolved for one actor context."""

    who_can_talk_mode: str
    who_can_talk_senders: list[str]
    when_to_reply_mode: str
    when_to_reply_senders: list[str]
    allowed_tools_mode: str
    allowed_tools_tools: list[str]
    allowed_tools_deny: list[str]
    persona_file: str | None


@dataclass(slots=True)
class PolicyDecision:
    """Final policy decision for message handling."""

    accept_message: bool
    should_respond: bool
    allowed_tools: set[str]
    persona_file: str | None
    reason: str


class PolicyEngine:
    """Evaluates per-channel/per-chat policy rules."""

    def __init__(
        self,
        policy: PolicyConfig,
        workspace: Path,
        apply_channels: set[str] | None = None,
    ):
        self.policy = policy
        self.workspace = workspace.expanduser().resolve()
        self.apply_channels = {"telegram", "whatsapp"} if apply_channels is None else set(apply_channels)

    def validate(self, known_tools: set[str]) -> None:
        """Validate high-risk policy issues at startup."""
        self._validate_owner_only()
        self._validate_tools(known_tools)
        self._validate_persona_paths()

    def _validate_owner_only(self) -> None:
        default_mode = self.policy.defaults.who_can_talk.mode
        reply_default_mode = self.policy.defaults.when_to_reply.mode
        for channel in self.apply_channels:
            owners = self.policy.owners.get(channel, [])
            if not owners and (default_mode == "owner_only" or reply_default_mode == "owner_only"):
                raise ValueError(
                    f"policy owner_only configured but owners.{channel} is empty"
                )

        for channel, channel_policy in self.policy.channels.items():
            if channel not in self.apply_channels:
                continue
            owners = self.policy.owners.get(channel, [])
            if not owners and self._channel_uses_owner_only(channel_policy):
                raise ValueError(
                    f"policy owner_only configured for {channel} but owners.{channel} is empty"
                )

    @staticmethod
    def _channel_uses_owner_only(channel_policy: Any) -> bool:
        if PolicyEngine._override_uses_owner_only(channel_policy.default):
            return True
        return any(PolicyEngine._override_uses_owner_only(ov) for ov in channel_policy.chats.values())

    @staticmethod
    def _override_uses_owner_only(override: ChatPolicyOverride) -> bool:
        who = override.who_can_talk.mode if override.who_can_talk else None
        rep = override.when_to_reply.mode if override.when_to_reply else None
        return who == "owner_only" or rep == "owner_only"

    def _validate_tools(self, known_tools: set[str]) -> None:
        for mode, allow, deny, path in self._iter_tool_policy_refs():
            if mode == "allowlist":
                unknown = sorted(set(allow) - known_tools)
                if unknown:
                    raise ValueError(f"unknown tools in allowlist at {path}: {unknown}")
            unknown_deny = sorted(set(deny) - known_tools)
            if unknown_deny:
                raise ValueError(f"unknown tools in deny list at {path}: {unknown_deny}")

    def _validate_persona_paths(self) -> None:
        for persona_file, path in self._iter_persona_refs():
            if not persona_file:
                continue
            try:
                resolve_persona_path(persona_file, self.workspace)
            except ValueError as e:
                raise ValueError(f"invalid personaFile at {path}: {e}") from e

    def _iter_tool_policy_refs(self) -> list[tuple[str, list[str], list[str], str]]:
        refs: list[tuple[str, list[str], list[str], str]] = []
        d = self.policy.defaults.allowed_tools
        refs.append((d.mode, d.tools, d.deny, "defaults.allowedTools"))
        for channel, cp in self.policy.channels.items():
            if cp.default.allowed_tools:
                ad = cp.default.allowed_tools
                refs.append((ad.mode or "all", ad.tools or [], ad.deny or [], f"channels.{channel}.default.allowedTools"))
            for chat, ov in cp.chats.items():
                if ov.allowed_tools:
                    ao = ov.allowed_tools
                    refs.append((ao.mode or "all", ao.tools or [], ao.deny or [], f"channels.{channel}.chats.{chat}.allowedTools"))
        return refs

    def _iter_persona_refs(self) -> list[tuple[str | None, str]]:
        refs: list[tuple[str | None, str]] = [(self.policy.defaults.persona_file, "defaults.personaFile")]
        for channel, cp in self.policy.channels.items():
            refs.append((cp.default.persona_file, f"channels.{channel}.default.personaFile"))
            for chat, ov in cp.chats.items():
                refs.append((ov.persona_file, f"channels.{channel}.chats.{chat}.personaFile"))
        return refs

    def resolve_policy(self, channel: str, chat_id: str) -> EffectivePolicy:
        """Resolve policy with precedence defaults -> channel default -> chat override."""
        merged = self.policy.defaults.model_dump()
        channel_policy = self.policy.channels.get(channel)
        if channel_policy:
            merged = _deep_merge(merged, channel_policy.default.model_dump(exclude_none=True))
            chat_override = channel_policy.chats.get(chat_id)
            if chat_override:
                merged = _deep_merge(merged, chat_override.model_dump(exclude_none=True))
        resolved = ChatPolicy.model_validate(merged)
        return EffectivePolicy(
            who_can_talk_mode=resolved.who_can_talk.mode,
            who_can_talk_senders=resolved.who_can_talk.senders,
            when_to_reply_mode=resolved.when_to_reply.mode,
            when_to_reply_senders=resolved.when_to_reply.senders,
            allowed_tools_mode=resolved.allowed_tools.mode,
            allowed_tools_tools=resolved.allowed_tools.tools,
            allowed_tools_deny=resolved.allowed_tools.deny,
            persona_file=resolved.persona_file,
        )

    @staticmethod
    def _sender_match(sender_primary: str, sender_aliases: list[str], allowed: list[str]) -> bool:
        if not allowed:
            return False
        normalized = {sender_primary.strip(), *[a.strip() for a in sender_aliases if a.strip()]}
        return any(item.strip() in normalized for item in allowed if item.strip())

    def _owner_match(self, actor: ActorContext) -> bool:
        owners = self.policy.owners.get(actor.channel, [])
        return self._sender_match(actor.sender_primary, actor.sender_aliases, owners)

    def _evaluate_who_can_talk(self, actor: ActorContext, policy: EffectivePolicy) -> tuple[bool, str]:
        mode = policy.who_can_talk_mode
        if mode == "everyone":
            return True, "who_can_talk:everyone"
        if mode == "allowlist":
            ok = self._sender_match(actor.sender_primary, actor.sender_aliases, policy.who_can_talk_senders)
            return ok, "who_can_talk:allowlist"
        if mode == "owner_only":
            return self._owner_match(actor), "who_can_talk:owner_only"
        return False, f"who_can_talk:unknown_mode:{mode}"

    def _evaluate_when_to_reply(self, actor: ActorContext, policy: EffectivePolicy) -> tuple[bool, str]:
        mode = policy.when_to_reply_mode
        if mode == "all":
            return True, "when_to_reply:all"
        if mode == "off":
            return False, "when_to_reply:off"
        if mode == "mention_only":
            if not actor.is_group:
                return True, "when_to_reply:mention_only_dm"
            ok = actor.mentioned_bot or actor.reply_to_bot
            return ok, "when_to_reply:mention_only_group"
        if mode == "allowed_senders":
            ok = self._sender_match(actor.sender_primary, actor.sender_aliases, policy.when_to_reply_senders)
            return ok, "when_to_reply:allowed_senders"
        if mode == "owner_only":
            return self._owner_match(actor), "when_to_reply:owner_only"
        return False, f"when_to_reply:unknown_mode:{mode}"

    @staticmethod
    def _resolve_allowed_tools(policy: EffectivePolicy, all_tools: set[str]) -> set[str]:
        if policy.allowed_tools_mode == "all":
            allowed = set(all_tools)
        else:
            allowed = set(policy.allowed_tools_tools)
        allowed -= set(policy.allowed_tools_deny)
        allowed &= all_tools
        # Guardrail: deny spawn whenever exec is denied.
        if "exec" not in allowed:
            allowed.discard("spawn")
        return allowed

    def evaluate(self, actor: ActorContext, all_tools: set[str]) -> PolicyDecision:
        """Evaluate policy decision for an actor."""
        # Policy currently applies to Telegram/WhatsApp only.
        if actor.channel not in self.apply_channels:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=set(all_tools),
                persona_file=None,
                reason="policy_not_applied",
            )

        policy = self.resolve_policy(actor.channel, actor.chat_id)
        accepted, accept_reason = self._evaluate_who_can_talk(actor, policy)
        if not accepted:
            return PolicyDecision(
                accept_message=False,
                should_respond=False,
                allowed_tools=set(),
                persona_file=policy.persona_file,
                reason=accept_reason,
            )

        should_respond, reply_reason = self._evaluate_when_to_reply(actor, policy)
        if not should_respond:
            return PolicyDecision(
                accept_message=True,
                should_respond=False,
                allowed_tools=set(),
                persona_file=policy.persona_file,
                reason=reply_reason,
            )

        return PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=self._resolve_allowed_tools(policy, all_tools),
            persona_file=policy.persona_file,
            reason=f"{accept_reason}|{reply_reason}",
        )

    def persona_text(self, persona_file: str | None) -> str | None:
        """Load persona text for a decision."""
        return load_persona_text(persona_file, self.workspace)
