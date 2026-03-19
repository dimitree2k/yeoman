"""SQLite-backed telemetry — gateway writes, overseer reads."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SqliteTelemetry:
    """Persistent telemetry store using SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT NOT NULL,
                labels TEXT DEFAULT '{}',
                value REAL NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gauges (
                name TEXT NOT NULL,
                labels TEXT DEFAULT '{}',
                value REAL NOT NULL,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_counters_name_ts ON counters(name, ts);
            CREATE INDEX IF NOT EXISTS idx_gauges_name_ts ON gauges(name, ts);
        """)

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO counters (name, labels, value, ts) VALUES (?, ?, ?, ?)",
            (name, json.dumps(dict(labels)), value, ts),
        )
        self._conn.commit()

    def gauge(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO gauges (name, labels, value, ts) VALUES (?, ?, ?, ?)",
            (name, json.dumps(dict(labels)), value, ts),
        )
        self._conn.commit()

    def histogram(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        self.incr(name, int(value), labels)

    def timing(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        self.gauge(name, value, labels)

    def query_counters(self, name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, labels, value, ts FROM counters WHERE name = ? ORDER BY ts DESC LIMIT ?",
            (name, limit),
        ).fetchall()
        return [{"name": r[0], "labels": json.loads(r[1]), "value": r[2], "ts": r[3]} for r in rows]

    def query_gauges(self, name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, labels, value, ts FROM gauges WHERE name = ? ORDER BY ts DESC LIMIT ?",
            (name, limit),
        ).fetchall()
        return [{"name": r[0], "labels": json.loads(r[1]), "value": r[2], "ts": r[3]} for r in rows]

    def counter_sum(self, name: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(value), 0) FROM counters WHERE name = ?", (name,),
        ).fetchone()
        return row[0] if row else 0.0
