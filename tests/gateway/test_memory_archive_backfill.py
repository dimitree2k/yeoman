"""Backfill from history: planned, visible, and never a surprise model bill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from yeoman_gateway.memory.archive_backfill import (
    ARCHIVE_EVENT_PREFIX,
    ArchiveEventSource,
    archive_event,
    build_batches,
    iter_archive_messages,
    iter_chat_ids,
    run_archive_backfill,
)
from yeoman_gateway.memory.extraction_jobs import SharedFactExtractionQueue
from yeoman_gateway.memory.store import MemoryStore
from yeoman_gateway.storage.inbound_archive import InboundArchive

CHAT = "gruppe@g.us"
CHANNEL = "whatsapp"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _archive(tmp_path: Path, count: int = 5, *, channel: str = CHANNEL, chat: str = CHAT) -> InboundArchive:
    archive = InboundArchive(db_path=tmp_path / "archive.db", retention_days=None)
    base = int((NOW - timedelta(days=2)).timestamp())
    for index in range(count):
        archive.record_inbound(
            channel=channel,
            chat_id=chat,
            message_id=f"m{index}",
            participant=None,
            sender_id="member-old",
            text=f"Nachricht {index}",
            timestamp=base + index * 60,
            sender_name="Old",
        )
    return archive


class _Extractor:
    """Records the events it was asked about and returns one fact per batch."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, events):
        from yeoman_gateway.memory.extraction_jobs import SharedFactCandidate

        ids = [getattr(event, "event_id", "") for event in events]
        self.calls.append(ids)
        refs = tuple((event_id, 1) for event_id in ids if event_id)
        return [
            SharedFactCandidate(
                content="Der Stammtisch ist donnerstags.",
                author_principal="member-old",
                visibility_scope="author_only",
                source_refs=refs,
                audience=frozenset({"member-old"}),
            )
        ]


def _queue(store: MemoryStore, archive: InboundArchive, extractor) -> SharedFactExtractionQueue:
    return SharedFactExtractionQueue(
        store=store,
        journal=ArchiveEventSource(archive),
        extractor=extractor,
        clock=lambda: 1_700_000_000_000,
    )


def test_archive_event_is_journal_shaped(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 1)
    row = archive.lookup_messages_in_range(
        CHANNEL, CHAT, since=NOW - timedelta(days=5), latest=True, limit=1
    )[0]

    event = archive_event(CHANNEL, CHAT, row)

    assert event.event_id == f"{ARCHIVE_EVENT_PREFIX}m0"
    assert event.principal == "member-old"
    assert event.payload["text"] == "Nachricht 0"
    assert event.payload_available is True
    archive.close()


def test_source_only_answers_for_archive_ids(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 1)
    source = ArchiveEventSource(archive)

    assert source.get_event("some-journal-event") is None
    assert source.get_event(f"{ARCHIVE_EVENT_PREFIX}m0") is None  # not cached yet
    archive.close()


def test_messages_are_read_oldest_first_since_the_cutoff(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 5)
    since_ms = int((NOW - timedelta(days=2)).timestamp() * 1000) + 60_000  # skip the first

    rows = iter_archive_messages(archive, channel=CHANNEL, chat_id=CHAT, since_ms=since_ms)

    assert [row["message_id"] for row in rows] == ["m1", "m2", "m3", "m4"]
    archive.close()


def test_batches_group_messages_and_respect_the_cap(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 5)
    rows = iter_archive_messages(
        archive, channel=CHANNEL, chat_id=CHAT, since_ms=0
    )

    batches = build_batches(rows, channel=CHANNEL, chat_id=CHAT, batch_size=2)
    capped = build_batches(rows, channel=CHANNEL, chat_id=CHAT, batch_size=2, max_batches=1)

    assert [len(batch.message_ids) for batch in batches] == [2, 2, 1]
    assert batches[0].source_refs == ((f"{ARCHIVE_EVENT_PREFIX}m0", 1), (f"{ARCHIVE_EVENT_PREFIX}m1", 1))
    assert len(capped) == 1
    archive.close()


def test_dry_run_plans_without_calling_anything(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 5)
    store = MemoryStore(tmp_path / "memory.db")
    extractor = _Extractor()
    queue = _queue(store, archive, extractor)

    report = run_archive_backfill(
        archive=archive, queue=queue, channel=CHANNEL, chat_id=CHAT,
        workspace_id="ws1", since_ms=0, batch_size=2, dry_run=True,
    )

    assert report.batches == 3
    assert report.messages == 5
    assert report.estimated_calls == 3
    assert extractor.calls == []
    assert store.count_fact_jobs() == 0
    assert "would run 3 extraction call(s)" in report.as_lines()[0]
    archive.close()
    store.close()


def test_apply_publishes_facts_with_archive_provenance(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 4)
    store = MemoryStore(tmp_path / "memory.db")
    extractor = _Extractor()
    queue = _queue(store, archive, extractor)

    report = run_archive_backfill(
        archive=archive, queue=queue, channel=CHANNEL, chat_id=CHAT,
        workspace_id="ws1", since_ms=0, batch_size=2, dry_run=False,
        now_ms=1_700_000_000_000,
    )

    assert report.batches == 2
    assert report.published == 2
    assert len(extractor.calls) == 2  # one model call per batch, nothing more
    facts = store.list_facts()
    assert len(facts) == 2
    sources = {source.source_event_id for fact in facts for source in fact.sources}
    assert all(item.startswith(ARCHIVE_EVENT_PREFIX) for item in sources)
    assert store.has_fact_embeddings() is False  # no embedder in this harness
    archive.close()
    store.close()


def test_chats_are_listed_busiest_first(tmp_path: Path) -> None:
    archive = _archive(tmp_path, 3, chat="busy@g.us")
    for index in range(1):
        archive.record_inbound(
            channel=CHANNEL, chat_id="quiet@g.us", message_id=f"q{index}",
            participant=None, sender_id="x", text="hi", timestamp=1, sender_name=None,
        )

    pairs = iter_chat_ids(archive, channel=CHANNEL)

    assert pairs[0] == (CHANNEL, "busy@g.us")
    assert (CHANNEL, "quiet@g.us") in pairs
    archive.close()
