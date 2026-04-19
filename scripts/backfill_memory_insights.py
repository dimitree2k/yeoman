"""Retro-extract distilled memory from the WhatsApp/Telegram inbound archive.

Reads inbound_messages from reply_context.db, groups them into batches that
mirror the live _flush_background_buffer path (5-min topic boundaries,
12-message chunks), and feeds each batch through the configured LLM
extractor in mode=hybrid. Distilled candidates land in memory2_nodes via
_persist_candidate, so the ACL, scope routing, and dedup index all apply —
re-running is safe.

Designed to be run with the gateway STOPPED so the live MemoryService isn't
competing for the SQLite WAL or hammering the same LLM API key. Dry-run does
not call the LLM — it only prints batch counts and per-chat coverage.

Usage:
    yeoman gateway stop
    uv run python scripts/backfill_memory_insights.py --dry-run
    uv run python scripts/backfill_memory_insights.py --since 2026-03-19
    yeoman gateway start
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from yeoman_gateway.contacts.service import ContactsService
from yeoman_gateway.memory.service import (
    MemoryService,
    _BackgroundNoteBuffer,
    _BackgroundNoteEvent,
)
from yeoman_shared.config.loader import load_config
from yeoman_shared.utils.helpers import get_operational_data_path

ARCHIVE_DB = Path("~/.yeoman/data/inbound/reply_context.db").expanduser()


@dataclass(slots=True)
class ChunkResult:
    chat_id: str
    events: int
    saved: int


def _load_archive(
    since: str | None,
    until: str | None,
    chat: str | None,
) -> dict[tuple[str, str], list[_BackgroundNoteEvent]]:
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
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
    parser.add_argument("--since", help="ISO date, e.g. 2026-03-19")
    parser.add_argument("--until", help="ISO date, exclusive upper bound")
    parser.add_argument("--chat", help="Restrict to one chat_id")
    parser.add_argument("--dry-run", action="store_true", help="count batches, no LLM calls")
    parser.add_argument(
        "--limit-batches", type=int, default=0, help="max batches to process (0 = all)"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=60,
        help="messages per extraction window (live default is 12; raise for backfill)",
    )
    parser.add_argument(
        "--max-candidates", type=int, default=30,
        help="accepted candidates cap per batch (live default is 4)",
    )
    args = parser.parse_args()

    MemoryService._CHUNK_SIZE = args.chunk_size

    if not ARCHIVE_DB.exists():
        print(f"archive db not found: {ARCHIVE_DB}", file=sys.stderr)
        return 1

    by_chat = _load_archive(args.since, args.until, args.chat)
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
    if args.dry_run:
        for (channel, chat_id), events in sorted(by_chat.items(), key=lambda e: -len(e[1])):
            chunks = sum(1 for k, _ in all_chunks if k == (channel, chat_id))
            print(f"  {channel:<9} {chat_id:<40} msgs={len(events):>5}  chunks={chunks}")
        return 0

    # Live run: wire the full MemoryService with extractor + contacts.
    config = load_config()
    workspace = config.workspace_path
    memory = MemoryService(workspace=workspace, config=config.memory, root_config=config)
    if memory.extractor is None:
        print("extractor not available — check models.routes config", file=sys.stderr)
        return 2
    # Widen the per-batch candidate cap for backfill — live setting of 4 would
    # throw away most of the signal when we feed 60-message chunks.
    memory.config.capture.max_candidates_per_message = args.max_candidates
    contacts = ContactsService(
        db_path=get_operational_data_path() / "contacts" / "contacts.db",
    )
    memory.set_contacts(contacts)

    results: list[ChunkResult] = []
    stop_at = args.limit_batches or len(all_chunks)
    t0 = time.time()
    for i, ((channel, chat_id), chunk) in enumerate(all_chunks):
        if i >= stop_at:
            break
        buf = _BackgroundNoteBuffer(
            channel=channel,
            chat_id=chat_id,
            is_group=chat_id.endswith("@g.us") or "-" in chat_id,
            events=chunk,
            first_ts=chunk[0].ts,
            batch_interval_seconds=0,
            batch_max_messages=len(chunk),
        )
        before = memory._background_notes_saved_total
        try:
            memory._flush_background_buffer(buf)
        except Exception as exc:
            print(f"  [{i+1}/{stop_at}] {chat_id} FAILED: {exc}", file=sys.stderr)
            continue
        saved = memory._background_notes_saved_total - before
        results.append(ChunkResult(chat_id=chat_id, events=len(chunk), saved=saved))
        print(
            f"  [{i+1}/{stop_at}] {chat_id[:50]:<50} "
            f"msgs={len(chunk):>2} saved={saved}"
        )

    dt = time.time() - t0
    print()
    print(f"processed: {len(results)} batches in {dt:.1f}s")
    print(f"saved:     {sum(r.saved for r in results)} memory rows")
    per_chat: dict[str, int] = {}
    for r in results:
        per_chat[r.chat_id] = per_chat.get(r.chat_id, 0) + r.saved
    for chat_id, n in sorted(per_chat.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {chat_id:<50} +{n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
