from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.app.bootstrap import policy_validation_tools
from yeoman_gateway.cli.policy_commands import _policy_known_tools
from yeoman_gateway.policy.capabilities import SERVICE_POLICY_CAPABILITIES
from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig


def _policy_with_send_media() -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "defaults": {
                "allowedTools": {"mode": "allowlist", "tools": ["send_media"]},
            },
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
        }
    )


def _a2a_owner_policy() -> PolicyConfig:
    """Mirror the live owner-chat shape used for controlled A2A delivery."""
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "defaults": {"allowedTools": {"mode": "all", "deny": []}},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "owner@s.whatsapp.net": {
                            "whoCanTalk": {
                                "mode": "allowlist",
                                "senders": ["$owner", "service:a2a"],
                            },
                            "whenToReply": {"mode": "all"},
                            "allowedTools": {"mode": "all", "deny": []},
                        }
                    }
                }
            },
        }
    )


def test_service_capability_allowlist_validates_without_exposing_llm_tool(tmp_path: Path) -> None:
    responder_tools = {"message"}
    engine = PolicyEngine(
        _policy_with_send_media(), apply_channels={"whatsapp"}, workspace=tmp_path
    )

    engine.validate(policy_validation_tools(responder_tools))

    assert "send_media" not in responder_tools


def test_policy_adapter_known_tools_include_service_capabilities() -> None:
    adapter = EnginePolicyAdapter(engine=None, known_tools={"message"})

    assert SERVICE_POLICY_CAPABILITIES <= adapter.known_tools


def test_cli_and_runtime_policy_vocabularies_include_same_service_capabilities() -> None:
    assert SERVICE_POLICY_CAPABILITIES <= _policy_known_tools()
    assert SERVICE_POLICY_CAPABILITIES <= policy_validation_tools({"message"})


@pytest.mark.parametrize("capability", ["message", "send_media"])
def test_real_engine_authorizes_a2a_delivery_capabilities(
    tmp_path: Path, capability: str
) -> None:
    """A2A image/file authorization must succeed through a real PolicyEngine."""
    engine = PolicyEngine(_a2a_owner_policy(), apply_channels={"whatsapp"}, workspace=tmp_path)
    runtime_tools = policy_validation_tools({"message", "send_voice"})
    engine.validate(runtime_tools)

    decision = engine.evaluate(
        ActorContext(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            sender_primary="service:a2a",
            sender_aliases=[],
            is_group=False,
            mentioned_bot=True,
            reply_to_bot=True,
        ),
        runtime_tools,
    )

    assert decision.accept_message
    assert decision.should_respond
    assert capability in decision.allowed_tools


def test_real_engine_omits_service_capability_when_validation_excludes_it(tmp_path: Path) -> None:
    """Guard the regression: the pre-fix vocabulary cannot authorize media delivery."""
    engine = PolicyEngine(_a2a_owner_policy(), apply_channels={"whatsapp"}, workspace=tmp_path)
    llm_tools = {"message", "send_voice"}

    decision = engine.evaluate(
        ActorContext(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            sender_primary="service:a2a",
            sender_aliases=[],
            is_group=False,
            mentioned_bot=True,
            reply_to_bot=True,
        ),
        llm_tools,
    )

    assert "message" in decision.allowed_tools
    assert "send_media" not in decision.allowed_tools


def test_owner_forward_command_is_addressed_in_mention_only_group(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "defaults": {
                "allowedTools": {"mode": "allowlist", "tools": ["forward_message"]},
            },
            "channels": {
                "whatsapp": {
                    "default": {
                        "whenToReply": {"mode": "mention_only"},
                        "toolAccess": {"forward_message": {"mode": "owner_only"}},
                    }
                }
            },
        }
    )
    engine = PolicyEngine(policy, apply_channels={"whatsapp"}, workspace=tmp_path)
    actor = ActorContext(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_primary="owner@s.whatsapp.net",
        sender_aliases=[],
        is_group=True,
        mentioned_bot=False,
        reply_to_bot=False,
        content="/forward Ente",
    )

    decision = engine.evaluate(actor, {"forward_message"})

    assert decision.should_respond is True
    assert "forward_message" in decision.allowed_tools
    assert decision.reason.endswith("when_to_reply:explicit_command")
