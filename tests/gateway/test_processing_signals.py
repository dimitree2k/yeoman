"""Plan 04 / R01 signals: provider events map deterministically into the journal."""
# ruff: noqa: F811

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create as create_history
from yeoman_gateway.knowledge._capture import ObservedSourceRegistrar
from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "event_id"),
    [
        ("synthetic@g.us", "bridge-event-group"),
        ("synthetic@g.us", "policy-ingest-group"),
        ("10001@s.whatsapp.net", "bridge-event-dm"),
        ("10001@s.whatsapp.net", "policy-ingest-dm"),
    ],
)
@pytest.mark.parametrize("lookup", ["known", "missing", "ambiguous"])
async def test_observed_registration_uses_live_history_snapshot(
    tmp_path, chat_id, event_id, lookup
):
    native_id = f"native-{event_id}"
    old_knowledge_path = tmp_path / "knowledge-v2.db"
    old_store = KnowledgeStore(old_knowledge_path)
    old_store.set_meta("migration_complete", "1")
    old_store.close()
    knowledge_path = tmp_path / "knowledge-v3.db"
    upgrade_history_knowledge(source=old_knowledge_path, target=knowledge_path)
    knowledge = open_knowledge_store(
        knowledge_path,
        workspace_id="synthetic",
        source_authority=RuntimeKnowledgeSources(),
        policy_authority=RuntimeKnowledgePolicy(
            engine=None, policy_revision=1
        ),
            )
    history_path = tmp_path / "history.db"
    with sqlite3.connect(history_path) as db:
        create_history(db)
        db.execute("INSERT INTO contacts VALUES ('author','person',NULL,'Synthetic','confirmed',NULL,'[]')")
        db.execute(
            "INSERT INTO identifier_history (contact_id,channel,kind,value,strength,evidence,"
            "valid_from_ms,source_refs) VALUES ('author','whatsapp','pn_jid',?,'strong',"
            "'owner_attested',1,'[]')",
            ("10001@s.whatsapp.net",),
        )
        message_id = f"whatsapp:{chat_id}:{native_id}"
        db.execute(
            "INSERT INTO messages (message_id,channel,chat_id,native_message_id,"
            "sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,"
            "text,provenance,source_refs) VALUES (?,'whatsapp',?,?, 'author',"
            "'10001@s.whatsapp.net','native_identifier','in',1791000000000,'native',"
            "'Synthetic source','native','[]')",
            (message_id, chat_id, native_id),
        )
        if chat_id.endswith("@g.us"):
            db.execute(
                "INSERT INTO message_events (event_id,kind,channel,chat_id,actor_basis,"
                "occurred_ms,time_certainty,payload_json,provenance,source_refs) "
                "VALUES ('roster','member_snapshot','whatsapp',?,'unknown',"
                "1790999999999,'native',?,'native','[]')",
                (chat_id, json.dumps({"complete": True, "participants": [["10001@s.whatsapp.net"]]})),
            )
    with sqlite3.connect(history_path) as db:
        if lookup == "missing":
            db.execute("DELETE FROM messages")
        elif lookup == "ambiguous":
            db.execute("INSERT INTO messages (message_id,channel,chat_id,native_message_id,"
                       "sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,"
                       "time_certainty,text,provenance,source_refs) SELECT message_id || ':duplicate',"
                       "channel,chat_id,native_message_id,sender_contact_id,sender_identifier,"
                       "sender_basis,direction,sent_ms,time_certainty,text,provenance,source_refs "
                       "FROM messages")
    processing = ProcessingStore(tmp_path / "processing.db")
    registrar = ObservedSourceRegistrar(
        knowledge=knowledge, processing=processing
    )
    registration_scopes = []

    def register(event_id):
        from yeoman_gateway.knowledge._history_identity import current_history_scope

        registration_scopes.append(current_history_scope(knowledge._store) is not None)
        return registrar(event_id)

    async def read_turn():
        return HistorySnapshot(
            1,
            (),
            sqlite3.connect(f"file:{history_path}?mode=ro", uri=True),
        )

    sink = SignalJournalSink(
        processing,
        sources=register,
        history_projector=SimpleNamespace(
            read_turn=read_turn,
            health=lambda: {"status": "ready", "generation": 1},
        ),
        history_knowledge=knowledge,
    )
    sink.capture(
        "message",
        {
            "chatJid": chat_id,
            "messageId": native_id,
            "senderId": "10001@s.whatsapp.net",
            "text": "Synthetic source",
            "isGroup": chat_id.endswith("@g.us"),
            "timestamp": 1_791_000_000,
        },
        event_id=event_id,
        event_key=f"whatsapp:synthetic:{chat_id}:message:{event_id}",
        account="synthetic",
        observed_at_ms=1_791_000_000_000,
        strict=True,
    )
    assert event_id != native_id
    assert registration_scopes == []

    try:
        assert await sink.register_observed_source_live(event_id) is (lookup == "known")
        assert registration_scopes == [True]
        source = knowledge.history_source_ledger.lookup(message_id, 1)
        if lookup == "known":
            assert source is not None and source.source.event_id == message_id
        else:
            assert source is None
    finally:
        processing.close()
        knowledge.close()


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
