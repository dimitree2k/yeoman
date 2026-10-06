import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from yeoman_gateway.app.bootstrap import GatewayRuntime
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


def test_start_bridge_replaces_blank_reference_dir_with_yeoman_home_default(
    tmp_path, monkeypatch
) -> None:
    runtime = tmp_path / "yeoman"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    monkeypatch.setenv("BRIDGE_MESSAGE_REFERENCE_DIR", " \t")
    whatsapp = SimpleNamespace(
        bridge_port=3001,
        bridge_url="ws://127.0.0.1:3001",
        bridge_host="127.0.0.1",
        auth_dir=tmp_path / "auth",
        read_receipts=False,
        accept_from_me=False,
        media=SimpleNamespace(
            persist_incoming_audio=False,
            persist_incoming_documents=False,
            incoming_path=tmp_path / "media/in",
            outgoing_path=tmp_path / "media/out",
        ),
    )
    manager = WhatsAppRuntimeManager(
        config=SimpleNamespace(channels=SimpleNamespace(whatsapp=whatsapp)),
        user_bridge_dir=tmp_path / "bridge",
    )
    manager.ensure_runtime = lambda: tmp_path / "bridge"
    manager.ensure_bridge_token = lambda **kwargs: "token"
    manager._systemd_bridge_active = lambda: False
    manager.status_bridge = lambda port: BridgeStatus(
        running=False, port=port, pids=[], log_path=tmp_path / "bridge.log"
    )
    monkeypatch.setattr("yeoman_gateway.channels.whatsapp_runtime.shutil.which", lambda _: "node")
    monkeypatch.setattr("yeoman_gateway.channels.whatsapp_runtime.time.sleep", lambda _: None)
    launched = {}

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        "yeoman_gateway.channels.whatsapp_runtime.subprocess.Popen",
        lambda *args, **kwargs: launched.update(kwargs) or Process(),
    )

    manager.start_bridge()

    assert launched["env"]["BRIDGE_MESSAGE_REFERENCE_DIR"] == str(
        runtime / "data/ops/bridge-message-references"
    )


def test_channels_login_replaces_blank_reference_dir_with_yeoman_home_default(
    tmp_path, monkeypatch
) -> None:
    from yeoman_gateway.cli.channel_commands import channels_login

    runtime = tmp_path / "yeoman"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    monkeypatch.setenv("BRIDGE_MESSAGE_REFERENCE_DIR", " \t")
    whatsapp = SimpleNamespace(
        bridge_port=3001,
        resolved_bridge_port=3001,
        bridge_host="127.0.0.1",
        auth_dir=tmp_path / "auth",
    )
    config = SimpleNamespace(channels=SimpleNamespace(whatsapp=whatsapp))
    monkeypatch.setattr("yeoman_shared.config.loader.load_config", lambda: config)
    monkeypatch.setattr(
        "yeoman_gateway.cli.channel_commands._ensure_whatsapp_bridge_token",
        lambda **kwargs: "token",
    )
    monkeypatch.setattr("yeoman_gateway.cli.channel_commands._get_bridge_dir", lambda: tmp_path)
    launched = {}
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: launched.update(kwargs)
    )

    channels_login()

    assert launched["env"]["BRIDGE_MESSAGE_REFERENCE_DIR"] == str(
        runtime / "data/ops/bridge-message-references"
    )


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


def test_gateway_cleanup_closes_processing_when_retention_stop_fails(monkeypatch) -> None:
    class AsyncNoop:
        tools = {}

        async def start(self):
            return None

        async def stop(self):
            return None

        async def aclose(self):
            return None

    class Lifecycle:
        async def start(self):
            return None

        def stop(self):
            return None

    class FailingRetention(AsyncNoop):
        async def stop(self):
            raise RuntimeError("retention failed")

    closed: list[str] = []

    class FailingOrchestrator(AsyncNoop):
        async def run(self):
            raise RuntimeError("runtime body failed")

        def stop(self):
            closed.append("orchestrator")

    class Channels(AsyncNoop):
        async def start_all(self):
            return None

        async def stop_all(self):
            closed.append("channels")

    class Processing:
        def close(self):
            closed.append("processing")

    class Closing:
        def close(self):
            closed.append("close")

    runtime = GatewayRuntime(
        orchestrator=FailingOrchestrator(),
        channels=Channels(),
        cron=Lifecycle(),
        heartbeat=Lifecycle(),
        inbound_archive=Closing(),
        responder=AsyncNoop(),
        memory=Closing(),
        contacts=Closing(),
        chat_registry=Closing(),
        processing=Processing(),
        retention=FailingRetention(),
    )

    monkeypatch.setattr("yeoman_gateway.app.bootstrap.tracing.init", lambda: None)
    monkeypatch.setattr("yeoman_gateway.app.bootstrap.tracing.shutdown", AsyncNoop().stop)

    with pytest.raises(RuntimeError, match="retention failed"):
        asyncio.run(runtime.run())

    assert "processing" in closed
    assert "channels" in closed
