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
    allowed_tools: AllowedToolsPolicy = Field(default_factory=AllowedToolsPolicy, alias="allowedTools")
    tool_access: dict[str, ToolAccessRule] = Field(default_factory=dict, alias="toolAccess")
    persona_file: str | None = Field(default=None, alias="personaFile")


class ChatPolicyOverride(PolicyModel):
    """Partial override at channel-default or specific-chat level."""

    who_can_talk: WhoCanTalkPolicyOverride | None = Field(default=None, alias="whoCanTalk")
    when_to_reply: WhenToReplyPolicyOverride | None = Field(default=None, alias="whenToReply")
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
                "mode": "all",
                "deny": ["exec", "spawn"],
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
