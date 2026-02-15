#!/usr/bin/env python3
"""List idea/backlog entries from nanobot semantic memory."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path


IDEA_MARKERS = ("[idea]", "idea:", "#idea", "inbox idea")
BACKLOG_MARKERS = ("[backlog]", "#backlog", "backlog:")
IDEA_PREFIX_WORDS = {
    "idea",      # EN/ES/IT
    "idee",      # DE/FR/NL (idée -> idee after accent fold)
    "ideia",     # PT
    "идея",      # RU
    "아이디어",   # KO
    "アイデア",    # JA
    "想法",       # ZH
}
IDEA_PREFIX_PHRASES = {
    "new idea",
    "inbox idea",
}
BACKLOG_PREFIX_WORDS = {
    "backlog",
    "todo",
    "aufgabe",   # DE
    "aufgaben",  # DE plural
    "tache",     # FR (tâche -> tache after accent fold)
    "tarea",     # ES
    "задача",    # RU
    "任务",       # ZH
    "할일",       # KO
}
BACKLOG_PREFIX_PHRASES = {
    "to do",
}


def _default_db_path() -> Path:
    config_path = Path.home() / ".nanobot" / "config.json"
    fallback = Path.home() / ".nanobot" / "memory" / "memory.db"
    try:
        if not config_path.exists():
            return fallback
        payload = json.loads(config_path.read_text())
        db_path = (
            payload.get("memory", {}).get("dbPath")
            or payload.get("memory", {}).get("db_path")
            or ""
        )
        if not isinstance(db_path, str) or not db_path.strip():
            return fallback
        return Path(db_path).expanduser()
    except Exception:
        return fallback


def _fold_accents(text: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def _leading_tokens(text: str, *, limit: int = 3) -> list[str]:
    folded = _fold_accents(text).lower()
    tokens = re.findall(r"[^\W_]+", folded, flags=re.UNICODE)
    return tokens[:limit]


def _status_of(content: str, *, mode: str) -> str | None:
    lowered = content.lower()
    tokens = _leading_tokens(content, limit=3)
    first = tokens[0] if tokens else ""
    first_two = " ".join(tokens[:2]) if len(tokens) >= 2 else first
    first_three = " ".join(tokens[:3]) if len(tokens) >= 3 else first_two

    if (
        first in BACKLOG_PREFIX_WORDS
        or first_two in BACKLOG_PREFIX_PHRASES
        or first_three in BACKLOG_PREFIX_PHRASES
    ):
        return "backlog"
    if any(token in lowered for token in BACKLOG_MARKERS):
        return "backlog"
    if (
        first in IDEA_PREFIX_WORDS
        or first_two in IDEA_PREFIX_PHRASES
        or first_three in IDEA_PREFIX_PHRASES
    ):
        return "idea"
    if any(token in lowered for token in IDEA_MARKERS):
        return "idea"
    if mode == "inbox":
        return "idea"
    return None


def _truncate(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def query_entries(
    *,
    db_path: Path,
    channel: str | None,
    chat_id: str | None,
    days: int,
    limit: int,
) -> list[sqlite3.Row]:
    if not db_path.exists():
        raise FileNotFoundError(f"memory db not found: {db_path}")

    since = (datetime.now(UTC) - timedelta(days=max(0, days))).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        clauses = [
            "is_deleted = 0",
            "updated_at >= ?",
        ]
        params: list[object] = [since]
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)

        sql = (
            "SELECT id, updated_at, channel, chat_id, sender_id, kind, content "
            "FROM memory2_nodes "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )
        params.append(max(1, limit))
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def print_table(rows: list[sqlite3.Row], *, status_filter: str, mode: str) -> int:
    out_rows: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        status = _status_of(str(row["content"] or ""), mode=mode)
        if status is None:
            continue
        if status_filter != "all" and status != status_filter:
            continue
        out_rows.append(
            (
                str(row["updated_at"])[:19],
                str(row["channel"] or "-"),
                str(row["chat_id"] or "-"),
                status,
                _truncate(str(row["content"] or "")),
            )
        )

    if not out_rows:
        print("No matching idea/backlog entries.")
        return 0

    widths = [19, 9, 18, 7, 120]
    header = ("updated_at", "channel", "chat_id", "status", "content")
    line = " | ".join(
        f"{h:<{w}}" if i < 4 else h for i, (h, w) in enumerate(zip(header, widths, strict=False))
    )
    print(line)
    print("-" * len(line))
    for updated_at, channel, chat_id, status, content in out_rows:
        print(
            f"{updated_at:<19} | {channel:<9} | {chat_id:<18} | {status:<7} | {content}"
        )
    return len(out_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="List idea/backlog entries from memory2_nodes.")
    parser.add_argument("--db-path", default=str(_default_db_path()), help="Path to memory SQLite DB")
    parser.add_argument("--channel", default=None, help="Optional channel filter")
    parser.add_argument("--chat-id", default=None, help="Optional chat_id filter")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--limit", type=int, default=500, help="Max rows to inspect before filtering")
    parser.add_argument(
        "--status",
        choices=("all", "idea", "backlog"),
        default="all",
        help="Filter by classified status",
    )
    parser.add_argument(
        "--mode",
        choices=("markers", "inbox"),
        default="markers",
        help="Classification mode: markers=explicit tags/prefixes, inbox=unmarked entries count as ideas",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser()
    try:
        rows = query_entries(
            db_path=db_path,
            channel=(args.channel or "").strip() or None,
            chat_id=(args.chat_id or "").strip() or None,
            days=args.days,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    count = print_table(rows, status_filter=args.status, mode=args.mode)
    print(f"\nTotal: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
