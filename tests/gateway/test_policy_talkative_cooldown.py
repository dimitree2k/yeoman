from __future__ import annotations

from pathlib import Path

from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig


def _policy(notice: bool | None = None) -> PolicyConfig:
    talkative: dict[str, object] = {"enabled": True}
    if notice is not None:
        talkative["notice"] = notice
    return PolicyConfig.model_validate({"defaults": {"talkativeCooldown": talkative}})


def test_talkative_notice_default_true() -> None:
    policy = _policy()

    assert policy.defaults.talkative_cooldown.notice is True


def test_talkative_notice_parsed_false(tmp_path: Path) -> None:
    policy = _policy(notice=False)

    assert policy.defaults.talkative_cooldown.notice is False
    engine = PolicyEngine(policy, apply_channels={"telegram"}, workspace=tmp_path)
    resolved = engine.resolve_policy("telegram", "group")

    assert resolved.talkative_cooldown_notice is False
