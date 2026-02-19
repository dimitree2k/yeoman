"""Policy evaluation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanobot.policy.identity import normalize_identity_token, normalize_sender_list
from nanobot.policy.persona import load_persona_text, resolve_persona_path
from nanobot.policy.schema import ChatPolicy, ChatPolicyOverride, MemoryNotesMode, PolicyConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base. Lists are replaced, not appended."""
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _normalize_tool_names(values: list[str]) -> frozenset[str]:
    normalized = {value.strip() for value in values if value.strip()}
    return frozenset(normalized)


def _normalize_wake_text(value: str) -> str:
    lowered = str(value or "").lower()
    if not lowered:
        return ""
    normalized = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return " ".join(normalized.split())


def _normalize_wake_phrases(values: list[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for phrase in values:
        norm = _normalize_wake_text(phrase)
        if norm:
            normalized.add(norm)
    return frozenset(normalized)


def _wake_phrase_match(content: str, wake_phrases: frozenset[str]) -> bool:
    if not wake_phrases:
        return False
    normalized = _normalize_wake_text(content)
    if not normalized:
        return False
    haystack = f" {normalized} "
    for phrase in wake_phrases:
        needle = f" {phrase} "
        if needle in haystack:
            return True
    return False


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
    content: str = ""
    is_voice: bool = False


@dataclass(slots=True)
class EffectivePolicy:
    """Policy resolved for one actor context."""

    who_can_talk_mode: str
    who_can_talk_senders: list[str]
    when_to_reply_mode: str
    when_to_reply_senders: list[str]
    blocked_senders: list[str]
    allowed_tools_mode: str
    allowed_tools_tools: list[str]
    allowed_tools_deny: list[str]
    tool_access: dict[str, dict[str, Any]]
    persona_file: str | None
    voice_input_wake_phrases: list[str]
    voice_output_mode: str
    voice_output_tts_route: str
    voice_output_voice: str
    voice_output_format: str
    voice_output_max_sentences: int
    voice_output_max_chars: int


@dataclass(slots=True)
class PolicyDecision:
    """Final policy decision for message handling."""

    accept_message: bool
    should_respond: bool
    allowed_tools: set[str]
    persona_file: str | None
    reason: str


@dataclass(slots=True)
class MemoryNotesDecision:
    """Resolved background memory-notes settings for one chat context."""

    enabled: bool
    mode: MemoryNotesMode
    allow_blocked_senders: bool
    batch_interval_seconds: int
    batch_max_messages: int
    source: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _CompiledToolAccessRule:
    mode: str
    senders: frozenset[str]


@dataclass(frozen=True, slots=True)
class _CompiledPolicy:
    who_can_talk_mode: str
    who_can_talk_senders: frozenset[str]
    when_to_reply_mode: str
    when_to_reply_senders: frozenset[str]
    blocked_senders: frozenset[str]
    allowed_tools_mode: str
    allowed_tools_tools: frozenset[str]
    allowed_tools_deny: frozenset[str]
    tool_access_rules: dict[str, _CompiledToolAccessRule]
    persona_file: str | None
    voice_input_wake_phrases: frozenset[str]
    voice_output_mode: str
    voice_output_tts_route: str
    voice_output_voice: str
    voice_output_format: str
    voice_output_max_sentences: int
    voice_output_max_chars: int


@dataclass(frozen=True, slots=True)
class _CompiledMemoryNotesSettings:
    enabled: bool | None
    mode: MemoryNotesMode | None
    allow_blocked_senders: bool | None


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
        self._owner_index: dict[str, frozenset[str]] = {}
        self._channel_defaults: dict[str, _CompiledPolicy] = {}
        self._chat_rules: dict[tuple[str, str], _CompiledPolicy] = {}
        self._resolved_cache: dict[tuple[str, str], _CompiledPolicy] = {}
        self._memory_notes_apply_channels: set[str] = set()
        self._memory_notes_batch_interval_seconds = 1800
        self._memory_notes_batch_max_messages = 100
        self._memory_notes_defaults = _CompiledMemoryNotesSettings(
            enabled=True,
            mode="adaptive",
            allow_blocked_senders=False,
        )
        self._memory_notes_channel_defaults: dict[str, _CompiledMemoryNotesSettings] = {}
        self._memory_notes_chat_overrides: dict[tuple[str, str], _CompiledMemoryNotesSettings] = {}
        self._compile()

    def _compile(self) -> None:
        self._owner_index = {
            channel: normalize_sender_list(channel, owners)
            for channel, owners in self.policy.owners.items()
        }

        def dump_override(override: ChatPolicyOverride) -> dict[str, Any]:
            # Human-only fields (like comment) must not affect evaluation.
            return override.model_dump(exclude_none=True, exclude={"comment"})

        channels_to_compile = set(self.apply_channels) | set(self.policy.channels.keys())
        for channel in channels_to_compile:
            merged = self.policy.defaults.model_dump()
            channel_policy = self.policy.channels.get(channel)
            if channel_policy:
                merged = _deep_merge(merged, dump_override(channel_policy.default))
            resolved = ChatPolicy.model_validate(merged)
            compiled_default = self._compile_chat_policy(channel, resolved)
            self._channel_defaults[channel] = compiled_default

            if channel_policy:
                for chat_id, override in channel_policy.chats.items():
                    chat_merged = _deep_merge(merged, dump_override(override))
                    chat_resolved = ChatPolicy.model_validate(chat_merged)
                    self._chat_rules[(channel, chat_id)] = self._compile_chat_policy(channel, chat_resolved)

        self._resolved_cache.clear()
        self._compile_memory_notes()

    @staticmethod
    def _compile_memory_notes_override(override: Any) -> _CompiledMemoryNotesSettings:
        return _CompiledMemoryNotesSettings(
            enabled=override.enabled,
            mode=override.mode,
            allow_blocked_senders=override.allow_blocked_senders,
        )

    def _compile_memory_notes(self) -> None:
        notes = self.policy.memory_notes
        self._memory_notes_apply_channels = {
            str(channel).strip()
            for channel in notes.apply_channels
            if str(channel).strip()
        }
        self._memory_notes_batch_interval_seconds = int(notes.batch.interval_seconds)
        self._memory_notes_batch_max_messages = int(notes.batch.max_messages)
        self._memory_notes_defaults = _CompiledMemoryNotesSettings(
            enabled=bool(notes.defaults.groups_enabled),
            mode=notes.defaults.mode,
            allow_blocked_senders=bool(notes.defaults.allow_blocked_senders),
        )
        self._memory_notes_channel_defaults.clear()
        self._memory_notes_chat_overrides.clear()
        for channel, cfg in notes.channels.items():
            self._memory_notes_channel_defaults[channel] = self._compile_memory_notes_override(
                cfg.default
            )
            for chat_id, override in cfg.chats.items():
                self._memory_notes_chat_overrides[(channel, chat_id)] = (
                    self._compile_memory_notes_override(override)
                )

    @staticmethod
    def _compile_chat_policy(channel: str, resolved: ChatPolicy) -> _CompiledPolicy:
        return _CompiledPolicy(
            who_can_talk_mode=resolved.who_can_talk.mode,
            who_can_talk_senders=normalize_sender_list(channel, resolved.who_can_talk.senders),
            when_to_reply_mode=resolved.when_to_reply.mode,
            when_to_reply_senders=normalize_sender_list(channel, resolved.when_to_reply.senders),
            blocked_senders=normalize_sender_list(channel, resolved.blocked_senders.senders),
            allowed_tools_mode=resolved.allowed_tools.mode,
            allowed_tools_tools=_normalize_tool_names(resolved.allowed_tools.tools),
            allowed_tools_deny=_normalize_tool_names(resolved.allowed_tools.deny),
            tool_access_rules={
                tool_name.strip(): _CompiledToolAccessRule(
                    mode=rule.mode,
                    senders=normalize_sender_list(channel, rule.senders),
                )
                for tool_name, rule in resolved.tool_access.items()
                if tool_name.strip()
            },
            persona_file=resolved.persona_file,
            voice_input_wake_phrases=_normalize_wake_phrases(resolved.voice.input.wake_phrases),
            voice_output_mode=str(resolved.voice.output.mode),
            voice_output_tts_route=str(resolved.voice.output.tts_route),
            voice_output_voice=str(resolved.voice.output.voice),
            voice_output_format=str(resolved.voice.output.format),
            voice_output_max_sentences=int(resolved.voice.output.max_sentences),
            voice_output_max_chars=int(resolved.voice.output.max_chars),
        )

    def resolve_compiled_policy(self, channel: str, chat_id: str) -> _CompiledPolicy:
        """Resolve compiled policy with precedence defaults -> channel default -> chat override."""
        key = (channel, chat_id)
        cached = self._resolved_cache.get(key)
        if cached is not None:
            return cached

        compiled = self._chat_rules.get(key)
        if compiled is None:
            compiled = self._channel_defaults[channel]

        self._resolved_cache[key] = compiled
        return compiled

    def validate(self, known_tools: set[str]) -> None:
        """Validate high-risk policy issues at startup."""
        self._validate_owner_only()
        self._validate_tools(known_tools)
        self._validate_persona_paths()

    def _validate_owner_only(self) -> None:
        default_mode = self.policy.defaults.who_can_talk.mode
        reply_default_mode = self.policy.defaults.when_to_reply.mode
        default_tool_owner_only = any(rule.mode == "owner_only" for rule in self.policy.defaults.tool_access.values())
        for channel in self.apply_channels:
            owners = self._owner_index.get(channel, frozenset())
            if not owners and (default_mode == "owner_only" or reply_default_mode == "owner_only" or default_tool_owner_only):
                raise ValueError(f"policy owner_only configured but owners.{channel} is empty")

        for channel, channel_policy in self.policy.channels.items():
            if channel not in self.apply_channels:
                continue
            owners = self._owner_index.get(channel, frozenset())
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
        tool_owner_only = any(
            rule.mode == "owner_only"
            for rule in (override.tool_access or {}).values()
        )
        return who == "owner_only" or rep == "owner_only" or tool_owner_only

    def _validate_tools(self, known_tools: set[str]) -> None:
        for mode, allow, deny, path in self._iter_tool_policy_refs():
            if mode == "allowlist":
                unknown = sorted(set(allow) - known_tools)
                if unknown:
                    raise ValueError(f"unknown tools in allowlist at {path}: {unknown}")
            unknown_deny = sorted(set(deny) - known_tools)
            if unknown_deny:
                raise ValueError(f"unknown tools in deny list at {path}: {unknown_deny}")
        for tools, path in self._iter_tool_access_refs():
            unknown = sorted({tool.strip() for tool in tools if tool.strip()} - known_tools)
            if unknown:
                raise ValueError(f"unknown tools in toolAccess at {path}: {unknown}")

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
                refs.append(
                    (
                        ad.mode or "all",
                        ad.tools or [],
                        ad.deny or [],
                        f"channels.{channel}.default.allowedTools",
                    )
                )
            for chat, ov in cp.chats.items():
                if ov.allowed_tools:
                    ao = ov.allowed_tools
                    refs.append(
                        (
                            ao.mode or "all",
                            ao.tools or [],
                            ao.deny or [],
                            f"channels.{channel}.chats.{chat}.allowedTools",
                        )
                    )
        return refs

    def _iter_tool_access_refs(self) -> list[tuple[list[str], str]]:
        refs: list[tuple[list[str], str]] = []
        refs.append((list(self.policy.defaults.tool_access.keys()), "defaults.toolAccess"))
        for channel, cp in self.policy.channels.items():
            if cp.default.tool_access:
                refs.append((list(cp.default.tool_access.keys()), f"channels.{channel}.default.toolAccess"))
            for chat, ov in cp.chats.items():
                if ov.tool_access:
                    refs.append((list(ov.tool_access.keys()), f"channels.{channel}.chats.{chat}.toolAccess"))
        return refs

    def _iter_persona_refs(self) -> list[tuple[str | None, str]]:
        refs: list[tuple[str | None, str]] = [(self.policy.defaults.persona_file, "defaults.personaFile")]
        for channel, cp in self.policy.channels.items():
            refs.append((cp.default.persona_file, f"channels.{channel}.default.personaFile"))
            for chat, ov in cp.chats.items():
                refs.append((ov.persona_file, f"channels.{channel}.chats.{chat}.personaFile"))
        return refs

    def resolve_policy(self, channel: str, chat_id: str) -> EffectivePolicy:
        """Return resolved policy in non-compiled form."""
        resolved = self.resolve_compiled_policy(channel, chat_id)
        return EffectivePolicy(
            who_can_talk_mode=resolved.who_can_talk_mode,
            who_can_talk_senders=sorted(resolved.who_can_talk_senders),
            when_to_reply_mode=resolved.when_to_reply_mode,
            when_to_reply_senders=sorted(resolved.when_to_reply_senders),
            blocked_senders=sorted(resolved.blocked_senders),
            allowed_tools_mode=resolved.allowed_tools_mode,
            allowed_tools_tools=sorted(resolved.allowed_tools_tools),
            allowed_tools_deny=sorted(resolved.allowed_tools_deny),
            tool_access={
                tool_name: {
                    "mode": rule.mode,
                    "senders": sorted(rule.senders),
                }
                for tool_name, rule in sorted(resolved.tool_access_rules.items())
            },
            persona_file=resolved.persona_file,
            voice_input_wake_phrases=sorted(resolved.voice_input_wake_phrases),
            voice_output_mode=resolved.voice_output_mode,
            voice_output_tts_route=resolved.voice_output_tts_route,
            voice_output_voice=resolved.voice_output_voice,
            voice_output_format=resolved.voice_output_format,
            voice_output_max_sentences=resolved.voice_output_max_sentences,
            voice_output_max_chars=resolved.voice_output_max_chars,
        )

    @staticmethod
    def _sender_match(sender_primary: str, sender_aliases: list[str], allowed: frozenset[str]) -> bool:
        if not allowed:
            return False
        normalized = {
            normalize_identity_token(sender_primary),
            *[normalize_identity_token(a) for a in sender_aliases],
        }
        normalized.discard("")
        return any(item in allowed for item in normalized)

    def _owner_match(self, actor: ActorContext) -> bool:
        owners = self._owner_index.get(actor.channel, frozenset())
        return self._sender_match(actor.sender_primary, actor.sender_aliases, owners)

    def is_owner(self, actor: ActorContext) -> bool:
        """Public check: is the actor a configured owner for their channel?"""
        return self._owner_match(actor)

    def _evaluate_who_can_talk(self, actor: ActorContext, policy: _CompiledPolicy) -> tuple[bool, str]:
        mode = policy.who_can_talk_mode
        if mode == "everyone":
            return True, "who_can_talk:everyone"
        if mode == "allowlist":
            ok = self._sender_match(actor.sender_primary, actor.sender_aliases, policy.who_can_talk_senders)
            return ok, "who_can_talk:allowlist"
        if mode == "owner_only":
            return self._owner_match(actor), "who_can_talk:owner_only"
        return False, f"who_can_talk:unknown_mode:{mode}"

    def _evaluate_blocked_sender(self, actor: ActorContext, policy: _CompiledPolicy) -> tuple[bool, str]:
        blocked = self._sender_match(actor.sender_primary, actor.sender_aliases, policy.blocked_senders)
        return blocked, "blocked_sender"

    def _evaluate_when_to_reply(self, actor: ActorContext, policy: _CompiledPolicy) -> tuple[bool, str]:
        mode = policy.when_to_reply_mode
        if mode == "all":
            return True, "when_to_reply:all"
        if mode == "off":
            return False, "when_to_reply:off"
        if mode == "mention_only":
            if not actor.is_group:
                return True, "when_to_reply:mention_only_dm"
            if actor.mentioned_bot or actor.reply_to_bot:
                return True, "when_to_reply:mention_only_group"
            if actor.is_voice and _wake_phrase_match(actor.content, policy.voice_input_wake_phrases):
                return True, "when_to_reply:mention_only_group_voice_wake_phrase"
            return False, "when_to_reply:mention_only_group"
        if mode == "allowed_senders":
            ok = self._sender_match(actor.sender_primary, actor.sender_aliases, policy.when_to_reply_senders)
            return ok, "when_to_reply:allowed_senders"
        if mode == "owner_only":
            return self._owner_match(actor), "when_to_reply:owner_only"
        return False, f"when_to_reply:unknown_mode:{mode}"

    def _is_tool_allowed_for_actor(
        self,
        actor: ActorContext,
        tool_name: str,
        policy: _CompiledPolicy,
    ) -> bool:
        rule = policy.tool_access_rules.get(tool_name)
        if rule is None:
            return True
        if rule.mode == "everyone":
            return True
        if rule.mode == "allowlist":
            return self._sender_match(actor.sender_primary, actor.sender_aliases, rule.senders)
        if rule.mode == "owner_only":
            return self._owner_match(actor)
        return False

    def _resolve_allowed_tools(self, actor: ActorContext, policy: _CompiledPolicy, all_tools: set[str]) -> set[str]:
        if policy.allowed_tools_mode == "all":
            allowed = set(all_tools)
        else:
            allowed = set(policy.allowed_tools_tools)
        allowed -= set(policy.allowed_tools_deny)
        allowed &= all_tools
        # Guardrail: deny spawn whenever exec is denied.
        if "exec" not in allowed:
            allowed.discard("spawn")
        return {tool for tool in allowed if self._is_tool_allowed_for_actor(actor, tool, policy)}

    def evaluate(self, actor: ActorContext, all_tools: set[str]) -> PolicyDecision:
        """Evaluate policy decision for an actor."""
        if actor.channel not in self.apply_channels:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=set(all_tools),
                persona_file=None,
                reason="policy_not_applied",
            )

        policy = self.resolve_compiled_policy(actor.channel, actor.chat_id)
        is_blocked, blocked_reason = self._evaluate_blocked_sender(actor, policy)
        if is_blocked:
            return PolicyDecision(
                accept_message=False,
                should_respond=False,
                allowed_tools=set(),
                persona_file=policy.persona_file,
                reason=blocked_reason,
            )

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
            allowed_tools=self._resolve_allowed_tools(actor, policy, all_tools),
            persona_file=policy.persona_file,
            reason=f"{accept_reason}|{reply_reason}",
        )

    def persona_text(self, persona_file: str | None) -> str | None:
        """Load persona text for a decision."""
        return load_persona_text(persona_file, self.workspace)

    def resolve_memory_notes(
        self,
        *,
        channel: str,
        chat_id: str,
        is_group: bool,
    ) -> MemoryNotesDecision:
        """Resolve memory-notes settings with precedence defaults -> channel -> chat."""
        notes = self.policy.memory_notes
        source: dict[str, str] = {}
        if not notes.enabled:
            return MemoryNotesDecision(
                enabled=False,
                mode="adaptive",
                allow_blocked_senders=False,
                batch_interval_seconds=self._memory_notes_batch_interval_seconds,
                batch_max_messages=self._memory_notes_batch_max_messages,
                source={"enabled": "memoryNotes.enabled=false"},
            )
        if channel not in self._memory_notes_apply_channels:
            return MemoryNotesDecision(
                enabled=False,
                mode="adaptive",
                allow_blocked_senders=False,
                batch_interval_seconds=self._memory_notes_batch_interval_seconds,
                batch_max_messages=self._memory_notes_batch_max_messages,
                source={"enabled": f"memoryNotes.applyChannels excludes {channel}"},
            )

        enabled = (
            bool(notes.defaults.groups_enabled) if is_group else bool(notes.defaults.dms_enabled)
        )
        source["enabled"] = (
            "memoryNotes.defaults.groupsEnabled"
            if is_group
            else "memoryNotes.defaults.dmsEnabled"
        )
        mode = self._memory_notes_defaults.mode or "adaptive"
        source["mode"] = "memoryNotes.defaults.mode"
        allow_blocked = bool(self._memory_notes_defaults.allow_blocked_senders)
        source["allowBlockedSenders"] = "memoryNotes.defaults.allowBlockedSenders"

        channel_override = self._memory_notes_channel_defaults.get(channel)
        if channel_override is not None:
            if channel_override.enabled is not None:
                enabled = bool(channel_override.enabled)
                source["enabled"] = f"memoryNotes.channels.{channel}.default.enabled"
            if channel_override.mode is not None:
                mode = channel_override.mode
                source["mode"] = f"memoryNotes.channels.{channel}.default.mode"
            if channel_override.allow_blocked_senders is not None:
                allow_blocked = bool(channel_override.allow_blocked_senders)
                source["allowBlockedSenders"] = (
                    f"memoryNotes.channels.{channel}.default.allowBlockedSenders"
                )

        chat_override = self._memory_notes_chat_overrides.get((channel, chat_id))
        if chat_override is not None:
            if chat_override.enabled is not None:
                enabled = bool(chat_override.enabled)
                source["enabled"] = (
                    f"memoryNotes.channels.{channel}.chats.{chat_id}.enabled"
                )
            if chat_override.mode is not None:
                mode = chat_override.mode
                source["mode"] = f"memoryNotes.channels.{channel}.chats.{chat_id}.mode"
            if chat_override.allow_blocked_senders is not None:
                allow_blocked = bool(chat_override.allow_blocked_senders)
                source["allowBlockedSenders"] = (
                    f"memoryNotes.channels.{channel}.chats.{chat_id}.allowBlockedSenders"
                )

        return MemoryNotesDecision(
            enabled=enabled,
            mode=mode,
            allow_blocked_senders=allow_blocked,
            batch_interval_seconds=self._memory_notes_batch_interval_seconds,
            batch_max_messages=self._memory_notes_batch_max_messages,
            source=source,
        )
