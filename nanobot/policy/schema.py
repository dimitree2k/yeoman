"""Policy schema for per-channel and per-chat access control."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PolicyModel(BaseModel):
    """Base model with strict config parsing."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


WhoCanTalkMode = Literal["everyone", "allowlist", "owner_only"]
WhenToReplyMode = Literal["all", "mention_only", "allowed_senders", "owner_only", "off"]
AllowedToolsMode = Literal["all", "allowlist"]
ToolAccessMode = Literal["everyone", "allowlist", "owner_only"]
MemoryNotesMode = Literal["adaptive", "heuristic", "hybrid"]


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


class ChatPolicy(PolicyModel):
    """Resolved chat policy (no optional fields)."""

    who_can_talk: WhoCanTalkPolicy = Field(default_factory=WhoCanTalkPolicy, alias="whoCanTalk")
    when_to_reply: WhenToReplyPolicy = Field(default_factory=WhenToReplyPolicy, alias="whenToReply")
    blocked_senders: BlockedSendersPolicy = Field(default_factory=BlockedSendersPolicy, alias="blockedSenders")
    allowed_tools: AllowedToolsPolicy = Field(default_factory=AllowedToolsPolicy, alias="allowedTools")
    tool_access: dict[str, ToolAccessRule] = Field(default_factory=dict, alias="toolAccess")
    persona_file: str | None = Field(default=None, alias="personaFile")


class ChatPolicyOverride(PolicyModel):
    """Partial override at channel-default or specific-chat level."""

    who_can_talk: WhoCanTalkPolicyOverride | None = Field(default=None, alias="whoCanTalk")
    when_to_reply: WhenToReplyPolicyOverride | None = Field(default=None, alias="whenToReply")
    blocked_senders: BlockedSendersPolicyOverride | None = Field(default=None, alias="blockedSenders")
    allowed_tools: AllowedToolsPolicyOverride | None = Field(default=None, alias="allowedTools")
    tool_access: dict[str, ToolAccessRuleOverride] | None = Field(default=None, alias="toolAccess")
    persona_file: str | None = Field(default=None, alias="personaFile")
    comment: str | None = Field(default=None, alias="comment")


class ChannelPolicy(PolicyModel):
    """Per-channel policy section."""

    default: ChatPolicyOverride = Field(default_factory=ChatPolicyOverride)
    chats: dict[str, ChatPolicyOverride] = Field(default_factory=dict)


class RuntimePolicy(PolicyModel):
    """Runtime behavior for policy handling."""

    reload_on_change: bool = Field(default=True, alias="reloadOnChange")
    reload_check_interval_seconds: float = Field(default=1.0, alias="reloadCheckIntervalSeconds", ge=0.1)
    feature_flags: dict[str, bool] = Field(default_factory=dict, alias="featureFlags")
    admin_command_rate_limit_per_minute: int = Field(default=30, alias="adminCommandRateLimitPerMinute", ge=1)
    admin_require_confirm_for_risky: bool = Field(default=False, alias="adminRequireConfirmForRisky")


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
    channels: dict[str, MemoryNotesChannelPolicy] = Field(default_factory=_default_memory_notes_channels)


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


class PolicyConfig(PolicyModel):
    """Root policy configuration."""

    version: int = 2
    owners: dict[str, list[str]] = Field(default_factory=_default_owners)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)
    defaults: ChatPolicy = Field(default_factory=_default_policy_defaults)
    channels: dict[str, ChannelPolicy] = Field(default_factory=_default_channels)
    memory_notes: MemoryNotesPolicy = Field(default_factory=MemoryNotesPolicy, alias="memoryNotes")
