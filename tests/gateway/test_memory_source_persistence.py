"""Task A: durable WhatsApp source authority and revocation projections."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


def _source(event_id: str = "event-message", revision: int = 1) -> SourceRef:
    return SourceRef(
        event_id=event_id,
        revision=revision,
        channel="whatsapp",
        chat_id=CHAT,
        author_principal="4915",
        occurred_at_ms=T0,
    )


def _message(store: ProcessingStore, *, event_id: str = "event-message") -> None:
    SignalJournalSink(store, clock=lambda: T0).capture(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "provider-message-1",
            "senderId": "4915@s.whatsapp.net",
            "text": "original source text",
            "timestamp": T0,
        },
        event_id=event_id,
        event_key=f"whatsapp:{CHAT}:message:provider-message-1",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )


@pytest.mark.parametrize("kind", ["edit", "delete"])
def test_strict_edit_delete_projection_failure_propagates_after_append(kind: str) -> None:
    class _FailingProjectionStore:
        def __init__(self) -> None:
            self.appended: list[dict[str, object]] = []

        def append_event(self, **kwargs: object) -> str:
            self.appended.append(kwargs)
            return str(kwargs["event_id"])

        def project_source_revocation(self, signal: object, *, now_ms: int | None = None):
            del signal, now_ms
            raise RuntimeError("projection unavailable")

    store = _FailingProjectionStore()
    sink = SignalJournalSink(store, clock=lambda: T0)
    payload = {"chatJid": CHAT, "messageId": f"provider-{kind}"}
    if kind == "edit":
        payload["text"] = "replacement source text"

    with pytest.raises(RuntimeError, match="projection unavailable"):
        sink.capture(
            kind,
            payload,
            event_id=f"event-{kind}-projection-failure",
            event_key=f"whatsapp:{CHAT}:{kind}:projection-failure",
            account="account-a",
            observed_at_ms=T0,
            strict=True,
        )

    assert [entry["event_id"] for entry in store.appended] == [
        f"event-{kind}-projection-failure"
    ]


def test_source_authority_survives_restart_without_runtime_archive(tmp_path: Path) -> None:
    path = tmp_path / "processing.db"
    store = ProcessingStore(path)
    _message(store)
    authority = RuntimeKnowledgeSources(processing_store=store)
    source = _source()
    authority.register_source(
        source,
        EvidenceAudience.known({"4915", "4916"}, snapshot_id="snap-1"),
        policy_revision=7,
    )
    store.close()

    reopened = ProcessingStore(path)
    restored = RuntimeKnowledgeSources(processing_store=reopened)
    audience = restored.evidence_audience(source, basis="reply")
    assert restored.verify_source(source)
    assert audience is not None
    assert audience.status == "known"
    assert audience.members == frozenset({"4915", "4916"})
    assert audience.snapshot_id == "snap-1"
    assert restored.policy_revision(source) == 7
    assert restored.archive == {}
    reopened.close()


def test_delete_is_append_only_and_revokes_original_source_idempotently(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    _message(store)
    authority = RuntimeKnowledgeSources(processing_store=store)
    source = _source()
    authority.register_source(source, EvidenceAudience.author_only(snapshot_id="snap-1"))

    sink = SignalJournalSink(store, clock=lambda: T0 + 10)
    delete_id = sink.capture(
        "delete",
        {
            "chatJid": CHAT,
            "messageId": "provider-message-1",
        },
        event_id="event-delete",
        event_key=f"whatsapp:{CHAT}:delete:provider-message-1",
        account="account-a",
        observed_at_ms=T0 + 10,
        strict=True,
    )

    assert delete_id == "event-delete"
    original = store.get_event("event-message")
    tombstone = store.get_event("event-delete")
    assert original is not None and original.payload is not None
    assert original.payload["text"] == "original source text"
    assert tombstone is not None and tombstone.kind == "delete"
    assert authority.source_revoked(source)
    assert authority.revocation_event_id(source) == "event-delete"

    # Replaying the same provider event does not append or revoke twice.
    assert sink.capture(
        "delete",
        {
            "chatJid": CHAT,
            "messageId": "provider-message-1",
        },
        event_id="event-delete",
        event_key=f"whatsapp:{CHAT}:delete:provider-message-1",
        account="account-a",
        observed_at_ms=T0 + 20,
        strict=True,
    ) == "event-delete"
    assert store.count_events() == 2
    assert authority.revocation_event_id(source) == "event-delete"
    store.close()


def test_edit_revokes_previous_revision_and_retains_both_events(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    _message(store)
    authority = RuntimeKnowledgeSources(processing_store=store)
    source = _source()
    authority.register_source(source, EvidenceAudience.known({"4915"}, snapshot_id="snap-1"))

    sink = SignalJournalSink(store, clock=lambda: T0 + 10)
    sink.capture(
        "edit",
        {
            "chatJid": CHAT,
            "messageId": "provider-message-1",
            "senderId": "4915@s.whatsapp.net",
            "text": "replacement source text",
            "revision": 2,
        },
        event_id="event-edit",
        event_key=f"whatsapp:{CHAT}:edit:provider-message-1:2",
        account="account-a",
        observed_at_ms=T0 + 10,
        strict=True,
    )

    assert authority.source_revoked(source)
    assert authority.revocation_event_id(source) == "event-edit"
    replacement = authority.source_for_event("event-edit", 2)
    assert replacement is not None
    assert not authority.source_revoked(replacement)
    assert store.get_event("event-message") is not None
    assert store.get_event("event-edit") is not None
    assert store.get_event("event-edit").payload["text"] == "replacement source text"  # type: ignore[union-attr]
    store.close()


def test_delete_in_other_account_does_not_revoke_source_authority(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    _message(store)
    authority = RuntimeKnowledgeSources(processing_store=store)
    source = _source()
    authority.register_source(source, EvidenceAudience.author_only(snapshot_id="snap-1"))

    SignalJournalSink(store, clock=lambda: T0 + 10).capture(
        "delete",
        {
            "chatJid": CHAT,
            "messageId": "provider-message-1",
            "senderId": "other@s.whatsapp.net",
        },
        event_id="event-foreign-delete",
        event_key=f"whatsapp:{CHAT}:delete:provider-message-1:foreign",
        account="account-b",
        observed_at_ms=T0 + 10,
        strict=True,
    )

    assert not authority.source_revoked(source)
    store.close()
