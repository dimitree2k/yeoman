"""Rebuild drill: replay one chat's native raw lines into an empty journal (V1 spec §4.0).

The drill proves that the raw archive alone recreates the canonical journal. It replays
native inbound WhatsApp frames through the same ``SignalJournalSink.capture`` the live
channel uses, skipping duplicates (a frame resent after a restart) and suppressed lines.
Deletes are replayed as deletes; the original stays in the archive. The drill never writes
to the live home.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from yeoman_shared.raw_archive.records import archive_files, is_month_stem, iter_records
from yeoman_shared.raw_archive.verify import load_suppressions
from yeoman_shared.raw_archive.writer import read_start_ms, safe_channel
from yeoman_shared.utils.helpers import get_data_path
from yeoman_shared.whatsapp_protocol import REPLAYABLE_EVENT_TYPES

from yeoman_gateway.processing.models import JournalConflictError
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore


@dataclass(frozen=True, slots=True)
class RebuildReport:
    lines_read: int
    replayed: int
    duplicates: int
    suppressed: int
    conflicts: int
    skipped_other: int
    journal_event_ids: tuple[str, ...]
    missing_vs_live: tuple[str, ...] = ()
    extra_vs_live: tuple[str, ...] = ()


def _live_event_ids(path: Path, *, channel: str, chat_id: str, since_ms: int) -> set[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT event_id FROM events WHERE channel = ? AND chat_id = ? AND direction = 'in'"
            " AND created_ms >= ?",
            (channel, chat_id, since_ms),
        )
        return {str(row[0]) for row in rows}
    finally:
        con.close()


def rebuild_chat(
    archive_root: Path,
    *,
    channel: str,
    chat_id: str,
    target_home: Path,
    live_processing_db: Path | None = None,
) -> RebuildReport:
    live_home = get_data_path().resolve()
    target = target_home.expanduser().resolve()
    if target == live_home or live_home in target.parents:
        raise RuntimeError("refusing to rebuild into the live YEOMAN_HOME")
    journal_path = target / "data" / "processing" / "processing.db"
    if journal_path.exists():
        raise RuntimeError(f"rebuild target must be empty: {journal_path} exists")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    sink = SignalJournalSink(ProcessingStore(journal_path))

    name = safe_channel(channel)
    suppressed_keys = load_suppressions(archive_root)
    seen: set[tuple[str, str]] = set()
    lines_read = replayed = duplicates = suppressed = conflicts = skipped = 0
    event_ids: list[str] = []
    records = [
        record
        for path in archive_files(archive_root, name)
        if is_month_stem(path.stem)
        for _, record, _ in iter_records(path)
        if record is not None and record.get("chat_id") == chat_id
    ]
    for record in sorted(records, key=lambda r: int(r.get("received_ms") or 0)):
        lines_read += 1
        frame = record.get("native")
        if (
            record.get("direction") != "in"
            or record.get("kind") not in REPLAYABLE_EVENT_TYPES
            or not isinstance(frame, dict)
            or not isinstance(frame.get("payload"), dict)
        ):
            skipped += 1
            continue
        if (name, chat_id, str(record.get("native_id") or "")) in suppressed_keys:
            suppressed += 1
            continue
        fingerprint = hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()
        key = (str(frame.get("eventId") or ""), fingerprint)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        try:
            sink.capture(
                str(frame["type"]),
                frame["payload"],
                event_id=str(frame["eventId"]),
                event_key=str(frame["eventKey"]),
                account=str(frame.get("accountId") or ""),
                observed_at_ms=int(frame["observedAt"]),
                strict=True,
            )
        except JournalConflictError:
            conflicts += 1
            continue
        replayed += 1
        event_ids.append(str(frame["eventId"]))

    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    if live_processing_db is not None:
        # Month files only hold lines since the writer's START, so compare against the live
        # journal from the same moment on; older live events exist only as seed lines.
        since = read_start_ms(archive_root) or 0
        live = _live_event_ids(live_processing_db, channel=name, chat_id=chat_id, since_ms=since)
        rebuilt = set(event_ids)
        missing = tuple(sorted(live - rebuilt))
        extra = tuple(sorted(rebuilt - live))
    return RebuildReport(
        lines_read,
        replayed,
        duplicates,
        suppressed,
        conflicts,
        skipped,
        tuple(event_ids),
        missing,
        extra,
    )
