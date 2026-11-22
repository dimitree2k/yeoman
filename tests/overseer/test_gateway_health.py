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
    monkeypatch.setattr(checks, "GATEWAY_PING_MISSES_BEFORE_UNHEALTHY", 1)
    monkeypatch.setattr(checks, "_GATEWAY_PING_MISSES", {})
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


def test_gateway_health_needs_three_missed_pings_in_a_row(monkeypatch):
    monkeypatch.setattr(checks, "_GATEWAY_PING_MISSES", {})
    monkeypatch.setattr(checks, "check_systemd_active", lambda **_: checks.CheckResult(True))
    pings = iter([False, False, True, False, False, False, False])
    monkeypatch.setattr(
        checks, "_ping_gateway", lambda: checks.CheckResult(next(pings), detail="Gateway ping failed: timed out")
    )

    results = [checks.check_gateway_healthy(target="yeoman-gateway.service") for _ in range(7)]

    # A pong resets the count; only the third miss in a row is unhealthy, then counting restarts.
    assert [r.value for r in results] == [True, True, True, True, True, False, True]
    assert results[0].detail.startswith("Gateway ping missed 1/3")
    assert "3 checks in a row" in results[5].detail


def test_gateway_health_inactive_unit_is_unhealthy_at_once(monkeypatch):
    monkeypatch.setattr(checks, "_GATEWAY_PING_MISSES", {"yeoman-gateway.service": 2})
    monkeypatch.setattr(checks, "check_systemd_active", lambda **_: checks.CheckResult(False, detail="inactive"))
    monkeypatch.setattr(checks, "_ping_gateway", lambda: pytest.fail("no ping for an inactive unit"))

    assert checks.check_gateway_healthy(target="yeoman-gateway.service").value is False
    assert checks._GATEWAY_PING_MISSES == {}


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
