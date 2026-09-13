from __future__ import annotations

from pathlib import Path

from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.app.bootstrap import policy_validation_tools
from yeoman_gateway.cli.policy_commands import _policy_known_tools
from yeoman_gateway.policy.capabilities import SERVICE_POLICY_CAPABILITIES
from yeoman_gateway.policy.engine import PolicyEngine
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
