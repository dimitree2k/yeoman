"""`yeoman channels login` relinks through the running bridge in one command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from yeoman_gateway.cli import channel_commands


def _fake_bridge(monkeypatch: pytest.MonkeyPatch, health: dict, logins: list[dict]) -> list[str]:
    calls: list[str] = []

    def fake(command_type: str, payload: dict | None = None, **_: object) -> dict:
        calls.append(command_type)
        if command_type == "health":
            return {"whatsapp": health, "queue": {"clients": 2}}
        if command_type == "login_start":
            return {"login": logins.pop(0)}
        if command_type == "login_wait":
            return {"connected": True}
        raise AssertionError(command_type)

    monkeypatch.setattr("yeoman_gateway.cli.bridge_client.bridge_command", fake)
    monkeypatch.setattr(channel_commands, "_print_qr", lambda qr: None)
    return calls


def test_already_connected_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_bridge(monkeypatch, {"connected": True}, [])
    channel_commands.channels_login(fresh=False, timeout=5)
    assert calls == ["health"]


def test_logged_out_moves_auth_aside_and_links(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    auth = tmp_path / "whatsapp-auth"
    auth.mkdir()
    (auth / "creds.json").write_text("{}")
    config = SimpleNamespace(
        channels=SimpleNamespace(whatsapp=SimpleNamespace(resolved_auth_dir=auth))
    )
    monkeypatch.setattr("yeoman_shared.config.loader.load_config", lambda: config)
    systemctl: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda args, **kw: systemctl.append(list(args)))
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = _fake_bridge(
        monkeypatch,
        {"connected": False, "hold": {"kind": "needs_human", "code": 401}},
        [
            {
                "qr": "https://wa.me/settings/linked_devices#abc",
                "connected": False,
                "status": "qr_ready",
            }
        ],
    )

    channel_commands.channels_login(fresh=False, timeout=30)

    backups = [p for p in tmp_path.iterdir() if p.name.startswith("whatsapp-auth.loggedout-")]
    assert len(backups) == 1 and (backups[0] / "creds.json").exists()
    assert auth.is_dir() and not any(auth.iterdir())
    assert systemctl[0][-2:] == ["stop", "yeoman-bridge.service"]
    assert systemctl[1][-2:] == ["start", "yeoman-bridge.service"]
    assert calls[:3] == ["health", "login_start", "login_wait"]
    assert not channel_commands._qr_file().exists()
