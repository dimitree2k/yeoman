"""Read-only evaluation of explicit isolated paths; stdout is aggregate JSON only."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal


class InvalidInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_input")


class EvaluationUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("eval_unavailable")


@dataclass(frozen=True, slots=True)
class GoldCase:
    query: str
    reader: dict[str, object]
    expected_status: Literal["resolved", "ambiguous", "not_found"]
    expected_ids: tuple[int, ...]


def _cases(path: Path) -> tuple[GoldCase, ...]:
    try:
        raw = json.loads(path.read_text())
    except ValueError, UnicodeError:
        raise InvalidInputError() from None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "cases"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or not isinstance(raw["cases"], list)
        or not 20 <= len(raw["cases"]) <= 30
    ):
        raise InvalidInputError()
    cases = []
    required = {"channel", "chat_id", "principal_id", "kind", "at_ms"}
    for row in raw["cases"]:
        if not isinstance(row, dict) or set(row) != {
            "query",
            "reader",
            "expected_status",
            "expected_ids",
        }:
            raise InvalidInputError()
        query, reader, status, ids = (
            row[k] for k in ("query", "reader", "expected_status", "expected_ids")
        )
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(reader, dict)
            or not required <= set(reader)
            or set(reader) - required - {"national_calling_code"}
        ):
            raise InvalidInputError()
        if (
            any(
                not isinstance(reader[k], str) or not reader[k].strip()
                for k in ("channel", "chat_id", "principal_id", "kind")
            )
            or reader["channel"] not in ("whatsapp", "telegram")
            or reader["kind"] not in ("owner_dm", "group", "other_dm")
            or type(reader["at_ms"]) is not int
            or reader["at_ms"] <= 0
        ):
            raise InvalidInputError()
        code = reader.get("national_calling_code")
        if code is not None and (
            not isinstance(code, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", code)
        ):
            raise InvalidInputError()
        if (
            status not in ("resolved", "ambiguous", "not_found")
            or not isinstance(ids, list)
            or any(type(p) is not int or p <= 0 for p in ids)
            or len(set(ids)) != len(ids)
            or (status == "resolved" and len(ids) != 1)
            or (status == "ambiguous" and len(ids) < 2)
            or (status == "not_found" and ids)
        ):
            raise InvalidInputError()
        cases.append(GoldCase(query, dict(reader), status, tuple(ids)))
    return tuple(cases)


def percentile(samples: list[float], fraction: float) -> float:
    return sorted(samples)[math.ceil(fraction * len(samples)) - 1]


def score(case: GoldCase, result: Any) -> tuple[bool, bool]:
    actual = (
        {result.person_id}
        if result.person_id is not None
        else {candidate.person_id for candidate in result.candidates}
    )
    expected = set(case.expected_ids)
    return result.status == case.expected_status and actual == expected, bool(actual - expected)


def evaluate(people_db: Path, history_db: Path, gold: Path) -> dict[str, Any]:
    from yeoman_gateway.history.reader import HistorySnapshot
    from yeoman_gateway.people.disclosure import reader_context
    from yeoman_gateway.people.resolver import PeopleResolver, resolve_person

    directory = PeopleResolver(Path(people_db))
    connection = None
    snapshot = None
    try:
        cases = _cases(Path(gold))
        connection = sqlite3.connect(
            Path(history_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=0
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT state_json FROM projector_state WHERE file='@runtime'"
        ).fetchone()
        state = json.loads(row[0]) if row is not None else None
        if (
            not isinstance(state, dict)
            or type(state.get("generation")) is not int
            or state["generation"] <= 0
        ):
            raise EvaluationUnavailableError()
        snapshot = HistorySnapshot(state["generation"], (), connection)
        start = time.perf_counter_ns()
        index = directory._index()
        cold = (time.perf_counter_ns() - start) / 1e6
        readers = []
        for case in cases:
            raw = case.reader
            if not set(case.expected_ids) <= index.terminal_ids:
                raise InvalidInputError()
            reader = reader_context(
                directory,
                channel=raw["channel"],
                chat_id=raw["chat_id"],
                principal_id=raw["principal_id"],
                is_owner=raw["kind"] == "owner_dm",
                at_ms=raw["at_ms"],
                history=snapshot,
            )
            if reader.kind != raw["kind"]:
                raise InvalidInputError()
            principal = directory._endpoint(reader.principal_id, reader, index)
            if len(principal) != 1:
                raise InvalidInputError()
            if reader.kind == "group":
                if not principal <= directory.scope(reader).member_ids:
                    raise InvalidInputError()
            elif directory._endpoint(reader.chat_id, reader, index) != principal:
                raise InvalidInputError()
            if "national_calling_code" in raw:
                code = raw["national_calling_code"]
                if code is not None and code != reader.national_calling_code:
                    raise InvalidInputError()
                reader = replace(reader, national_calling_code=code)
            readers.append(reader)
            resolve_person(case.query, reader)
        report = dict.fromkeys(
            ("resolved", "ambiguous", "not_found", "expected_outcomes", "wrong_person"), 0
        )
        samples = []
        for repeat in range(20):
            for case, reader in zip(cases, readers, strict=True):
                start = time.perf_counter_ns()
                result = resolve_person(case.query, reader)
                samples.append((time.perf_counter_ns() - start) / 1e6)
                if repeat == 0:
                    correct, wrong = score(case, result)
                    report[result.status] += 1
                    report["expected_outcomes"] += int(correct)
                    report["wrong_person"] += int(wrong)
        return {
            "queries": len(cases),
            **report,
            "mapping_unavailable": getattr(directory._local, "mapping_unavailable", 0),
            "people_count": len(index.terminal_ids),
            "latency_ms": {
                "p50": percentile(samples, 0.5),
                "p95": percentile(samples, 0.95),
                "max": max(samples),
            },
            "cold_load_ms": cold,
        }
    except InvalidInputError:
        raise
    except Exception:
        raise EvaluationUnavailableError() from None
    finally:
        directory.close()
        if snapshot is not None:
            snapshot.close()
        elif connection is not None:
            connection.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvalidInputError()


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _Parser(add_help=False)
        for flag in ("people-db", "history-db", "gold"):
            parser.add_argument("--" + flag, type=Path, required=True)
        args = parser.parse_args(argv)
        report = evaluate(args.people_db, args.history_db, args.gold)
        passed = (
            report["wrong_person"] == 0
            and report["expected_outcomes"] / report["queries"] >= 0.95
            and report["latency_ms"]["p95"] <= 20
        )
        code = 0 if passed else 1
    except Exception as exc:
        report = {
            "error_code": "invalid_input"
            if isinstance(exc, InvalidInputError)
            else "eval_unavailable",
            "queries": 0,
        }
        code = 2
    print(json.dumps(report, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
