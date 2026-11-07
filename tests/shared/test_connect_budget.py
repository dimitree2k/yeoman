from pathlib import Path

from yeoman_shared.connect_budget import ConnectBudget


def _budget(path: Path, clock: dict) -> ConnectBudget:
    return ConnectBudget(path, clock=lambda: clock["t"])


def test_hourly_limit_waits_for_oldest_attempt(tmp_path: Path) -> None:
    clock = {"t": 10_000.0}
    b = _budget(tmp_path / "b.json", clock)
    for _ in range(5):
        assert b.decide().action == "connect"
        b.record_attempt()
        clock["t"] += 1
    d = b.decide()
    assert d.action == "wait" and d.why == "hourly_limit" and d.wait_s > 3500


def test_daily_limit_then_one_probe_per_8h(tmp_path: Path) -> None:
    clock = {"t": 1_000_000.0}
    b = _budget(tmp_path / "b.json", clock)
    for _ in range(15):
        clock["t"] += 3600
        b.record_attempt()
    d = b.decide()
    assert d.action == "wait" and d.why == "budget_exhausted" and d.wait_s == 8 * 3600
    clock["t"] += 8 * 3600
    d = b.decide()
    assert d.action == "connect" and d.probe
    b.record_attempt(probe=True)
    assert b.decide().wait_s == 8 * 3600


def test_restart_does_not_refill_and_reset_does(tmp_path: Path) -> None:
    clock = {"t": 5_000.0}
    path = tmp_path / "b.json"
    b = _budget(path, clock)
    for _ in range(5):
        b.record_attempt()
    assert _budget(path, clock).decide().action == "wait"
    b.reset()
    assert _budget(path, clock).decide().action == "connect"


def test_corrupt_file_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{oops")
    assert ConnectBudget(path).decide().action == "connect"
