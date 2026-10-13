"""P0 baseline: content-free latency and outcome counts of today's history person resolver.

Run on an isolated copy of history.db, never on the live file:

    python scripts/bench_person_resolver.py --history-db /tmp/copy/history.db [--json]

The report holds counts and timings only, never names or identifiers.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any


def _open(history_db: Path) -> tuple[sqlite3.Connection, int]:
    conn = sqlite3.connect(f"{history_db.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    row = conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()
    state = json.loads(row[0]) if row is not None else {}
    generation = state.get("generation") if isinstance(state, dict) else None
    if not isinstance(generation, int):
        conn.close()
        raise SystemExit("history.db has no runtime generation; is it a projected history?")
    return conn, generation


def _queries(conn: sqlite3.Connection) -> list[str]:
    from yeoman_gateway.history.queries import _name_tokens

    names = [str(r[0]) for r in conn.execute(
        "SELECT display_name FROM contacts WHERE kind = 'person' AND merged_into IS NULL "
        "AND display_name IS NOT NULL AND coalesce(role, '') <> 'assistant' ORDER BY contact_id")]
    result: list[str] = []
    for name in names:
        tokens = _name_tokens(name)
        if not tokens:
            continue
        result.append(name)
        result.append(tokens[0])
    return list(dict.fromkeys(result))


def _owner_principal(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT i.value FROM identifier_history i JOIN contacts c ON c.contact_id = i.contact_id "
        "WHERE c.role = 'owner' AND i.kind = 'pn_jid' ORDER BY i.id LIMIT 1").fetchone()
    return "whatsapp:" + str(row[0]).split("@", 1)[0].split(":", 1)[0] if row else None


def _percentile(samples: list[float], q: int) -> float:
    if not samples:
        return 0.0
    if len(samples) < 2:
        return samples[0]
    return statistics.quantiles(samples, n=100)[q - 1]


def run_benchmark(history_db: Path, *, repeats: int = 3) -> dict[str, Any]:
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.history.reader import HistorySnapshot

    conn, generation = _open(history_db)
    snapshot = HistorySnapshot(generation, (), conn)
    try:
        at_ms = int(conn.execute(
            "SELECT max(t) FROM (SELECT max(sent_ms) AS t FROM messages "
            "UNION ALL SELECT max(occurred_ms) FROM message_events)").fetchone()[0] or 0) + 1
        queries = _queries(conn)
        scope = "owner_groups"
        principal = _owner_principal(conn)
        chat_ids = HistoryQueries(snapshot).owner_member_chat_ids(principal, at_ms) if principal else ()
        if not chat_ids:
            scope = "all_chats"
            chat_ids = tuple(str(r["chat_id"]) for r in HistoryQueries(snapshot).chats()
                             if str(r["chat_id"]).endswith("@g.us"))
        outcomes = {"resolved": 0, "ambiguous": 0, "not_found": 0}
        samples: list[float] = []
        for repeat in range(max(1, repeats)):
            for query in queries:
                lookup = HistoryQueries(snapshot)            # cold: one lookup per turn, as live
                started = time.perf_counter_ns()
                result = lookup.resolve_person(query, chat_ids, at_ms)
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
                if repeat == 0:
                    outcomes[result.status] += 1
        persons = conn.execute(
            "SELECT count(*) FROM contacts WHERE kind = 'person' AND merged_into IS NULL").fetchone()[0]
        return {
            "queries": len(queries),
            "outcomes": outcomes,
            "latency_ms": {
                "p50": round(_percentile(samples, 50), 3),
                "p95": round(_percentile(samples, 95), 3),
                "max": round(max(samples, default=0.0), 3),
            },
            "persons": int(persons),
            "chats_in_scope": len(chat_ids),
            "scope": scope,
            "repeats": max(1, repeats),
        }
    finally:
        snapshot.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history-db", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_benchmark(args.history_db, repeats=args.repeats)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
