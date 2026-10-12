"""Canonical membership snapshots stay journaled without retired identity writes."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

ACCOUNT = "synthetic-membership-account"
CHAT = "synthetic-membership-room@g.us"
SNAPSHOT_AT = 1_780_000_000_000


def _snapshot(*participants: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    return {
        "chatJid": CHAT,
        "snapshotAtMs": SNAPSHOT_AT,
        "complete": complete,
        "memberCount": len(participants) if complete else len(participants) + 1,
        "participants": list(participants),
    }


@pytest.mark.parametrize("case", [
    "message", "edit", "reaction", "delete", "receipt", "membership_change",
    "membership_snapshot", "group_subject", "group_description",
    "observation_only", "opaque_edit",
])
def test_retired_capture_journals_all_kinds_without_identity_projection(knowledge_harness, case):
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES, _digest
    from yeoman_gateway.processing.signals import SIGNAL_KINDS

    h = knowledge_harness
    retired = _retired_knowledge(h)
    kind = "message" if case in {"observation_only", "opaque_edit"} else case
    assert kind in SIGNAL_KINDS
    payload = {"chatJid": CHAT, "messageId": "synthetic-message", "text": "synthetic text",
               "senderId": "4910000000101@s.whatsapp.net", "timestamp": SNAPSHOT_AT}
    if kind == "edit":
        payload["revision"] = 2
    elif kind == "reaction":
        payload.update(targetMessageId="synthetic-message", emoji="👍")
    elif kind == "receipt":
        payload.update(status="delivered", recipientJid="4910000000101@s.whatsapp.net")
    elif kind == "membership_change":
        payload.update(action="add", changeId="synthetic-change", participants=[{"lid": "8420000101@lid"}])
    elif kind == "membership_snapshot":
        payload = _snapshot({"phoneJid": "4910000000101@s.whatsapp.net", "lid": "8420000101@lid", "admin": False})
    elif kind in {"group_subject", "group_description"}:
        payload = {"chatJid": CHAT, "value": "synthetic metadata", "snapshot": True, "observedAtMs": SNAPSHOT_AT}
    elif case == "observation_only":
        payload.update(observationOnly=True, observationType="encrypted_message_edit_undecoded",
                       encryptedEdit={"kind": "secretEncryptedMessage", "secretEncType": 2})
    elif case == "opaque_edit":
        payload.update(observationOnly=True, observationType="encrypted_message_edit_undecoded",
                       targetMessageId="synthetic-target", encryptedEdit={"kind": "secretEncryptedMessage", "secretEncType": 2, "targetMessageId": "synthetic-target",
                       "encPayload": "AQID", "encIv": "BAUG", "keyVersion": 1})
    store = ProcessingStore(h.tmp_path / "retired-processing.db")
    sink = SignalJournalSink(store)
    try:
        with sqlite3.connect(retired) as db:
            before = _digest(db, FROZEN_IDENTITY_TABLES)
            stored = sink.capture(kind, payload, event_id=case, event_key=f"synthetic:{case}",
                                  account=ACCOUNT, observed_at_ms=SNAPSHOT_AT, strict=True)
            assert stored == case
            event = store.get_event(stored)
            assert event is not None and event.kind == kind
            assert store.count_events() == 1
            assert _digest(db, FROZEN_IDENTITY_TABLES) == before
            if case == "membership_snapshot":
                assert event.payload["participants"][0]["phone_jid"] == "4910000000101@s.whatsapp.net"
            if case == "opaque_edit":
                assert event.payload["observation_only"] is True
                assert event.payload["encrypted_edit"]["encPayload"] == "AQID"
    finally:
        store.close()


def _retired_knowledge(h):
    """Return the fixture's already-open projected-v3 database for read-only digest checks."""
    return h._raw_service.db_path  # noqa: SLF001


def test_retired_telegram_turn_skips_frozen_identity_projection(knowledge_harness):
    import asyncio
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.core.pipeline import PipelineContext
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES, _digest
    from yeoman_gateway.pipeline.contacts import ContactsMiddleware

    h = knowledge_harness
    retired = _retired_knowledge(h)
    issued = []
    progressed = []
    event = InboundEvent(channel="telegram", chat_id="synthetic-room", sender_id="10001",
                         content="Synthetic", timestamp=datetime.now(UTC), message_id="synthetic-turn")
    middleware = ContactsMiddleware()

    async def next_step(ctx):
        progressed.append(ctx.event)

    with sqlite3.connect(retired) as db:
        before = _digest(db, FROZEN_IDENTITY_TABLES)
        asyncio.run(middleware(PipelineContext(event=event), next_step))
        assert issued == []
        assert progressed == [event]
        assert _digest(db, FROZEN_IDENTITY_TABLES) == before
