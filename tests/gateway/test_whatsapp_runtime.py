from types import SimpleNamespace
from unittest.mock import patch

from yeoman_gateway.channels.whatsapp_runtime import BridgeStatus, WhatsAppRuntimeManager


def test_status_removes_pid_file_for_non_bridge_process(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    config = SimpleNamespace(
        channels=SimpleNamespace(
            whatsapp=SimpleNamespace(bridge_port=3001, bridge_url="ws://127.0.0.1:3001")
        )
    )
    manager = WhatsAppRuntimeManager(config=config, user_bridge_dir=tmp_path / "bridge")
    manager.bridge_pid_path.write_text("12345")
    monkeypatch.setattr(
        "yeoman_gateway.channels.whatsapp_runtime.listener_pids_for_port", lambda port: set()
    )
    monkeypatch.setattr(
        "yeoman_gateway.channels.whatsapp_runtime.pid_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "yeoman_gateway.channels.whatsapp_runtime.is_bridge_process", lambda pid: False
    )

    status = manager.status_bridge()

    assert status.running is False
    assert not manager.bridge_pid_path.exists()


def test_start_bridge_does_not_spawn_beside_active_systemd_unit(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    config = SimpleNamespace(
        channels=SimpleNamespace(
            whatsapp=SimpleNamespace(bridge_port=3001, bridge_url="ws://127.0.0.1:3001")
        )
    )
    manager = WhatsAppRuntimeManager(config=config, user_bridge_dir=tmp_path / "bridge")
    status = BridgeStatus(
        running=True,
        port=3001,
        pids=[9876],
        log_path=runtime / "var/logs/bridge.log",
    )
    monkeypatch.setattr(manager, "_systemd_bridge_active", lambda: True)
    monkeypatch.setattr(manager, "_wait_for_systemd_bridge", lambda port: status)

    with patch("yeoman_gateway.channels.whatsapp_runtime.subprocess.Popen") as popen:
        assert manager.start_bridge() is status

    popen.assert_not_called()


def test_systemd_bridge_accepts_reachable_port_without_visible_pid(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    config = SimpleNamespace(
        channels=SimpleNamespace(
            whatsapp=SimpleNamespace(
                bridge_port=3001,
                bridge_url="ws://127.0.0.1:3001",
                bridge_startup_timeout_ms=1000,
            )
        )
    )
    manager = WhatsAppRuntimeManager(config=config, user_bridge_dir=tmp_path / "bridge")
    hidden_pid_status = BridgeStatus(
        running=False,
        port=3001,
        pids=[],
        log_path=runtime / "var/logs/bridge.log",
    )
    monkeypatch.setattr(manager, "status_bridge", lambda port: hidden_pid_status)
    monkeypatch.setattr(manager, "_bridge_port_open", lambda port: True)

    status = manager._wait_for_systemd_bridge(3001)

    assert status.running is True
    assert status.pids == []
