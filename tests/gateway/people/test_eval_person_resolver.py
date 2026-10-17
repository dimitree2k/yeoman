"""CLI reports and failures contain only aggregate evidence and fixed error codes."""

import itertools
import json

import pytest


def arguments(p):
    return ["--people-db", str(p.people), "--history-db", str(p.history), "--gold", str(p.gold)]


def test_eval_report_and_errors_are_content_free(gold_directory, eval_module, capsys, monkeypatch):
    p = gold_directory
    ticks = itertools.count(step=1000)
    monkeypatch.setattr(eval_module.time, "perf_counter_ns", lambda: next(ticks))
    assert eval_module.main(arguments(p)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    assert set(report) == {
        "queries",
        "resolved",
        "ambiguous",
        "not_found",
        "expected_outcomes",
        "wrong_person",
        "mapping_unavailable",
        "people_count",
        "latency_ms",
        "cold_load_ms",
    }
    assert report["latency_ms"]["p95"] >= 0
    assert "Vincent" not in captured.out and "4915000000012" not in captured.out
    assert str(p.people) not in captured.out


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "bool_version",
        "empty",
        "bad_id",
        "duplicate_ids",
        "wrong_status",
        "bad_time",
        "extra_reader",
        "bad_channel",
        "owner_spoof",
        "bad_country",
    ],
)
def test_invalid_gold_is_fixed_json(gold_directory, eval_module, capsys, mutation):
    p = gold_directory
    raw = json.loads(p.gold.read_text())
    case = raw["cases"][0]
    if mutation == "unknown":
        raw["PRIVATE_path"] = "PRIVATE_secret"
    elif mutation == "bool_version":
        raw["version"] = True
    elif mutation == "empty":
        raw["cases"] = []
    elif mutation == "bad_id":
        case["expected_ids"] = [True]
    elif mutation == "duplicate_ids":
        case["expected_status"], case["expected_ids"] = "ambiguous", [1, 1]
    elif mutation == "wrong_status":
        case["expected_status"] = "not_found"
    elif mutation == "bad_time":
        case["reader"]["at_ms"] = 0
    elif mutation == "extra_reader":
        case["reader"]["PRIVATE_key"] = "PRIVATE_value"
    elif mutation == "bad_channel":
        case["reader"]["channel"] = "PRIVATE_channel"
    elif mutation == "owner_spoof":
        case["reader"]["chat_id"] = "g1@g.us"
    elif mutation == "bad_country":
        case["reader"]["national_calling_code"] = "1"
    p.gold.write_text(json.dumps(raw))
    assert eval_module.main(arguments(p)) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"error_code": "invalid_input", "queries": 0}


@pytest.mark.parametrize("field", ["people", "history", "gold"])
def test_missing_files_are_fixed_json(gold_directory, eval_module, capsys, field):
    args = arguments(gold_directory)
    flag = {"people": "--people-db", "history": "--history-db", "gold": "--gold"}[field]
    args[args.index(flag) + 1] += "-PRIVATE-missing"
    assert eval_module.main(args) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"error_code": "eval_unavailable", "queries": 0}


@pytest.mark.parametrize("args", [[], ["--help"], ["--PRIVATE", "PRIVATE_value"]])
def test_argument_errors_never_print_usage(eval_module, capsys, args):
    assert eval_module.main(args) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"error_code": "invalid_input", "queries": 0}


@pytest.mark.parametrize(
    "status,ids,correct,wrong",
    [
        ("resolved", (1,), False, True),
        ("ambiguous", (3,), False, False),
        ("not_found", (), False, False),
        ("ambiguous", (3, 4), True, False),
        ("ambiguous", (3, 4, 9), False, True),
    ],
)
def test_scoring_compares_entire_candidate_set(eval_module, status, ids, correct, wrong):
    from yeoman_gateway.people.resolver import Candidate, Resolution

    case = eval_module.GoldCase("Synthetic", {}, "ambiguous", (3, 4))
    result = Resolution(
        status,
        ids[0] if status == "resolved" else None,
        tuple(Candidate(p) for p in ids) if status == "ambiguous" else (),
    )
    assert eval_module.score(case, result) == (correct, wrong)


@pytest.mark.parametrize(
    "updates",
    [
        {"expected_outcomes": 94},
        {"wrong_person": 1},
        {"latency_ms": {"p95": 20.00001}},
    ],
)
def test_cli_threshold_failure_exits_one(eval_module, monkeypatch, capsys, updates):
    report = {
        "queries": 100,
        "expected_outcomes": 100,
        "wrong_person": 0,
        "latency_ms": {"p95": 20},
    }
    report.update(updates)
    monkeypatch.setattr(eval_module, "evaluate", lambda *args: report)
    assert eval_module.main(["--people-db", "p", "--history-db", "h", "--gold", "g"]) == 1
    assert json.loads(capsys.readouterr().out) == report


def test_percentile_is_nearest_rank(eval_module):
    assert eval_module.percentile(list(range(1, 21)), 0.95) == 19


def test_unexpected_candidate_for_not_found_is_wrong_person(eval_module):
    from yeoman_gateway.people.resolver import Candidate, Resolution

    case = eval_module.GoldCase("Synthetic", {}, "not_found", ())
    assert eval_module.score(
        case, Resolution("ambiguous", candidates=(Candidate(1), Candidate(2)))
    ) == (False, True)


def test_malformed_json_and_runtime_errors_never_echo_input(
    gold_directory,
    eval_module,
    capsys,
    monkeypatch,
):
    p = gold_directory
    p.gold.write_text('{"PRIVATE_secret":')
    assert eval_module.main(arguments(p)) == 2
    assert json.loads(capsys.readouterr().out) == {"error_code": "invalid_input", "queries": 0}

    def unavailable(*args):
        raise RuntimeError("PRIVATE_exception with /PRIVATE/path")

    monkeypatch.setattr(eval_module, "evaluate", unavailable)
    assert eval_module.main(arguments(p)) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"error_code": "eval_unavailable", "queries": 0}


def test_actual_cli_argument_failure_is_json_only(eval_module):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, eval_module.__file__, "--PRIVATE_argument"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout) == {"error_code": "invalid_input", "queries": 0}


def test_exact_accuracy_boundary_passes(eval_module, monkeypatch, capsys):
    report = {"queries": 100, "expected_outcomes": 95, "wrong_person": 0, "latency_ms": {"p95": 20}}
    monkeypatch.setattr(eval_module, "evaluate", lambda *args: report)
    assert eval_module.main(["--people-db", "p", "--history-db", "h", "--gold", "g"]) == 0
    assert json.loads(capsys.readouterr().out) == report
