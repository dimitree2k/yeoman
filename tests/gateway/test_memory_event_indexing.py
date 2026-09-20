"""Focused checks for canonical WhatsApp text projection into existing FTS."""

from __future__ import annotations

import json
from pathlib import Path

from yeoman_gateway.knowledge._memory.store import MemoryStore
from yeoman_gateway.processing.models import CanonicalEvent
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore


def _event(*, event_id: str = "event-1", text: str = "canonical message") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_key=f"whatsapp:chat@g.us:{event_id}",
        trace_id=f"trace-{event_id}",
        kind="message",
        origin="whatsapp_bridge",
        principal="4915",
        channel="whatsapp",
        chat_id="chat@g.us",
        revision=1,
        occurred_ms=1_700_000_000_000,
        source_message_id=f"provider-{event_id}",
        payload={"text": text},
    )


def test_canonical_message_is_searchable_with_source_audience_metadata(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    event = _event()

    entries = store.index_canonical_event(
        event,
        audience={
            "status": "known",
            "members": ["4915", "4916"],
            "snapshot_id": "snapshot-1",
            "policy_revision": "7",
        },
    )

    assert len(entries) == 1
    assert [hit.entry.content for hit in store.search_lexical(
        workspace_id=entries[0].workspace_id,
        query="canonical message",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    )] == ["canonical message"]
    metadata = json.loads(entries[0].meta_json)
    assert metadata["source_event_id"] == "event-1"
    assert metadata["source_revision"] == 1
    assert metadata["audience_status"] == "known"
    assert metadata["audience_members"] == ["4915", "4916"]
    assert metadata["audience_snapshot_id"] == "snapshot-1"
    assert metadata["policy_revision"] == "7"
    store.close()


def test_duplicate_canonical_ingestion_has_one_node_and_one_fts_row(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    event = _event()

    first = store.index_canonical_event(event)
    second = store.index_canonical_event(event)

    assert [entry.id for entry in second] == [entry.id for entry in first]
    assert store._conn.execute("SELECT COUNT(*) FROM memory2_nodes").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM memory2_nodes_fts").fetchone()[0] == 1
    store.close()


def test_only_approved_enrichments_are_indexed_as_source_linked_nodes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    entries = store.index_canonical_event(
        _event(text="caption"),
        enrichments=(
            {"kind": "voice_transcript", "text": "approved transcript", "approved": True},
            {"kind": "image_description", "text": "private description", "approved": False},
        ),
    )

    assert {entry.kind for entry in entries} == {"whatsapp_message", "whatsapp_enrichment"}
    hits = store.search_lexical(
        workspace_id=entries[0].workspace_id,
        query="approved transcript",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    )
    assert [hit.entry.content for hit in hits] == ["approved transcript"]
    assert store.search_lexical(
        workspace_id=entries[0].workspace_id,
        query="private description",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    ) == []
    store.close()


def test_tombstoned_canonical_content_follows_existing_search_semantics(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    event = _event(text="content that must disappear")
    entries = store.index_canonical_event(event)

    assert store.search_lexical(
        workspace_id=entries[0].workspace_id,
        query="must disappear",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    )

    assert store.soft_delete_sources(["event-1"]) == 1
    assert store.search_lexical(
        workspace_id=entries[0].workspace_id,
        query="must disappear",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    ) == []
    assert store._conn.execute(
        "SELECT COUNT(*) FROM memory2_nodes_fts WHERE entry_id = ?", (entries[0].id,)
    ).fetchone()[0] == 0
    store.close()


def test_strict_delete_tombstones_the_canonical_fts_projection(tmp_path: Path) -> None:
    processing = ProcessingStore(tmp_path / "processing.db")
    memory = MemoryStore(tmp_path / "memory.db")
    sink = SignalJournalSink(processing, memory=memory, clock=lambda: 1_700_000_000_000)
    sink.capture(
        "message",
        {
            "chatJid": "chat@g.us",
            "messageId": "provider-1",
            "senderId": "4915",
            "text": "strictly tombstoned text",
        },
        event_id="event-1",
        event_key="wa:event-1",
        account="account-1",
        observed_at_ms=1_700_000_000_000,
        strict=True,
    )
    assert memory.search_lexical(
        workspace_id="canonical",
        query="tombstoned text",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    )

    sink.capture(
        "delete",
        {
            "chatJid": "chat@g.us",
            "messageId": "provider-1",
        },
        event_id="delete-1",
        event_key="wa:delete-1",
        account="account-1",
        observed_at_ms=1_700_000_000_001,
        strict=True,
    )
    assert memory.search_lexical(
        workspace_id="canonical",
        query="tombstoned text",
        scope_keys=["channel:whatsapp:chat:chat@g.us"],
    ) == []
    processing.close()
    memory.close()
