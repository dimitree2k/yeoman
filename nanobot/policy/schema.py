"""Policy schema for per-channel and per-chat access control."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyModel(BaseModel):
    """Base model with strict config parsing."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


WhoCanTalkMode = Literal["everyone", "allowlist", "owner_only"]
WhenToReplyMode = Literal["all", "mention_only", "allowed_senders", "owner_only", "off"]
AllowedToolsMode = Literal["all", "allowlist"]
ToolAccessMode = Literal["everyone", "allowlist", "owner_only"]
MemoryNotesMode = Literal["adaptive", "heuristic", "hybrid"]
VoiceOutputMode = Literal["text", "in_kind", "always", "off"]


class WhoCanTalkPolicy(PolicyModel):
    """Who is allowed to send messages to the bot."""

    mode: WhoCanTalkMode = "everyone"
    senders: list[str] = Field(default_factory=list)


class WhoCanTalkPolicyOverride(PolicyModel):
    """Partial override for who-can-talk policy."""

    mode: WhoCanTalkMode | None = None
    senders: list[str] | None = None


class WhenToReplyPolicy(PolicyModel):
    """When the bot should respond after a message is accepted."""

    mode: WhenToReplyMode = "all"
    senders: list[str] = Field(default_factory=list)


class WhenToReplyPolicyOverride(PolicyModel):
    """Partial override for when-to-reply policy."""

    mode: WhenToReplyMode | None = None
    senders: list[str] | None = None


class AllowedToolsPolicy(PolicyModel):
    """Which tools the model can call in this context."""

    mode: AllowedToolsMode = "all"
    tools: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class AllowedToolsPolicyOverride(PolicyModel):
    """Partial override for allowed-tools policy."""

    mode: AllowedToolsMode | None = None
    tools: list[str] | None = None
    deny: list[str] | None = None


class BlockedSendersPolicy(PolicyModel):
    """Explicit sender deny-list evaluated before whoCanTalk."""

    senders: list[str] = Field(default_factory=list)


class BlockedSendersPolicyOverride(PolicyModel):
    """Partial override for blocked senders deny-list."""

    senders: list[str] | None = None


class ToolAccessRule(PolicyModel):
    """Per-tool sender access rule."""

    mode: ToolAccessMode = "everyone"
    senders: list[str] = Field(default_factory=list)


class ToolAccessRuleOverride(PolicyModel):
    """Partial override for per-tool sender access rule."""

    mode: ToolAccessMode | None = None
    senders: list[str] | None = None


class VoiceInputPolicy(PolicyModel):
    """Voice input settings for a chat."""

    wake_phrases: list[str] = Field(default_factory=list, alias="wakePhrases")


class VoiceInputPolicyOverride(PolicyModel):
    """Partial override for voice input settings."""

    wake_phrases: list[str] | None = Field(default=None, alias="wakePhrases")


class VoiceOutputPolicy(PolicyModel):
    """Voice output settings for a chat."""

    mode: VoiceOutputMode = "text"
    tts_route: str = Field(default="tts.speak", alias="ttsRoute")
    voice: str = "alloy"
    format: str = "opus"
    max_sentences: int = Field(default=2, alias="maxSentences", ge=1)
    max_chars: int = Field(default=150, alias="maxChars", ge=1)


class VoiceOutputPolicyOverride(PolicyModel):
    """Partial override for voice output settings."""

    mode: VoiceOutputMode | None = None
    tts_route: str | None = Field(default=None, alias="ttsRoute")
    voice: str | None = None
    format: str | None = None
    max_sentences: int | None = Field(default=None, alias="maxSentences", ge=1)
    max_chars: int | None = Field(default=None, alias="maxChars", ge=1)


class VoicePolicy(PolicyModel):
    """Voice policy settings for a chat (input + output)."""

    input: VoiceInputPolicy = Field(default_factory=VoiceInputPolicy)
    output: VoiceOutputPolicy = Field(default_factory=VoiceOutputPolicy)


