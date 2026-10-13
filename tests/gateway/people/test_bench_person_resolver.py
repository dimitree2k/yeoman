from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests.gateway.people.people_fixtures import three_person_history

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "bench_person_resolver.py"


def _module():
    spec = importlib.util.spec_from_file_location("bench_person_resolver", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_reports_counts_without_names(tmp_path):
    history = three_person_history(tmp_path)
    history.close()
    report = _module().run_benchmark(history.path, repeats=1)
    assert report["queries"] >= 4
    assert sum(report["outcomes"].values()) == report["queries"]
    assert report["outcomes"]["ambiguous"] >= 1          # "Alex" is ambiguous
    assert report["outcomes"]["resolved"] >= 1
    assert report["latency_ms"]["p95"] >= 0
    assert report["scope"] == "owner_groups" and report["chats_in_scope"] == 1
    text = json.dumps(report)
    assert "Alex" not in text and "Frank" not in text     # content-free


def test_cli_prints_json(tmp_path, capsys):
    history = three_person_history(tmp_path)
    history.close()
    assert _module().main(["--history-db", str(history.path), "--repeats", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["queries"] >= 4
