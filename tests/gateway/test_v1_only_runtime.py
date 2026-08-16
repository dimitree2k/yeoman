from dataclasses import fields
from importlib.util import find_spec
from pathlib import Path

from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.core.intents import PersistSessionIntent
from yeoman_gateway.core.models import PolicyDecision
from yeoman_gateway.policy.schema import ChatPolicy, ChatPolicyOverride
from yeoman_shared.config.defaults import DEFAULT_MODEL_PROFILES, DEFAULT_MODEL_ROUTES


def test_turn_engine_package_is_absent() -> None:
    assert find_spec("yeoman_gateway.turn") is None


def test_responder_has_no_legacy_or_turn_engine_dependency() -> None:
    source = Path(__import__(
        "yeoman_gateway.adapters.responder_llm",
        fromlist=["__file__"],
    ).__file__).read_text()
    assert find_spec("yeoman_gateway.adapters.responder_legacy") is None
    assert "LegacyLLMResponder" not in source
    assert "yeoman_gateway.turn" not in source
    assert "_generate_v2" not in source
    assert "_v2_unavailable" not in source
    assert LLMResponder.__bases__[0].__name__ != "LLMResponder"


def test_v1_persistence_intent_has_no_v2_delivery_fields() -> None:
    names = {field.name for field in fields(PersistSessionIntent)}
    assert names == {"session_key", "user_content", "assistant_content"}


def test_bootstrap_and_pipeline_have_no_v2_generation_wiring() -> None:
    root = Path(__file__).parents[2] / "packages/gateway/yeoman_gateway"
    text = "\n".join(
        path.read_text()
        for path in (
            root / "app/bootstrap.py",
            root / "core/orchestrator.py",
            root / "pipeline/implicit_address.py",
            root / "pipeline/outbound.py",
        )
    )
    for token in (
        "_configure_turn_engine",
        "turn_generation_callback",
        "persist_v2_intent",
        "turn_engine_v2",
        "_v2_turn_outcome",
    ):
        assert token not in text


def test_policy_and_defaults_have_no_turn_engine_surface() -> None:
    assert "turn_engine" not in PolicyDecision.__dataclass_fields__
    assert "authorized_tools" not in PolicyDecision.__dataclass_fields__
    assert "turn_engine" not in ChatPolicy.model_fields
    assert "turn_engine" not in ChatPolicyOverride.model_fields
    assert "turn_planner" not in DEFAULT_MODEL_PROFILES
    assert "turn.plan" not in DEFAULT_MODEL_ROUTES


def test_production_and_defaults_have_no_turn_engine_tokens() -> None:
    repo = Path(__file__).parents[2]
    gateway_root = repo / "packages/gateway/yeoman_gateway"
    source_paths = [
        *gateway_root.rglob("*.py"),
        repo / "packages/shared/yeoman_shared/config/defaults.py",
    ]
    forbidden = (
        "yeoman_gateway.turn",
        "turnEngine",
        "turn_engine",
        "turn.plan",
        "turn_planner",
        "_turn_generation",
        "turn_engine_v2",
        "persist_v2",
        "turn_v2",
        "SAFE_UNAVAILABLE_TEXT",
        "per V2 turn",
    )
    matches = {
        token: [
            path.relative_to(repo)
            for path in source_paths
            if token in path.read_text()
        ]
        for token in forbidden
    }
    assert matches == {token: [] for token in forbidden}
