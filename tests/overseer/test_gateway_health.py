import json
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from yeoman_overseer.executor.deterministic import parse_deterministic_actions
from yeoman_overseer.runbook.parser import parse_runbook
from yeoman_overseer.trigger import checks


@pytest.mark.parametrize("active,reply,healthy", [
    (False, None, False),
    (True, None, False),
    (True, b'{"status":"ok","response":"pong"}\n', True),
    (True, b'{"status":"error"}\n', False),
    (True, b'[]\n', False),
    (True, b'broken\n', False),
    (True, b'', False),
])
def test_gateway_health_checks_service_and_ping(tmp_path, monkeypatch, active, reply, healthy):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setattr(checks, "check_systemd_active", lambda **_: checks.CheckResult(active))
    run = tmp_path / "run"
    run.mkdir()

    def serve(listener):
        conn, _ = listener.accept()
        with conn:
            assert json.loads(conn.recv(4096))["cmd"] == "ping"
            conn.sendall(reply)

    if reply is None:
        result = checks.run_check("gateway_healthy", target="yeoman-gateway.service")
    else:
        with socket.socket(socket.AF_UNIX) as listener, ThreadPoolExecutor(1) as pool:
            listener.bind(str(run / "gateway.sock"))
            listener.listen(1)
            listener.settimeout(2)
            future = pool.submit(serve, listener)
            result = checks.run_check("gateway_healthy", target="yeoman-gateway.service")
            future.result(timeout=3)
    assert result.value is healthy


def test_gateway_runbook_restarts_on_unhealthy_result():
    root = Path(__file__).resolve().parents[2]
    rb = parse_runbook(root / "packages/overseer/yeoman_overseer/starter_runbooks/health-gateway.md")
    condition = rb.meta.trigger.condition
    assert condition.check == "gateway_healthy"
    assert condition.operator == "==" and condition.value is False
    actions = parse_deterministic_actions(rb.body)
    assert [(a.action, a.target) for a in actions] == [
        ("restart_service", "yeoman-gateway.service"),
    ]