class VoicePolicyOverride(PolicyModel):
    """Partial override for voice policy settings."""

    input: VoiceInputPolicyOverride | None = None
    output: VoiceOutputPolicyOverride | None = None


class ChatPolicy(PolicyModel):
    """Resolved chat policy (no optional fields)."""

    who_can_talk: WhoCanTalkPolicy = Field(default_factory=WhoCanTalkPolicy, alias="whoCanTalk")
    when_to_reply: WhenToReplyPolicy = Field(default_factory=WhenToReplyPolicy, alias="whenToReply")
    blocked_senders: BlockedSendersPolicy = Field(
        default_factory=BlockedSendersPolicy, alias="blockedSenders"
    )
    allowed_tools: AllowedToolsPolicy = Field(
        default_factory=AllowedToolsPolicy, alias="allowedTools"
    )
    tool_access: dict[str, ToolAccessRule] = Field(default_factory=dict, alias="toolAccess")
    persona_file: str | None = Field(default=None, alias="personaFile")
    voice: VoicePolicy = Field(default_factory=VoicePolicy)


class ChatPolicyOverride(PolicyModel):
    """Partial override at channel-default or specific-chat level."""

    who_can_talk: WhoCanTalkPolicyOverride | None = Field(default=None, alias="whoCanTalk")
    when_to_reply: WhenToReplyPolicyOverride | None = Field(default=None, alias="whenToReply")
    blocked_senders: BlockedSendersPolicyOverride | None = Field(
        default=None, alias="blockedSenders"
    )
    allowed_tools: AllowedToolsPolicyOverride | None = Field(default=None, alias="allowedTools")
    tool_access: dict[str, ToolAccessRuleOverride] | None = Field(default=None, alias="toolAccess")
    persona_file: str | None = Field(default=None, alias="personaFile")
    voice: VoicePolicyOverride | None = None
    comment: str | None = Field(default=None, alias="comment")


class ChannelPolicy(PolicyModel):
    """Per-channel policy section."""

    default: ChatPolicyOverride = Field(default_factory=ChatPolicyOverride)
    chats: dict[str, ChatPolicyOverride] = Field(default_factory=dict)


class RuntimePolicy(PolicyModel):
    """Runtime behavior for policy handling."""

    reload_on_change: bool = Field(default=True, alias="reloadOnChange")
    reload_check_interval_seconds: float = Field(
        default=1.0, alias="reloadCheckIntervalSeconds", ge=0.1
    )
    feature_flags: dict[str, bool] = Field(default_factory=dict, alias="featureFlags")
    admin_command_rate_limit_per_minute: int = Field(
        default=30, alias="adminCommandRateLimitPerMinute", ge=1
    )
    admin_require_confirm_for_risky: bool = Field(
        default=False, alias="adminRequireConfirmForRisky"
    )


class MemoryNotesBatchPolicy(PolicyModel):
    """Batch ingestion controls for background group notes."""

    interval_seconds: int = Field(default=1800, alias="intervalSeconds", ge=1)
    max_messages: int = Field(default=100, alias="maxMessages", ge=1)


class MemoryNotesDefaultsPolicy(PolicyModel):
    """Default behavior for background memory-notes capture."""

    groups_enabled: bool = Field(default=True, alias="groupsEnabled")
    dms_enabled: bool = Field(default=False, alias="dmsEnabled")
    mode: MemoryNotesMode = "adaptive"
    allow_blocked_senders: bool = Field(default=False, alias="allowBlockedSenders")


class MemoryNotesOverride(PolicyModel):
    """Per-channel/chat override for memory-notes capture."""

    enabled: bool | None = None
    mode: MemoryNotesMode | None = None
    allow_blocked_senders: bool | None = Field(default=None, alias="allowBlockedSenders")


class MemoryNotesChannelPolicy(PolicyModel):
    """Per-channel defaults and per-chat overrides for memory-notes capture."""

    default: MemoryNotesOverride = Field(default_factory=MemoryNotesOverride)
    chats: dict[str, MemoryNotesOverride] = Field(default_factory=dict)


