"""Read-only aggregate counts for archived messages eligible for extraction.

Historical extraction is disabled. This utility only reports aggregate counts
from an explicitly supplied isolated archive.

Usage:
    uv run python scripts/backfill_memory_insights.py \
        --archive-db COPY/reply_context.db --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__:
    from .history_maintenance_guard import preflight_isolated_paths
else:
    from history_maintenance_guard import preflight_isolated_paths

from yeoman_gateway.history.export import require_isolated_paths
from yeoman_gateway.knowledge._memory.service import MemoryService, _BackgroundNoteEvent


def _load_archive(
    archive_db: Path,
    since: str | None,
    until: str | None,
    chat: str | None,
) -> dict[tuple[str, str], list[_BackgroundNoteEvent]]:
    conn = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    where = ["text IS NOT NULL", "text != ''"]
    params: list[object] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        params.append(until)
    if chat:
        where.append("chat_id = ?")
        params.append(chat)

    sql = f"""
        SELECT channel, chat_id, message_id, sender_id, text, timestamp
        FROM inbound_messages
        WHERE {" AND ".join(where)}
        ORDER BY channel, chat_id, timestamp ASC
    """
    by_chat: dict[tuple[str, str], list[_BackgroundNoteEvent]] = {}
    for row in conn.execute(sql, params):
        sender = (row["sender_id"] or "").strip()
        if not sender:
            continue
        ts = float(row["timestamp"] or 0)
        if ts <= 0:
            continue
        event = _BackgroundNoteEvent(
            sender_id=sender,
            message_id=row["message_id"],
            content=row["text"],
            ts=ts,
            mode="hybrid",
        )
        by_chat.setdefault((row["channel"], row["chat_id"]), []).append(event)
    conn.close()
    return by_chat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-db", required=True, type=Path)
    parser.add_argument("--since", help="ISO date, e.g. 2026-03-19")
    parser.add_argument("--until", help="ISO date, exclusive upper bound")
    parser.add_argument("--chat", help="Restrict to one chat_id")
    parser.add_argument(
        "--dry-run", action="store_true", required=True, help="required; report counts only"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=60, help="messages per reporting chunk"
    )
    args = parser.parse_args()

    preflight_isolated_paths(args.archive_db)
    require_isolated_paths(args.archive_db)
    archive_db = args.archive_db.expanduser().resolve()

    MemoryService._CHUNK_SIZE = args.chunk_size

    if not archive_db.exists():
        print(f"archive db not found: {archive_db}", file=sys.stderr)
        return 1

    by_chat = _load_archive(archive_db, args.since, args.until, args.chat)
    if not by_chat:
        print("no archived messages matched the filter")
        return 0

    total_events = sum(len(v) for v in by_chat.values())
    all_chunks: list[tuple[tuple[str, str], list[_BackgroundNoteEvent]]] = []
    for key, events in by_chat.items():
        for chunk in MemoryService._chunk_events(events):
            if chunk:
                all_chunks.append((key, chunk))

    print(f"chats:            {len(by_chat)}")
    print(f"total messages:   {total_events}")
    print(f"batches (chunks): {len(all_chunks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
