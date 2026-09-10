"""Backfill shared facts from the inbound archive (Plan 05/06 follow-up).

Extraction is normally triggered by a *new* settled turn, so history would never be
extracted on its own. This module makes the past reachable without inventing provenance:

* Messages come from the inbound archive, which is the complete record.
* Their source reference is ``archive:<message_id>`` at revision 1, which is visibly
  different from a journal event id - a fact derived from history can always be told
  apart from one derived from a live turn.
* Grouping is explicit (batches of N messages), because every batch costs one extraction
  call. Nothing runs unless a caller asks for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from loguru import logger

ARCHIVE_EVENT_PREFIX = "archive:"


def row_ms(row: Mapping[str, Any]) -> int | None:
    """Archive timestamps are epoch *seconds*; this module works in milliseconds."""
    stamp = row.get("timestamp")
    if not isinstance(stamp, (int, float)):
        return None
    value = int(stamp)
    return value * 1000 if value < 100_000_000_000 else value

#: The archive caps a single lookup at 300 rows.
PAGE_LIMIT = 300


@dataclass(frozen=True, slots=True)
class ArchiveEvent:
    """A journal-shaped view of one archived message."""

    event_id: str
    channel: str
    chat_id: str
    principal: str
    occurred_ms: int | None
    payload: Mapping[str, Any]
    payload_available: bool = True
    kind: str = "message"


class ArchiveEventSource:
    """Answers ``get_event`` like the journal, reading from the inbound archive."""

    def __init__(self, archive: Any) -> None:
        self._archive = archive
        self._cache: dict[str, ArchiveEvent] = {}

    def cache(self, events: Sequence[ArchiveEvent]) -> None:
        for event in events:
            self._cache[event.event_id] = event

    def get_event(self, event_id: str) -> ArchiveEvent | None:
        key = str(event_id)
        if not key.startswith(ARCHIVE_EVENT_PREFIX):
            return None
        return self._cache.get(key)


def archive_event(channel: str, chat_id: str, row: Mapping[str, Any]) -> ArchiveEvent:
    """Build the journal-shaped event for one archive row."""
    message_id = str(row.get("message_id") or "")
    sender = row.get("sender_id") or row.get("participant") or ""
    return ArchiveEvent(
        event_id=f"{ARCHIVE_EVENT_PREFIX}{message_id}",
        channel=str(channel),
        chat_id=str(chat_id),
        principal=str(sender or ""),
        occurred_ms=row_ms(row),
        payload={
            "text": str(row.get("text") or ""),
            "is_group": str(chat_id).endswith("@g.us"),
            "archived": True,
        },
    )


def iter_archive_messages(
    archive: Any,
    *,
    channel: str,
    chat_id: str,
    since_ms: int,
    limit: int | None = None,
) -> list[Mapping[str, Any]]:
    """Archived messages of one chat since ``since_ms``, oldest first.

    Pages backwards from the newest message, so a long history is read without loading
    the whole table into memory.
    """
    if not channel or not chat_id:
        return []
    wanted = None if limit is None else max(1, int(limit))
    since = datetime.fromtimestamp(int(since_ms) / 1000, tz=UTC)
    rows = list(
        archive.lookup_messages_in_range(
            channel, chat_id, since=since, latest=True, limit=PAGE_LIMIT
        )
    )
    collected: list[Mapping[str, Any]] = list(rows)
    while rows and len(rows) == PAGE_LIMIT and (wanted is None or len(collected) < wanted):
        oldest = rows[0]
        oldest_id = str(oldest.get("message_id") or "")
        stamp = row_ms(oldest)
        if not oldest_id or stamp is None or stamp <= int(since_ms):
            break
        rows = list(
            archive.lookup_messages_before(channel, chat_id, oldest_id, limit=PAGE_LIMIT)
        )
        if not rows:
            break
        collected = rows + collected
    deduped: dict[str, Mapping[str, Any]] = {}
    for row in collected:
        stamp = row_ms(row)
        if stamp is not None and stamp < int(since_ms):
            continue
        key = str(row.get("message_id") or "")
        if key and key not in deduped:
            deduped[key] = row
    ordered = sorted(
        deduped.values(),
        key=lambda row: (row_ms(row) or 0, str(row.get("message_id") or "")),
    )
    if wanted is not None:
        ordered = ordered[-wanted:]
    return ordered


@dataclass(frozen=True, slots=True)
class BackfillBatch:
    """One extraction call's worth of archived messages."""

    channel: str
    chat_id: str
    message_ids: tuple[str, ...]

    @property
    def source_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple((f"{ARCHIVE_EVENT_PREFIX}{message_id}", 1) for message_id in self.message_ids)


