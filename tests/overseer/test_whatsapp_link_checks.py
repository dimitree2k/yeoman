"""The Overseer restarts the bridge only when it is dead, and alerts on a held link."""

from __future__ import annotations

import pytest
from yeoman_overseer.runbook.parser import parse_runbook
from yeoman_overseer.trigger import checks


def _health(monkeypatch: pytest.MonkeyPatch, whatsapp: dict | Exception) -> None:
    def fake(*, target: str, timeout_s: float) -> dict:
        if isinstance(whatsapp, Exception):
            raise whatsapp
        return {"whatsapp": whatsapp}

    monkeypatch.setattr(checks, "_whatsapp_bridge_health", fake)


def test_disconnected_but_answering_bridge_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, {"connected": False, "lastDisconnectStatus": 401})
    assert checks.check_whatsapp_bridge_reachable(target="default").value is True


def test_dead_bridge_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, ConnectionRefusedError("refused"))
    assert checks.check_whatsapp_bridge_reachable(target="default").value is False


def test_logout_hold_names_the_relink_command(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(
        monkeypatch,
        {"connected": False, "hold": {"kind": "needs_human", "code": 401, "reason": "logged_out"}},
    )
    result = checks.check_whatsapp_link_held(target="default")
    assert result.value is True
    assert "401" in result.detail and "yeoman channels login" in result.detail


def test_no_hold_when_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, {"connected": True, "hold": {"kind": "budget_exhausted"}})
    assert checks.check_whatsapp_link_held(target="default").value is False


def test_starter_runbooks_use_the_new_checks() -> None:
    from pathlib import Path

    base = Path(checks.__file__).parents[1] / "starter_runbooks"
    bridge = parse_runbook(base / "health-bridge.md")
    link = parse_runbook(base / "health-whatsapp-link.md")
    assert bridge.meta.version == 4
    assert bridge.meta.trigger.condition.check == "whatsapp_bridge_reachable"
    assert link.meta.trigger.condition.check == "whatsapp_link_held"
    assert "restart_service" not in link.body
