"""One-time seeding of existing history into the raw archive (V1 spec §4.0).

Only items older than the live writer's START marker are seeded; everything after START
already exists as native lines. Sources are read read-only, in priority order. A message
already written from a higher-priority source is skipped (deduplicated by channel, chat and
message id). Session lines carry no message id, so they're only seeded when they're older
than every id-bearing item of the same chat. Each source becomes one sealed
``seed-<source>.jsonl`` file per channel with a MANIFEST entry.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import CLOSED_FILE_MODE, OPEN_FILE_MODE, append_line, dumps
from yeoman_shared.raw_archive.verify import record_closed
from yeoman_shared.raw_archive.writer import ARCHIVE_VERSION, month_of, read_start_ms, safe_channel
from yeoman_shared.utils.helpers import get_operational_data_path

SEED_SOURCES: tuple[str, ...] = ("journal", "reply_context", "session_jsonl", "memory2")


@dataclass(frozen=True, slots=True)
class SeedPaths:
    processing_db: Path
    reply_context_db: Path
    inbound_dir: Path
    knowledge_db: Path
    legacy_memory_db: Path | None = None

    @classmethod
    def default(cls) -> SeedPaths:
        data = get_operational_data_path()
        return cls(
            processing_db=data / "processing" / "processing.db",
            reply_context_db=data / "inbound" / "reply_context.db",
            inbound_dir=data / "inbound",
            knowledge_db=data / "knowledge" / "knowledge.db",
            legacy_memory_db=data / "memory" / "memory.db",
        )


@dataclass
class SeedReport:
    per_source: dict[str, dict[str, int]] = field(default_factory=dict)
    per_month: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class _Item:
    channel: str
    chat_id: str
    native_id: str
    received_ms: int
    kind: str
    direction: str
    account: str
    native: dict[str, Any]
    has_id: bool = True


def _iso_ms(value: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _readonly(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _journal(path: Path, counts: dict[str, int]) -> Iterator[_Item]:
    con = _readonly(path)
    if con is None:
        return
    with con:
        rows = con.execute(
            "SELECT event_id, kind, channel, chat_id, direction, source_message_id, created_ms,"
            " account, payload_json FROM events ORDER BY created_ms"
        )
        for row in rows:
            counts["read"] += 1
            if row["payload_json"] is None:
                counts["skipped_no_payload"] += 1
                continue
            yield _Item(
                channel=str(row["channel"]),
                chat_id=str(row["chat_id"] or ""),
                native_id=str(row["source_message_id"] or row["event_id"]),
                received_ms=int(row["created_ms"]),
                kind=str(row["kind"]),
                direction=str(row["direction"] or "in"),
                account=str(row["account"] or ""),
                native=json.loads(row["payload_json"]),
            )


def _reply_context(path: Path, counts: dict[str, int]) -> Iterator[_Item]:
    con = _readonly(path)
    if con is None:
        return
    with con:
        for row in con.execute("SELECT * FROM inbound_messages"):
            counts["read"] += 1
            stamp = row["timestamp"]
            received = int(stamp) * 1000 if stamp else _iso_ms(str(row["created_at"] or "")) or 0
            yield _Item(
                channel=str(row["channel"]),
                chat_id=str(row["chat_id"]),
                native_id=str(row["message_id"]),
                received_ms=received,
                kind="message",
                direction="in",
                account="",
                native={key: row[key] for key in row.keys()},
            )


def _session_jsonl(directory: Path, counts: dict[str, int]) -> Iterator[_Item]:
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*_*.jsonl")):
        if "_thread_" in path.name:
            continue
        channel, _, chat_id = path.stem.partition("_")
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, raw in enumerate(handle, start=1):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("_type") == "metadata":
                    continue
                counts["read"] += 1
                received = _iso_ms(str(record.get("timestamp") or ""))
                if received is None:
                    counts["skipped_no_time"] += 1
                    continue
                yield _Item(
                    channel=channel,
                    chat_id=chat_id,
                    native_id=f"session:{path.name}:{number}",
                    received_ms=received,
                    kind="message",
                    direction="in" if record.get("role") == "user" else "out",
                    account="",
                    native=record,
                    has_id=False,
                )


def _memory2(paths: SeedPaths, counts: dict[str, int]) -> Iterator[_Item]:
    databases = [paths.knowledge_db]
    if paths.legacy_memory_db is not None and paths.legacy_memory_db != paths.knowledge_db:
        databases.append(paths.legacy_memory_db)
    for path in databases:
        con = _readonly(path)
        if con is None:
            continue
        with con:
            rows = con.execute(
                "SELECT id, channel, chat_id, sender_id, content, source_message_id, created_at, kind"
                " FROM memory2_nodes WHERE kind IN ('utterance', 'whatsapp_message') AND is_deleted = 0"
            )
            for row in rows:
                counts["read"] += 1
                received = _iso_ms(str(row["created_at"] or ""))
                if received is None:
                    counts["skipped_no_time"] += 1
                    continue
                source_id = str(row["source_message_id"] or "")
                yield _Item(
                    channel=str(row["channel"] or "whatsapp"),
                    chat_id=str(row["chat_id"] or ""),
                    native_id=source_id or f"memory2:{row['id']}",
                    received_ms=received,
                    kind="message",
                    direction="in",
                    account="",
                    native={key: row[key] for key in row.keys()},
                    has_id=bool(source_id),
                )


def seed_raw_archive(
    root: Path, paths: SeedPaths, *, dry_run: bool = False, now_ms: int | None = None
) -> SeedReport:
    """Seed history older than START. Refuses to run twice or before the live writer ran."""
    start_ms = read_start_ms(root)
    if start_ms is None:
        raise RuntimeError(
            "raw archive has no START marker; run the gateway with the archive first"
        )
    if any(root.glob("*/seed-*.jsonl")):
        raise RuntimeError("raw archive already seeded; seed files exist")
    now = int(now_ms if now_ms is not None else time.time() * 1000)

    readers = {
        "journal": lambda c: _journal(paths.processing_db, c),
        "reply_context": lambda c: _reply_context(paths.reply_context_db, c),
        "session_jsonl": lambda c: _session_jsonl(paths.inbound_dir, c),
        "memory2": lambda c: _memory2(paths, c),
    }
    report = SeedReport(dry_run=dry_run)
    seen: set[tuple[str, str, str]] = set()
    earliest_with_id: dict[tuple[str, str], int] = {}
    pending: dict[tuple[str, str], list[_Item]] = defaultdict(list)
    session_candidates: list[tuple[str, _Item]] = []

    for source in SEED_SOURCES:
        counts: dict[str, int] = defaultdict(int)
        for name in (
            "read",
            "written",
            "skipped_duplicate",
            "skipped_not_before_start",
            "skipped_no_payload",
            "skipped_no_time",
        ):
            counts[name] = 0
        for item in readers[source](counts):
            if item.received_ms >= start_ms:
                counts["skipped_not_before_start"] += 1
                continue
            channel = safe_channel(item.channel)
            chat_key = (channel, item.chat_id)
            if item.has_id:
                key = (channel, item.chat_id, item.native_id)
                if key in seen:
                    counts["skipped_duplicate"] += 1
                    continue
                seen.add(key)
                earliest_with_id[chat_key] = min(
                    earliest_with_id.get(chat_key, item.received_ms), item.received_ms
                )
            elif source == "session_jsonl":
                pending.setdefault((source, channel), [])
                session_candidates.append((channel, item))
                continue
            elif item.received_ms >= earliest_with_id.get(chat_key, start_ms):
                counts["skipped_duplicate"] += 1
                continue
            pending[(source, channel)].append(item)
            counts["written"] += 1
            month = month_of(item.received_ms)
            report.per_month[month] = report.per_month.get(month, 0) + 1
        report.per_source[source] = dict(counts)

    session_counts = report.per_source["session_jsonl"]
    for channel, item in session_candidates:
        chat_key = (channel, item.chat_id)
        if item.received_ms >= earliest_with_id.get(chat_key, start_ms):
            session_counts["skipped_duplicate"] += 1
            continue
        pending[("session_jsonl", channel)].append(item)
        session_counts["written"] += 1
        month = month_of(item.received_ms)
        report.per_month[month] = report.per_month.get(month, 0) + 1

    for (source, channel), items in pending.items():
        if not items:
            continue
        target = root / channel / f"seed-{source}.jsonl"
        report.files.append(target.relative_to(root).as_posix())
        if dry_run:
            continue
        for item in sorted(items, key=lambda i: i.received_ms):
            append_line(
                target,
                dumps(
                    {
                        "archive_version": ARCHIVE_VERSION,
                        "received_ms": item.received_ms,
                        "channel": channel,
                        "kind": item.kind,
                        "direction": item.direction,
                        "native_id": item.native_id,
                        "chat_id": item.chat_id,
                        "account": item.account,
                        "correlation_id": "",
                        "provenance": source,
                        "media": None,
                        "native": item.native,
                    }
                ),
                mode=OPEN_FILE_MODE,
            )
        target.chmod(CLOSED_FILE_MODE)
        record_closed(root, target, now_ms=now, note="seed")
    return report
