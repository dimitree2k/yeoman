from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.adapters import policy_engine as policy_engine_module
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.core.admin_commands import AdminCommandContext
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig


def _policy(*, when_to_reply: str = "all") -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "defaults": {
                "allowedTools": {"mode": "allowlist", "tools": ["message"]},
            },
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": "everyone"},
                        "whenToReply": {"mode": when_to_reply},
                    }
                }
            }
        }
    )


def _event(*, mentioned_bot: bool = False) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="chat@g.us",
        sender_id="sender@s.whatsapp.net",
        content="hello",
        is_group=True,
        mentioned_bot=mentioned_bot,
    )


def _adapter(tmp_path: Path, *, interval: float = 60.0) -> tuple[EnginePolicyAdapter, Path, PolicyConfig]:
    path = tmp_path / "policy.json"
    policy = _policy()
    save_policy(policy, path)
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools={"message"},
        policy_path=path,
        reload_on_change=True,
        reload_check_interval_seconds=interval,
        workspace=tmp_path,
    )
    return adapter, path, policy


def _replace_json(path: Path, data: dict[str, Any]) -> None:
    previous = path.stat()
    path.write_text(json.dumps(data), encoding="utf-8")
    mtime_ns = max(time.time_ns(), previous.st_mtime_ns + 1)
    os.utime(path, ns=(previous.st_atime_ns, mtime_ns))


def _force_reload_check(adapter: EnginePolicyAdapter) -> None:
    adapter._last_reload_check = 0.0


def test_invalid_changed_policy_returns_closed_decision(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)

    decision = adapter.evaluate(_event())

    assert decision.accept_message is False
    assert decision.should_respond is False
    assert decision.allowed_tools == frozenset()
    assert decision.reason == "policy_reload_failed"
    assert decision.when_to_reply_mode == "off"


def test_known_reload_error_stays_closed_before_next_check_interval(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path, interval=60.0)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)
    first = adapter.evaluate(_event())

    assert first.accept_message is False
    adapter._last_reload_check = time.monotonic()

    second = adapter.evaluate(_event())

    assert second.accept_message is False
    assert second.should_respond is False
    assert second.allowed_tools == frozenset()


def test_same_invalid_policy_version_logs_one_reload_failure(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path, interval=0.0)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        for _ in range(3):
            _force_reload_check(adapter)
            decision = adapter.evaluate(_event())
            assert decision.accept_message is False
    finally:
        logger.remove(handler_id)

    failures = [message for message in messages if "policy reload failed" in message]
    assert len(failures) == 1


def test_valid_policy_replaces_invalid_version_and_recovers(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)
    assert adapter.evaluate(_event()).accept_message is False

    recovered = _policy(when_to_reply="mention_only")
    save_policy(recovered, path)
    _force_reload_check(adapter)

    decision = adapter.evaluate(_event(mentioned_bot=True))

    assert decision.accept_message is True
    assert decision.should_respond is True
    assert decision.reason.endswith("when_to_reply:mention_only_group")
    assert adapter._policy_reload_error is None


def test_deleted_policy_closes_then_recreated_policy_recovers(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    path.unlink()
    _force_reload_check(adapter)

    closed = adapter.evaluate(_event())

    assert closed.accept_message is False
    assert closed.should_respond is False

    save_policy(policy, path)
    _force_reload_check(adapter)
    recovered = adapter.evaluate(_event())

    assert recovered.accept_message is True
    assert recovered.should_respond is True
    assert adapter._policy_reload_error is None


def test_transient_read_error_retries_same_file_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, path, policy = _adapter(tmp_path, interval=60.0)
    original_load_policy = policy_engine_module.load_policy
    calls = 0

    def flaky_load_policy(load_path: Path) -> PolicyConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary read failure")
        return original_load_policy(load_path)

    adapter._last_mtime_ns -= 1
    monkeypatch.setattr(policy_engine_module, "load_policy", flaky_load_policy)
    _force_reload_check(adapter)

    first = adapter.evaluate(_event())
    assert first.accept_message is False

    _force_reload_check(adapter)
    second = adapter.evaluate(_event())

    assert second.accept_message is True
    assert second.should_respond is True
    assert calls == 2
    assert adapter._policy_reload_error is None


def test_admin_apply_clears_reload_error_state(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)
    assert adapter.evaluate(_event()).accept_message is False

    save_policy(policy, path)
    adapter._on_policy_applied(policy)

    decision = adapter.evaluate(_event())

    assert decision.accept_message is True
    assert decision.should_respond is True
    assert adapter._policy_reload_error is None


def test_admin_command_recovers_from_reload_error(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)
    assert adapter.evaluate(_event()).accept_message is False

    result = adapter.policy_admin_handle(
        AdminCommandContext(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            sender_id="owner@s.whatsapp.net",
            participant=None,
            is_group=False,
            raw_text="/policy allow-group recovered@g.us",
        ),
        ["allow-group", "recovered@g.us"],
    )

    assert result.outcome == "applied"
    assert adapter._policy_reload_error is None
    assert path.exists()
    assert adapter.evaluate(_event()).accept_message is True


def test_first_admin_command_can_recover_from_invalid_policy(tmp_path: Path) -> None:
    adapter, path, policy = _adapter(tmp_path)
    invalid = policy.model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)

    result = adapter.policy_admin_handle(
        AdminCommandContext(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            sender_id="owner@s.whatsapp.net",
            participant=None,
            is_group=False,
            raw_text="/policy allow-group recovered@g.us",
        ),
        ["allow-group", "recovered@g.us"],
    )

    assert result.outcome == "applied"
    assert adapter._policy_reload_error is None
    assert adapter.evaluate(_event()).accept_message is True
