"""Plan 04 / task 7: the signal channel is version-gated and fail-closed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import (
    SignalJournalSink,
    WhatsAppSignalMapper,
    signal_event_id,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import (
    MAX_BRIDGE_FRAME_BYTES,
    MEDIA_METADATA_FIELDS,
    PROTOCOL_VERSION,
    REPLAYABLE_EVENT_TYPES,
)

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


def _channel(store: ProcessingStore) -> WhatsAppChannel:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: T0))
    return channel


def _frame(kind: str, payload: dict, *, version: int = PROTOCOL_VERSION) -> str:
    provider_id = payload.get("messageId") or payload.get("targetMessageId") or kind
    identity = str(provider_id)
    return json.dumps(
        {
            "version": version,
            "type": kind,
            "ts": T0,
            "accountId": "a",
            "eventId": f"test-event:{kind}:{identity}",
            "eventKey": f"test-key:{kind}:{identity}",
            "observedAt": T0,
            "payload": payload,
        }
    )


def test_protocol_is_v5_and_gateway_rejects_older_frames(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)

    asyncio.run(channel._handle_bridge_message(_frame("reaction", {
        "chatJid": CHAT, "targetMessageId": "3EB0", "senderId": "4915@s.whatsapp.net",
        "emoji": "🔥", "timestamp": 1_700_000_000,
    }, version=3)))

    assert PROTOCOL_VERSION == 5
    # A v3 frame is dropped instead of being half understood.
    assert store.count_events() == 0
    store.close()


def test_v5_payload_constants_match_supported_event_and_media_shapes() -> None:
    assert REPLAYABLE_EVENT_TYPES == frozenset({"message", "edit", "delete", "reaction", "receipt"})
    assert MAX_BRIDGE_FRAME_BYTES == 262_144
    assert MEDIA_METADATA_FIELDS == frozenset(
        {"kind", "mimeType", "fileName", "bytes", "path", "ref", "sha256", "hash"}
    )


def test_long_message_and_media_metadata_are_preserved_without_binary_payload() -> None:
    text = "  " + ("x" * 8_001) + " \n"
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "long-media-1",
            "senderId": "4915@s.whatsapp.net",
            "text": text,
            "media": {
                "kind": "document",
                "mimeType": "application/pdf",
                "fileName": "report.pdf",
                "bytes": 128,
                "path": "/safe/media/report.pdf",
                "sha256": "a" * 64,
                "data": b"must-not-cross-wire",
            },
        },
        kind="message",
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["text"] == text
    assert body["media"] == {
        "kind": "document",
        "mimeType": "application/pdf",
        "fileName": "report.pdf",
        "bytes": 128,
        "path": "/safe/media/report.pdf",
        "sha256": "a" * 64,
    }
    assert "data" not in body["media"]


def test_message_relation_and_capture_identity_are_not_invented() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "reply-1",
            "senderId": "4915@s.whatsapp.net",
            "text": "  exact text  ",
            "replyToMessageId": "original-1",
            "replyToText": "quoted context",
        },
        kind="message",
        event_id="bridge-event-1",
        event_key="bridge-key-1",
        account="account-a",
        observed_at_ms=T0,
    )

    assert signal is not None
    assert signal.event_id == "bridge-event-1"
    assert signal.event_key == "bridge-key-1"
    assert signal.account == "account-a"
    body = signal.to_event_payload()
    assert body["text"] == "  exact text  "
    assert body["reply_to_message_id"] == "original-1"
    assert body["reply_to_text"] == "quoted context"


def test_provider_occurrence_bridge_observation_and_gateway_commit_stay_distinct(
    tmp_path: Path,
) -> None:
    gateway_commit_ms = T0 + 500
    bridge_observation_ms = T0 + 250
    provider_occurrence_seconds = 1_700_000_123
    store = ProcessingStore(tmp_path / "p.db")
    sink = SignalJournalSink(store, clock=lambda: gateway_commit_ms)

    event_id = sink.capture(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "three-times-1",
            "senderId": "4915@s.whatsapp.net",
            "text": "provider text",
            "timestamp": provider_occurrence_seconds,
        },
        event_id="bridge-event-three-times",
        event_key="bridge-key-three-times",
        account="account-a",
        observed_at_ms=bridge_observation_ms,
        strict=True,
    )

    assert event_id == "bridge-event-three-times"
    event = store.get_event(event_id)
    assert event is not None
    assert event.occurred_ms == provider_occurrence_seconds * 1000
    assert event.created_ms == gateway_commit_ms
    assert event.payload is not None
    assert event.payload["observed_at_ms"] == bridge_observation_ms
    store.close()


def test_media_hash_mapping_keeps_only_valid_sha256_and_uses_first_valid_candidate() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "hash-1",
            "text": "[Document]",
            "media": {
                "kind": "document",
                "sha256": "invalid",
                "hash": "AB" * 32,
                "fileSha256": b"raw-provider-bytes",
            },
        },
        kind="message",
    )

    assert signal is not None
    assert signal.to_event_payload()["media"]["sha256"] == "ab" * 32


def test_edit_preserves_replacement_text_and_revision() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "edited-1",
            "text": "replacement text",
            "revision": 3,
            "timestamp": 1_700_000_123,
        },
        kind="edit",
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["text"] == "replacement text"
    assert body["revision"] == 3


def test_edit_revision_is_normalized_in_payload_and_signal_metadata() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "edited-normalized",
            "text": "replacement",
            "revision": "3",
        },
        kind="edit",
    )

    assert signal is not None
    assert signal.revision == 3
    assert signal.to_event_payload()["revision"] == 3


@pytest.mark.parametrize("revision", [True, 0, -1, "bad", 1.5])
def test_strict_signal_capture_rejects_invalid_revision(tmp_path: Path, revision: object) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    sink = SignalJournalSink(store)

    with pytest.raises(ValueError):
        sink.capture(
            "edit",
            {
                "chatJid": CHAT,
                "messageId": "edited-invalid",
                "text": "replacement",
                "revision": revision,
            },
            event_id="event-invalid",
            event_key="key-invalid",
            strict=True,
        )
    assert store.count_events() == 0
    store.close()


@pytest.mark.parametrize("field", ["event_id", "event_key"])
def test_strict_signal_capture_rejects_missing_or_empty_root_identity(
    tmp_path: Path, field: str
) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    sink = SignalJournalSink(store)
    identity = {"event_id": "event-1", "event_key": "key-1"}
    identity[field] = ""

    with pytest.raises(ValueError):
        sink.capture(
            "message",
            {
                "chatJid": CHAT,
                "messageId": "message-invalid",
                "senderId": "4915@s.whatsapp.net",
                "text": "hello",
            },
            event_id=identity["event_id"],
            event_key=identity["event_key"],
            strict=True,
        )
    assert store.count_events() == 0
    store.close()


def test_delete_reaction_and_receipt_keep_provider_references() -> None:
    mapper = WhatsAppSignalMapper()
    delete = mapper.map({"chatJid": CHAT, "messageId": "deleted-1"}, kind="delete")
    reaction = mapper.map(
        {
            "chatJid": CHAT,
            "targetMessageId": "reacted-1",
            "senderId": "4915@s.whatsapp.net",
            "emoji": "👍",
        },
        kind="reaction",
    )
    receipt = mapper.map(
        {
            "chatJid": CHAT,
            "messageId": "received-1",
            "recipientJid": "4915@s.whatsapp.net",
            "status": "read",
        },
        kind="receipt",
    )

    assert delete is not None and delete.source_message_id == "deleted-1"
    assert reaction is not None and reaction.target_message_id == "reacted-1"
    assert receipt is not None and receipt.source_message_id == "received-1"
    assert delete.to_event_payload()["source_message_id"] == "deleted-1"
    assert reaction.to_event_payload()["target_message_id"] == "reacted-1"
    assert receipt.to_event_payload()["source_message_id"] == "received-1"


def test_every_signal_kind_reaches_the_journal(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)
    frames = {
        "edit": {"chatJid": CHAT, "messageId": "3EB1", "text": "new", "timestamp": 1_700_000_000},
        "delete": {"chatJid": CHAT, "messageId": "3EB2"},
        "reaction": {
            "chatJid": CHAT, "targetMessageId": "3EB3", "senderId": "4915@s.whatsapp.net",
            "emoji": "👍", "timestamp": 1_700_000_000,
        },
        "receipt": {
            "chatJid": CHAT, "messageId": "3EB4", "recipientJid": "4915@s.whatsapp.net",
            "status": "read",
        },
    }

    for kind, payload in frames.items():
        asyncio.run(channel._handle_bridge_message(_frame(kind, payload)))

    assert store.count_events() == 4
    kinds = sorted(
        event.kind for event in (store.get_event(signal_event_id(key)) for key in [])
        if event is not None
    )
    assert kinds == []  # no ids invented here; the rows are asserted below
    stored = [store.get_event(event_id) for event_id in _event_ids(store)]
    assert sorted(event.kind for event in stored if event is not None) == [
        "delete", "edit", "reaction", "receipt",
    ]
    # A signal is evidence, never an order.
    assert store.get_lineage("whatsapp:" + CHAT + ":x").decisions == ()
    store.close()


def _event_ids(store: ProcessingStore) -> list[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        return [row[0] for row in con.execute("select event_id from events")]
    finally:
        con.close()


def test_a_message_frame_is_captured_before_the_legacy_path(tmp_path: Path) -> None:
    """Canonical capture precedes the existing message projection."""
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)
    published: list[str] = []

    async def _fake_publish(event):
        published.append(event.message_id)

    channel._enrich_media_event = lambda event: _passthrough(event)  # type: ignore[method-assign]
    channel._publish_event = _fake_publish  # type: ignore[method-assign]

    async def _ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del payload, timeout_seconds, kwargs
        assert command_type == "ack_event"
        return {"acknowledged": True}

    channel._send_command = _ack  # type: ignore[method-assign]

    async def _deliver() -> None:
        await channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "3EB9",
                    "senderId": "4915",
                    "text": "hello",
                    "timestamp": 1_700_000_000,
                },
            )
        )
        tasks = tuple(channel._inbound_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    asyncio.run(_deliver())

    assert published == ["3EB9"]
    assert store.count_events() == 1
    store.close()


async def _passthrough(event):
    return event


# ── platform identity carried to the identity middleware ─────────────────────
#
# A channel adapter is the only component that may claim a platform mapping.  These
# tests pin what it hands on: the canonical event id, the account namespace, the phone
# JID and the LID as separate typed identifiers, the provider's mapping verdict, and the
# push name as plain text.  A bare sender digit is never promoted to a phone JID.


def _to_inbound(channel: WhatsAppChannel, payload: dict):
    event = channel._parse_inbound_event(payload)  # noqa: SLF001 - adapter projection
    assert event is not None
    return event


def test_a_phone_and_lid_pair_travels_as_two_typed_identifiers() -> None:
    channel = _channel(ProcessingStore(Path("/tmp") / "signal-identity.db"))
    event = _to_inbound(
        channel,
        {
            "messageId": "m-1",
            "chatJid": CHAT,
            "senderId": "491111111111",
            "senderPhoneJid": "491111111111@s.whatsapp.net",
            "participantJid": "99999999999999@lid",
            "senderName": "Synthetic Push",
            "timestamp": T0,
            "text": "synthetic",
        },
    )
    assert event.sender_phone_jid == "491111111111@s.whatsapp.net"
    assert event.participant_jid == "99999999999999@lid"
    assert event.sender_name == "Synthetic Push"
    # The LID and the phone JID stay distinguishable: the LID digits are never used as
    # the phone number and the phone number is never used as a LID.
    assert "99999999999999" not in (event.sender_phone_jid or "")
    assert "@lid" not in (event.sender_phone_jid or "")


def test_a_bare_sender_number_does_not_become_a_phone_jid() -> None:
    channel = _channel(ProcessingStore(Path("/tmp") / "signal-identity-bare.db"))
    event = _to_inbound(
        channel,
        {
            "messageId": "m-2",
            "chatJid": CHAT,
            "senderId": "491111111111",
            "timestamp": T0,
            "text": "synthetic",
        },
    )
    # No phone JID was issued by the bridge, so the adapter does not invent one.
    assert event.sender_phone_jid is None
    assert event.sender_id == "491111111111"


def test_a_provider_mapping_conflict_is_preserved_for_the_identity_path() -> None:
    channel = _channel(ProcessingStore(Path("/tmp") / "signal-identity-conflict.db"))
    event = _to_inbound(
        channel,
        {
            "messageId": "m-3",
            "chatJid": CHAT,
            "senderId": "491111111111",
            "senderPhoneJid": "491111111111@s.whatsapp.net",
            "participantJid": "99999999999999@lid",
            "lidConflict": True,
            "timestamp": T0,
            "text": "synthetic",
        },
    )
    assert event.lid_conflict is True


def test_the_inbound_metadata_carries_the_identity_inputs(tmp_path: Path) -> None:
    """The middleware reads these keys; a rename here must fail loudly."""
    source = (
        Path(__file__).resolve().parents[2]
        / "packages/gateway/yeoman_gateway/channels/whatsapp.py"
    ).read_text(encoding="utf-8")
    for key in (
        '"sender_phone_jid": event.sender_phone_jid',
        '"participant_lid": event.participant_jid if event.sender_phone_jid else None',
        '"lid_conflict": event.lid_conflict',
        '"sender_name": event.sender_name',
    ):
        assert key in source, key
