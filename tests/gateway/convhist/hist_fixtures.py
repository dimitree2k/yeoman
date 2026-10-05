"""Builders for fake source homes used by the converter tests (no real data)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def make_db(path: Path, ddl: str, rows: dict[str, list[dict[str, Any]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    for table, items in rows.items():
        for item in items:
            cols = ", ".join(item)
            marks = ", ".join("?" for _ in item)
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(item.values()))
    conn.commit()
    conn.close()
    return path

def write_jsonl(path: Path, records: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

INBOUND_DDL = """
CREATE TABLE inbound_messages (
 channel TEXT, chat_id TEXT, message_id TEXT, participant TEXT, sender_id TEXT, text TEXT,
 timestamp INTEGER, created_at TEXT, sender_name TEXT, reply_to_message_id TEXT
);
"""
