"""The local profile benchmark is a measurement harness, not a fake acceptance test.

The ``< 200 ms p95`` and ``+256 MiB`` numbers are agreed targets *measured on the device*,
so the unit test only proves that the harness builds a correct synthetic shape and reports
a usable result.  Asserting a wall-clock budget here would be exactly the kind of faked
evidence the plan forbids.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge._snapshot import (
    SnapshotError,
    benchmark_profiles,
)


def test_benchmark_builds_and_reports_a_usable_result(tmp_path: Path) -> None:
    report = benchmark_profiles(
        target=tmp_path / "bench.db", people=50, statements=500, iterations=15
    )
    assert report.people == 50
    assert report.statements == 500
    assert report.iterations == 15
    assert len(report.samples_ms) == 15
    assert report.p50_ms <= report.p95_ms <= report.max_ms
    assert report.p95_ms > 0.0
    assert report.peak_rss_mib > 0.0
    assert report.database_bytes > 0
    # The result *carries* the budget verdict instead of asserting it.
    assert isinstance(report.within_latency_budget, bool)


def test_benchmark_is_deterministic_for_a_fixed_seed(tmp_path: Path) -> None:
    """Two runs with one seed build the same database, so numbers are comparable."""
    first = benchmark_profiles(
        target=tmp_path / "one.db", people=20, statements=200, iterations=5, seed=7
    )
    second = benchmark_profiles(
        target=tmp_path / "two.db", people=20, statements=200, iterations=5, seed=7
    )
    assert first.database_bytes == second.database_bytes


def test_benchmark_refuses_to_overwrite_and_validates_its_shape(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError) as excinfo:
        benchmark_profiles(target=tmp_path / "bad.db", people=10, statements=5)
    assert excinfo.value.code == "invalid_benchmark_shape"

    benchmark_profiles(target=tmp_path / "keep.db", people=10, statements=100, iterations=3)
    with pytest.raises(SnapshotError) as excinfo:
        benchmark_profiles(target=tmp_path / "keep.db", people=10, statements=100, iterations=3)
    assert excinfo.value.code == "target_exists"


def test_benchmark_makes_no_network_or_provider_call(tmp_path: Path, monkeypatch) -> None:
    """A local benchmark that quietly called a provider would not be a local benchmark."""
    import socket

    def _forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("the benchmark attempted a network connection")

    monkeypatch.setattr(socket, "socket", _forbidden)
    report = benchmark_profiles(
        target=tmp_path / "offline.db", people=10, statements=100, iterations=3
    )
    assert report.p95_ms > 0.0


def test_benchmark_cli_reports_the_measurement(tmp_path: Path) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    result = CliRunner().invoke(
        knowledge_app,
        [
            "benchmark",
            "--target",
            str(tmp_path / "cli.db"),
            "--people",
            "20",
            "--statements",
            "200",
            "--iterations",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "reads p50=" in result.output
    assert "peak RSS:" in result.output
    assert "p95 budget (<200 ms):" in result.output
