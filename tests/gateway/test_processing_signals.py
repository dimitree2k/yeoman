"""Plan 04 / R01 signals: provider events map deterministically into the journal."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.processing.models import JournalConflictError
from yeoman_gateway.processing.signals import (
    SignalJournalSink,
    WhatsAppSignalMapper,
    signal_event_id,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


def _mapper() -> WhatsAppSignalMapper:
    return WhatsAppSignalMapper()


def _append(db: ProcessingStore, signal, now_ms: int = T0) -> str:
    return db.append_event(
        event_key=signal.event_key,
        event_id=signal.event_id,
        trace_id=signal.trace_id,
        payload=signal.to_event_payload(),
        now_ms=now_ms,
    )


def test_event_id_is_the_provider_identity() -> None:
    signal = _mapper().map({"chatJid": CHAT, "messageId": "3EB0", "text": "hi"}, kind="message")
    assert signal is not None
    assert signal.event_key == f"whatsapp:{CHAT}:message:3EB0"
    assert signal.event_id == signal_event_id(signal.event_key)
    assert len(signal.event_id) == 32


def test_identical_signal_twice_is_one_row(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    payload = {"chatJid": CHAT, "messageId": "3EB0", "text": "hi"}
    first = _mapper().map(payload, kind="message")
    second = _mapper().map(payload, kind="message")

    assert _append(db, first) == _append(db, second) == first.event_id
    assert db.count_events() == 1
    db.close()


def test_same_identity_with_other_content_is_a_conflict(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    mapper = _mapper()
    _append(db, mapper.map({"chatJid": CHAT, "messageId": "3EB0", "text": "one"}, kind="message"))

    with pytest.raises(JournalConflictError):
        _append(db, mapper.map({"chatJid": CHAT, "messageId": "3EB0", "text": "two"}, kind="message"))
    db.close()


def test_reaction_without_a_target_stays_unresolved(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    signal = _mapper().map(
        {"chatJid": CHAT, "senderId": "4915111@s.whatsapp.net", "emoji": "👍", "timestamp": 1},
        kind="reaction",
    )

    assert signal is not None
    assert signal.target_message_id is None  # nothing was invented
    _append(db, signal)

    unresolved = db.unresolved_relations()
    assert [(rel.event_id, rel.ref_id, rel.resolved) for rel in unresolved] == []
    stored = db.get_event(signal.event_id)
    assert stored is not None and stored.kind == "reaction"
    db.close()


def test_reaction_with_a_target_links_to_it(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    mapper = _mapper()
    _append(db, mapper.map({"chatJid": CHAT, "messageId": "3EB0", "text": "hi"}, kind="message"))
    signal = mapper.map(
        {
            "chatJid": CHAT,
            "targetMessageId": "3EB0",
            "senderId": "4915111@s.whatsapp.net",
            "emoji": "🔥",
            "timestamp": 2,
        },
        kind="reaction",
    )
    _append(db, signal, now_ms=T0 + 1000)

    assert signal.target_message_id == "3EB0"
    assert db.unresolved_relations() == ()  # the source event resolves the relation
    db.close()


def test_delete_and_edit_are_distinct_events(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    mapper = _mapper()
    deleted = mapper.map({"chatJid": CHAT, "messageId": "3EB0"}, kind="delete")
    edited = mapper.map(
        {"chatJid": CHAT, "messageId": "3EB0", "timestamp": 1_700_000_000}, kind="edit"
    )

    assert deleted.event_key.endswith(":delete:3EB0")
    assert edited.event_key.endswith(":edit:3EB0:1700000000000")
    assert deleted.event_id != edited.event_id
    db.close()


def test_receipt_creates_no_message_event_and_no_decision(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    signal = _mapper().map(
        {"chatJid": CHAT, "messageId": "3EB0", "recipientJid": "4915111@s.whatsapp.net",
         "status": "read"},
        kind="receipt",
    )

    _append(db, signal)

    assert db.count_events() == 1
    stored = db.get_event(signal.event_id)
    assert stored is not None and stored.kind == "receipt"
    # A receipt is evidence, not an order: it must not create a policy decision.
    assert db.get_lineage(signal.trace_id).decisions == ()
    db.close()


def test_membership_change_maps_to_canonical_event() -> None:
    signal = _mapper().map(
        {
            "chatJid": CHAT,
            "action": "add",
            "changeId": "stub:message-7",
            "sourceCopyId": "stub:message-7",
            "messageId": "message-7",
            "stubType": 27,
            "providerTimestampMs": T0,
            "participants": [
                {"lid": "123@lid", "phoneJid": "49123@s.whatsapp.net"},
                {"lid": "456@lid", "phoneJid": "49456@s.whatsapp.net"},
            ],
            "actor": {"lid": "456@lid", "phoneJid": "49456@s.whatsapp.net"},
        },
        kind="membership_change",
        event_key="whatsapp:account-a:chat@g.us:membership_change:stub%3Amessage-7",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["kind"] == "membership_change"
    assert body["chat_id"] == CHAT
    assert body["account"] == "account-a"
    assert body["action"] == "add"
    assert body["participants"] == [{"lid": "123@lid"}, {"lid": "456@lid"}]
    assert "actor" not in body
    assert not {"sourceCopyId", "messageId", "stubType", "providerTimestampMs"} & body.keys()
    assert body["occurred_ms"] is None and "observed_at_ms" not in body


def test_membership_snapshot_maps_to_canonical_event() -> None:
    signal = _mapper().map(
        {
            "chatJid": CHAT,
            "snapshotAtMs": T0,
            "complete": True,
            "memberCount": 1,
            "participants": [
                {"lid": "123@lid", "phoneJid": "49123@s.whatsapp.net", "admin": True}
            ],
        },
        kind="membership_snapshot",
        event_key="whatsapp:account-a:chat@g.us:membership_snapshot:1700000000000",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["kind"] == "membership_snapshot"
    assert body["chat_id"] == CHAT
    assert body["snapshot_at_ms"] == T0
    assert body["complete"] is True and body["member_count"] == 1
    assert body["participants"] == [
        {"lid": "123@lid", "phone_jid": "49123@s.whatsapp.net", "admin": True}
    ]


def test_membership_change_source_copies_append_one_event(tmp_path: Path) -> None:
    db = ProcessingStore(tmp_path / "p.db")
    mapper = _mapper()
    stable = {
        "chatJid": CHAT,
        "action": "add",
        "changeId": "stub:message-7",
        "participants": [
            {"lid": "123@lid", "phoneJid": "49123@s.whatsapp.net"},
            {"lid": "456@lid", "phoneJid": "49456@s.whatsapp.net"},
        ],
        "actor": {"lid": "456@lid"},
    }
    stub = mapper.map(
        {
            **stable,
            "sourceCopyId": "stub:message-7",
            "messageId": "message-7",
            "stubType": 27,
            "providerTimestampMs": T0,
        },
        kind="membership_change",
        event_id="stub-event",
        event_key="shared-membership-key",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )
    update = mapper.map(
        {
            **stable,
            "participants": [{"lid": "456@lid"}, {"lid": "123@lid"}],
            "actor": None,
            "sourceCopyId": "update:copy-8",
            "providerTimestampMs": None,
        },
        kind="membership_change",
        event_id="update-event",
        event_key="shared-membership-key",
        account="account-a",
        observed_at_ms=T0 + 5,
        strict=True,
    )

    assert stub is not None and update is not None
    assert stub.to_event_payload() == update.to_event_payload()
    assert _append(db, stub) == _append(db, update) == "stub-event"
    assert db.count_events() == 1
    db.close()


def test_membership_snapshot_requires_boolean_admin_metadata() -> None:
    mapper = _mapper()
    base = {
        "chatJid": CHAT,
        "snapshotAtMs": T0,
        "complete": True,
        "memberCount": 1,
        "participants": [{"lid": "123@lid"}],
    }

    with pytest.raises(ValueError, match="malformed WhatsApp membership_snapshot"):
        mapper.map(base, kind="membership_snapshot", strict=True)
    with pytest.raises(ValueError, match="malformed WhatsApp membership_snapshot"):
        mapper.map(
            {**base, "participants": [{"lid": "123@lid", "admin": "unknown"}]},
            kind="membership_snapshot",
            strict=True,
        )


def test_membership_change_copy_requires_available_exact_payload(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace

    db = ProcessingStore(tmp_path / "p.db")
    sink = SignalJournalSink(db, clock=lambda: T0)
    base = {
        "chatJid": CHAT,
        "action": "add",
        "changeId": "stub:message-11",
        "participants": [{"lid": "123@lid"}],
    }
    sink.capture(
        "membership_change",
        {**base, "sourceCopyId": "stub:message-11"},
        event_id="stub-event-11",
        event_key="shared-membership-key-11",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )
    stored = db.get_event("stub-event-11")
    assert stored is not None
    purged = replace(stored, payload=None)
    monkeypatch.setattr(db, "get_event", lambda event_id: purged)

    with pytest.raises(ValueError, match="conflicting event identity"):
        sink.capture(
            "membership_change",
            {**base, "sourceCopyId": "update:copy-11"},
            event_id="update-event-11",
            event_key="shared-membership-key-11",
            account="account-a",
            observed_at_ms=T0 + 1,
            strict=True,
        )
    db.close()


def test_membership_same_id_replay_requires_available_exact_payload(
    tmp_path: Path, monkeypatch
) -> None:
    from dataclasses import replace

    db = ProcessingStore(tmp_path / "p.db")
    sink = SignalJournalSink(db, clock=lambda: T0)
    payload = {
        "chatJid": CHAT,
        "action": "add",
        "changeId": "stub:message-12",
        "participants": [{"lid": "123@lid"}],
        "sourceCopyId": "stub:message-12",
    }
    sink.capture(
        "membership_change",
        payload,
        event_id="same-event-12",
        event_key="shared-membership-key-12",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )
    stored = db.get_event("same-event-12")
    assert stored is not None
    purged = replace(stored, payload=None)
    monkeypatch.setattr(db, "get_event", lambda event_id: purged)
    # A payload-purged ProcessingStore row accepts the provider identity without
    # comparing content; model that store result while testing the strict sink guard.
    monkeypatch.setattr(db, "append_event", lambda **kwargs: "same-event-12")

    with pytest.raises(ValueError, match="conflicting event identity"):
        sink.capture(
            "membership_change",
            {**payload, "action": "remove"},
            event_id="same-event-12",
            event_key="shared-membership-key-12",
            account="account-a",
            observed_at_ms=T0 + 1,
            strict=True,
        )
    db.close()


def test_receipt_recipient_is_stored_hashed() -> None:
    signal = _mapper().map(
        {"chatJid": CHAT, "messageId": "3EB0", "recipientJid": "4915111@s.whatsapp.net",
         "status": "delivered"},
        kind="receipt",
    )
    body = signal.to_event_payload()

    assert "4915111" not in str(body["recipient_token"])
    assert body["recipient_token"] != "4915111"
    assert len(body["recipient_token"]) == 12
    assert body["status"] == "delivered"


def test_malformed_payloads_are_rejected_not_guessed() -> None:
    mapper = _mapper()
    assert mapper.map({}, kind="message") is None
    assert mapper.map({"chatJid": CHAT}, kind="message") is None
    assert mapper.map({"messageId": "3EB0"}, kind="delete") is None
    with pytest.raises(ValueError):
        mapper.map({"chatJid": CHAT, "messageId": "3EB0"}, kind="typing")
    malformed_membership = {
        "chatJid": CHAT,
        "action": "add",
        "changeId": "change-1",
        "participants": [{"lid": "not-a-lid"}],
    }
    assert mapper.map(malformed_membership, kind="membership_change") is None
    with pytest.raises(ValueError, match="malformed WhatsApp membership_change"):
        mapper.map(malformed_membership, kind="membership_change", strict=True)


def test_channel_hook_journals_signals_without_touching_ingest(tmp_path: Path) -> None:
    """A signal frame reaches the journal; a broken sink never costs a message."""
    import json

    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_shared.config.schema import WhatsAppConfig

    store = ProcessingStore(tmp_path / "p.db")
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: T0))

    event_key = f"whatsapp:{CHAT}:reaction:3EB0:4915:🔥:1700000000000"
    event_id = signal_event_id(event_key)
    frame = json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": "reaction",
            "accountId": "account-a",
            "eventId": event_id,
            "eventKey": event_key,
            "observedAt": T0,
            "payload": {"chatJid": CHAT, "targetMessageId": "3EB0", "senderId": "4915@s.whatsapp.net",
                        "emoji": "🔥", "timestamp": 1_700_000_000},
        }
    )
    import asyncio

    asyncio.run(channel._handle_bridge_message(frame))

    assert store.count_events() == 1
    stored = store.get_event(event_id)
    assert stored is not None and stored.kind == "reaction"

    class _Boom:
        def __call__(self, kind, payload):
            raise RuntimeError("sink down")

    channel.set_processing_signals(_Boom())
    asyncio.run(channel._handle_bridge_message(frame))  # logged, not raised

    channel.set_processing_signals(None)
    asyncio.run(channel._handle_bridge_message(frame))  # no sink: silently ignored
    store.close()