def build_batches(
    rows: Sequence[Mapping[str, Any]],
    *,
    channel: str,
    chat_id: str,
    batch_size: int = 20,
    max_batches: int | None = None,
) -> list[BackfillBatch]:
    size = max(1, int(batch_size))
    batches: list[BackfillBatch] = []
    for start in range(0, len(rows), size):
        chunk = rows[start : start + size]
        ids = tuple(str(row.get("message_id") or "") for row in chunk)
        ids = tuple(item for item in ids if item)
        if ids:
            batches.append(BackfillBatch(channel=channel, chat_id=chat_id, message_ids=ids))
    if max_batches is not None:
        batches = batches[: max(1, int(max_batches))]
    return batches


@dataclass(slots=True)
class BackfillReport:
    """What a backfill planned or did - batches are the model-call count."""

    batches: int = 0
    messages: int = 0
    published: int = 0
    dry_run: bool = False
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def estimated_calls(self) -> int:
        return self.batches

    def as_lines(self) -> list[str]:
        mode = "would run" if self.dry_run else "ran"
        lines = [
            f"{mode} {self.batches} extraction call(s) over {self.messages} archived message(s)"
        ]
        if not self.dry_run:
            lines.append(f"published {self.published} fact(s)")
        for reason, count in sorted(self.reasons.items()):
            lines.append(f"  {reason}: {count}")
        return lines


def run_archive_backfill(
    *,
    archive: Any,
    queue: Any,
    channel: str,
    chat_id: str,
    workspace_id: str,
    since_ms: int,
    batch_size: int = 20,
    max_batches: int | None = None,
    max_messages: int | None = None,
    dry_run: bool = True,
    now_ms: int | None = None,
) -> BackfillReport:
    """Plan (or run) extraction over archived history. Dry run by default."""
    from yeoman_gateway.memory.read_gate import chat_scope_key as scope_key

    stamp = int(now_ms if now_ms is not None else datetime.now(UTC).timestamp() * 1000)
    rows = iter_archive_messages(
        archive, channel=channel, chat_id=chat_id, since_ms=since_ms, limit=max_messages
    )
    batches = build_batches(
        rows, channel=channel, chat_id=chat_id, batch_size=batch_size, max_batches=max_batches
    )
    report = BackfillReport(
        batches=len(batches),
        messages=sum(len(batch.message_ids) for batch in batches),
        dry_run=bool(dry_run),
    )
    if dry_run:
        return report

    source = queue._journal if hasattr(queue, "_journal") else None
    scope = scope_key(channel, chat_id)
    for batch in batches:
        if isinstance(source, ArchiveEventSource):
            source.cache(
                [
                    archive_event(channel, chat_id, row)
                    for row in rows
                    if str(row.get("message_id") or "") in batch.message_ids
                ]
            )
        queue.enqueue(
            turn_ref=f"backfill:{batch.message_ids[0]}",
            source_refs=batch.source_refs,
            now_ms=stamp,
            workspace_id=workspace_id,
            chat_scope_key=scope,
        )
        outcome = queue.run_due(now_ms=stamp)
        report.published += outcome.published
        for reason, count in outcome.reasons.items():
            report.reasons[reason] = report.reasons.get(reason, 0) + count
        logger.info(
            "shared fact backfill batch chat={} messages={} published={}",
            chat_id,
            len(batch.message_ids),
            outcome.published,
        )
    return report


def iter_chat_ids(archive: Any, *, channel: str | None = None) -> list[tuple[str, str]]:
    """Distinct (channel, chat_id) pairs in the archive, busiest first."""
    with archive._lock:
        rows = archive._conn.execute(
            "SELECT channel, chat_id, COUNT(*) AS c FROM inbound_messages"
            " GROUP BY channel, chat_id ORDER BY c DESC"
        ).fetchall()
    pairs: list[tuple[str, str]] = []
    for row in rows:
        if channel and str(row["channel"]) != channel:
            continue
        pairs.append((str(row["channel"]), str(row["chat_id"])))
    return pairs