def _default_memory_notes_channels() -> dict[str, MemoryNotesChannelPolicy]:
    return {
        "whatsapp": MemoryNotesChannelPolicy(),
        "telegram": MemoryNotesChannelPolicy(),
    }


class MemoryNotesPolicy(PolicyModel):
    """Top-level background memory-notes policy."""

    enabled: bool = True
    apply_channels: list[str] = Field(
        default_factory=lambda: ["whatsapp", "telegram"],
        alias="applyChannels",
    )
    batch: MemoryNotesBatchPolicy = Field(default_factory=MemoryNotesBatchPolicy)
    defaults: MemoryNotesDefaultsPolicy = Field(default_factory=MemoryNotesDefaultsPolicy)
    channels: dict[str, MemoryNotesChannelPolicy] = Field(
        default_factory=_default_memory_notes_channels
    )


def _default_owners() -> dict[str, list[str]]:
    return {
        "telegram": [],
        "whatsapp": [],
    }


def _default_policy_defaults() -> ChatPolicy:
    # Conservative baseline for remote chat channels.
    return ChatPolicy.model_validate(
        {
            "allowedTools": {
                "mode": "allowlist",
                "tools": ["list_dir", "read_file", "web_search", "web_fetch"],
                "deny": [],
            }
        }
    )


def _default_channels() -> dict[str, ChannelPolicy]:
    def mention_only_default() -> ChatPolicyOverride:
        return ChatPolicyOverride.model_validate(
            {
                "whenToReply": {"mode": "mention_only", "senders": []},
            }
        )

    return {
        "telegram": ChannelPolicy(
            default=mention_only_default(),
            chats={},
        ),
        "whatsapp": ChannelPolicy(
            default=mention_only_default(),
            chats={},
        ),
    }


FileAccessMode = Literal["read", "read-write"]


class FileAccessGrantPolicy(PolicyModel):
    """Single scoped file-access grant."""

    id: str
    path: str
    recursive: bool = True
    mode: FileAccessMode = "read"
    description: str = ""

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("grant path must not be empty")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            raise ValueError(f"grant path must be absolute (got: {value})")
        resolved = expanded.resolve()
        return resolved.as_posix()


class FileAccessPolicy(PolicyModel):
    """Scoped file-access grants for owner sessions."""

    grants: list[FileAccessGrantPolicy] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list, alias="blockedPaths")
    blocked_patterns: list[str] = Field(default_factory=list, alias="blockedPatterns")
    owner_only: bool = Field(default=True, alias="ownerOnly")
    audit: bool = True

    @field_validator("blocked_paths")
    @classmethod
    def _validate_blocked_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            raw = str(value or "").strip()
            if not raw:
                raise ValueError("blocked path must not be empty")
            expanded = Path(raw).expanduser()
            if not expanded.is_absolute():
                raise ValueError(f"blocked path must be absolute (got: {value})")
            resolved = expanded.resolve()
            normalized.append(resolved.as_posix())
        return normalized

    @model_validator(mode="after")
    def _validate_unique_grant_ids(self) -> "FileAccessPolicy":
        seen: set[str] = set()
        for grant in self.grants:
            if grant.id in seen:
                raise ValueError(f"duplicate fileAccess grant id: {grant.id}")
            seen.add(grant.id)
        return self


class PolicyConfig(PolicyModel):
    """Root policy configuration."""

    version: int = 2
    owners: dict[str, list[str]] = Field(default_factory=_default_owners)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)
    defaults: ChatPolicy = Field(default_factory=_default_policy_defaults)
    channels: dict[str, ChannelPolicy] = Field(default_factory=_default_channels)
    memory_notes: MemoryNotesPolicy = Field(default_factory=MemoryNotesPolicy, alias="memoryNotes")
    file_access: FileAccessPolicy | None = Field(default=None, alias="fileAccess")
