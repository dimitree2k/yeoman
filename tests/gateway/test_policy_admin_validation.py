from __future__ import annotations

from pathlib import Path
from typing import Any

from yeoman_gateway.policy.admin import service as policy_admin_module
from yeoman_gateway.policy.admin.contracts import (
    PolicyActorContext,
    PolicyExecutionOptions,
)
from yeoman_gateway.policy.admin.service import PolicyAdminService
from yeoman_gateway.policy.loader import load_policy, save_policy
from yeoman_gateway.policy.schema import PolicyConfig


def _policy() -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "defaults": {
                "allowedTools": {"mode": "allowlist", "tools": ["message"]},
            },
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": "everyone"},
                        "whenToReply": {"mode": "all"},
                    }
                }
            },
        }
    )


def _actor() -> PolicyActorContext:
    return PolicyActorContext(
        source="cli",
        channel="cli",
        chat_id="local",
        sender_id="tester",
        is_group=False,
        is_owner=True,
    )


def _service(path: Path, *, on_policy_applied: Any = None) -> PolicyAdminService:
    return PolicyAdminService(
        policy_path=path,
        workspace=path.parent,
        known_tools={"message"},
        apply_channels={"whatsapp"},
        on_policy_applied=on_policy_applied,
    )


def test_dry_run_rejects_unknown_tool_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    before = _policy()
    save_policy(before, path)
    after = PolicyConfig.model_validate(
        {
            **before.model_dump(by_alias=True, exclude_none=True),
            "defaults": {
                **before.defaults.model_dump(by_alias=True, exclude_none=True),
                "allowedTools": {"mode": "allowlist", "tools": ["unknown_tool"]},
            },
        }
    )
    callback_calls: list[PolicyConfig] = []
    service = _service(path, on_policy_applied=callback_calls.append)
    before_bytes = path.read_bytes()

    result = service._commit_policy(
        before=before,
        after=after,
        actor=_actor(),
        command_name="test",
        command_raw="/policy test",
        dry_run=True,
    )

    assert result.outcome == "error"
    assert result.mutated is False
    assert result.dry_run is True
    assert path.read_bytes() == before_bytes
    assert not (tmp_path / "policy" / "audit").exists()
    assert callback_calls == []


def test_callback_failure_reports_new_policy_bytes(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    save_policy(_policy(), path)

    def fail_after_save(policy: PolicyConfig) -> None:
        del policy
        raise RuntimeError("active engine install failed")

    service = _service(path, on_policy_applied=fail_after_save)
    result = service.execute_from_text(
        "/policy allow-group new-chat@g.us",
        actor=_actor(),
        options=PolicyExecutionOptions(),
    )

    assert result.outcome == "error"
    assert result.mutated is True
    assert result.before_hash is not None
    assert result.after_hash is not None
    assert result.backup_ref is not None
    assert result.audit_id is not None
    assert result.meta["disk_state"] == "new"
    assert "new policy" in result.message
    assert "Policy updated successfully" not in result.message


def test_save_failure_after_replace_reports_new_policy_bytes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    path = tmp_path / "policy.json"
    save_policy(_policy(), path)
    original_save_policy = policy_admin_module.save_policy

    def save_then_fail(policy: PolicyConfig, save_path: Path) -> None:
        original_save_policy(policy, save_path)
        raise OSError("post-replace fs acknowledgement failed")

    monkeypatch.setattr(policy_admin_module, "save_policy", save_then_fail)
    service = _service(path)

    result = service.execute_from_text(
        "/policy allow-group new-chat@g.us",
        actor=_actor(),
        options=PolicyExecutionOptions(),
    )

    assert result.outcome == "error"
    assert result.mutated is True
    assert result.meta["disk_state"] == "new"
    assert "new policy" in result.message
    assert load_policy(path).channels["whatsapp"].chats["new-chat@g.us"].who_can_talk is not None


def test_save_failure_before_replace_reports_previous_policy_bytes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    path = tmp_path / "policy.json"
    before = _policy()
    save_policy(before, path)

    def fail_before_save(policy: PolicyConfig, save_path: Path) -> None:
        del policy, save_path
        raise OSError("write refused before replace")

    monkeypatch.setattr(policy_admin_module, "save_policy", fail_before_save)
    service = _service(path)

    result = service.execute_from_text(
        "/policy allow-group new-chat@g.us",
        actor=_actor(),
        options=PolicyExecutionOptions(),
    )

    assert result.outcome == "error"
    assert result.mutated is False
    assert result.meta["disk_state"] == "before"
    assert "policy file remains unchanged" in result.message
    assert "new-chat@g.us" not in path.read_text(encoding="utf-8")
