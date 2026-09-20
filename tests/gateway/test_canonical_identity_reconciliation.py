"""Canonical identity: one physical message is one event, not two sources.

The Bridge signal sink and the policy gate both journal WhatsApp messages into the same
canonical log.  The Bridge row is the canonical identity; the gate adopts it instead of
appending a parallel event.  Both writers keep working, old rows are never deleted, and
the two rows are never counted as independent evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.processing.models import CANONICAL_WHATSAPP_ORIGIN
from yeoman_gateway.processing.policy import IngestGate, IngestRequest
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

NOW = 1_700_000_000_000
CHAT = "120363407395534152@g.us"
MESSAGE_ID = "3EB085F7C92D46EA269D5F"
BRIDGE_KEY = f"whatsapp:default:120363407395534152%40g.us:message:{MESSAGE_ID}"
BRIDGE_EVENT_ID = "wa_5b5b275589d3ec227122bcebe39a023a"


class _Snapshots:
    def snapshot(self) -> Any:
        from yeoman_gateway.processing.models import PolicySnapshot

        return PolicySnapshot(version="v1", policy_hash="h1", loaded_ms=NOW)


class _Decision:
    accept_message = True
    should_respond = True


class _RecordingThreads:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def assign(self, event: Any, *, now_ms: int, allow_turn: bool) -> None:
        self.events.append(event)
        return None


def _config() -> Any:
    from yeoman_shared.config.schema import ProcessingConfig

    return ProcessingConfig.model_validate({"enabled": True, "chats": [f"whatsapp:{CHAT}"]})


def _gate(store: ProcessingStore, threads: Any = None) -> IngestGate:
    return IngestGate(
        config=_config(),
        store=store,
        snapshots=_Snapshots(),
        evaluate=lambda request: _Decision(),
        threads=threads,
    )


def _request() -> IngestRequest:
    return IngestRequest(
        event_key=f"whatsapp:{CHAT}:{MESSAGE_ID}",
        event_id=MESSAGE_ID,
        trace_id=f"whatsapp:{CHAT}:{MESSAGE_ID}",
        event=InboundEvent(
            channel="whatsapp",
            chat_id=CHAT,
            sender_id="491757070305",
            content="/new",
            message_id=MESSAGE_ID,
            timestamp=datetime.fromtimestamp(NOW / 1000, tz=UTC),
            is_group=True,
        ),
        payload_extra={"origin": "whatsapp_bridge"},
    )


def _bridge_sink(store: ProcessingStore) -> SignalJournalSink:
    return SignalJournalSink(store, clock=lambda: NOW)


def _capture_bridge_message(store: ProcessingStore) -> str:
    stored = _bridge_sink(store).capture(
        "message",
        {
            "chatJid": CHAT,
            "messageId": MESSAGE_ID,
            "senderId": "34596062240904",
            "text": "/new",
            "isGroup": True,
            "timestamp": NOW,
        },
        event_id=BRIDGE_EVENT_ID,
        event_key=BRIDGE_KEY,
        account="default",
        observed_at_ms=NOW,
        strict=True,
    )
    assert stored == BRIDGE_EVENT_ID
    return stored


def test_gate_adopts_the_bridge_event_instead_of_writing_a_second_row(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        canonical_id = _capture_bridge_message(store)
        gate = _gate(store)

        journaled = gate._journal(_request(), now=NOW)  # noqa: SLF001 - identity assertion

        assert journaled == canonical_id
        rows = store.events_by_provider_identity(
            channel="whatsapp", chat_id=CHAT, provider_message_id=MESSAGE_ID
        )
        assert len(rows) == 1
        assert rows[0].event_id == canonical_id
        assert rows[0].origin == CANONICAL_WHATSAPP_ORIGIN
        assert store.count_events() == 1
    finally:
        store.close()


def test_forms_that_have_no_bridge_row_keep_their_own_identity(tmp_path: Path) -> None:
    """Only a real Bridge row is adopted; every other channel is unaffected."""
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        gate = _gate(store)
        journaled = gate._journal(_request(), now=NOW)  # noqa: SLF001 - identity assertion
        assert journaled == MESSAGE_ID
        event = store.get_event(MESSAGE_ID)
        assert event is not None and event.event_key == f"whatsapp:{CHAT}:{MESSAGE_ID}"
        assert store.count_events() == 1
    finally:
        store.close()


def test_routing_attaches_thread_links_to_the_canonical_identity(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        canonical_id = _capture_bridge_message(store)
        threads = _RecordingThreads()
        gate = _gate(store, threads=threads)

        gate._assign(_request(), now=NOW, allow_turn=False)  # noqa: SLF001 - routing assertion

        assert [event.event_id for event in threads.events] == [canonical_id]
    finally:
        store.close()


def test_canonical_lookup_ignores_foreign_origins_and_outbound_rows(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        store.append_event(
            event_key=f"whatsapp:{CHAT}:{MESSAGE_ID}",
            event_id=MESSAGE_ID,
            trace_id="legacy",
            payload={
                "kind": "message",
                "origin": "whatsapp_bridge",
                "channel": "whatsapp",
                "chat_id": CHAT,
                "text": "legacy",
            },
            now_ms=NOW,
        )
        assert (
            store.canonical_event_for_provider_message(
                channel="whatsapp", chat_id=CHAT, provider_message_id=MESSAGE_ID
            )
            is None
        )

        canonical_id = _capture_bridge_message(store)
        assert (
            store.canonical_event_for_provider_message(
                channel="whatsapp", chat_id=CHAT, provider_message_id=MESSAGE_ID
            ).event_id
            == canonical_id
        )
        assert (
            store.canonical_event_for_provider_message(
                channel="whatsapp",
                chat_id=CHAT,
                provider_message_id=MESSAGE_ID,
                direction="out",
            )
            is None
        )
    finally:
        store.close()
