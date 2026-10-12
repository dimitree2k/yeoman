"""Native source registration: prove audiences from projected history, never a registry."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

from yeoman_gateway.history.context import history_knowledge_scope
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge._capture import ObservedSourceRegistrar
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.history_test_helpers import (
    ProjectedHistory,
    prepare_history_retirement_knowledge,
)

NOW = 1_700_000_000_000
GROUP = "491786127564-1611913127@g.us"
DIRECT = "491757070305@s.whatsapp.net"
SENDER = "491757070305"
OTHER = "491511"


class _Runtime:
    """A Knowledge service and registrar under the real native-history scope."""

    def __init__(self, tmp_path: Path, *, history_members: tuple[str, ...] = ()) -> None:
        self.store = ProcessingStore(tmp_path / "processing.db")
        self.history = ProjectedHistory(tmp_path / "projected-history")
        async def read_turn():
            return self.history.snapshot()
        self.history.read_turn = read_turn
        self.members = tuple(history_members)
        engine = PolicyEngine(PolicyConfig.model_validate({
            "defaults": {"whoCanTalk": {"mode": "everyone"}},
            "channels": {"whatsapp": {"chats": {GROUP: {}}}},
        }), tmp_path)
        self.policy = RuntimeKnowledgePolicy(engine=engine, policy_revision=1)
        self.sources = RuntimeKnowledgeSources(processing_store=self.store)
        self.knowledge = open_knowledge_store(
            prepare_history_retirement_knowledge(tmp_path, "knowledge"),
            workspace_id=workspace_id_for(tmp_path),
            source_authority=self.sources,
            policy_authority=self.policy,
            clock=lambda: NOW,

        )
        self.registrar = ObservedSourceRegistrar(knowledge=self.knowledge, processing=self.store, clock=lambda: NOW)
        self.registration_results: dict[str, bool] = {}
        self.sink = SignalJournalSink(self.store, clock=lambda: NOW, sources=self.registrar)
        self.sink._history_projector = self.history
        self.sink._history_knowledge = self.knowledge

    @contextmanager
    def scope(self):
        snapshot = self.history.snapshot()
        try:
            with history_knowledge_scope(snapshot, self.knowledge):
                yield snapshot
        finally:
            snapshot.close()

    def project_message(self, *, chat_id: str, message_id: str, occurred_ms: int = NOW) -> None:
        if chat_id.endswith("@g.us") and self.members:
            self.history.add_group(
                chat_id=chat_id,
                participants=[f"{member}@s.whatsapp.net" for member in self.members],
                timestamp=occurred_ms,
            )
        identifiers = {SENDER, *self.members}
        for member in identifiers:
            self.history.add_contact(
                identifiers=[f"{member}@s.whatsapp.net"], name=f"Synthetic {member}", timestamp=occurred_ms
            )
        self.history.add_message(
            chat_id=chat_id, message_id=message_id, text="Wir treffen uns am Freitag um acht.",
            timestamp=occurred_ms, sender_id=f"{SENDER}@s.whatsapp.net",
        )

    def close(self) -> None:
        self.history.close()
        self.knowledge.close()
        self.store.close()


def _append_message(
    runtime: _Runtime, *, chat_id: str, message_id: str, occurred_ms: int = NOW
) -> str:
    runtime.project_message(chat_id=chat_id, message_id=message_id, occurred_ms=occurred_ms)
    payload = {
        "chatJid": chat_id, "messageId": message_id, "senderId": SENDER,
        "text": "Wir treffen uns am Freitag um acht.",
        "isGroup": chat_id.endswith("@g.us"), "timestamp": occurred_ms,
    }
    stored = runtime.sink.capture(
        "message", payload, event_id=f"wa_{message_id}",
        event_key=f"whatsapp:default:{chat_id}:message:{message_id}", account="default",
        observed_at_ms=NOW, strict=True,
    )
    runtime.registration_results[stored] = asyncio.run(
        runtime.sink.register_observed_source_live(stored)
    )
    assert stored == f"wa_{message_id}"
    return stored


def _source(runtime: _Runtime, chat_id: str, native_id: str, revision: int = 1):
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources

    with runtime.scope() as snapshot:
        queries = HistoryQueries(snapshot)
        authority = HistoryKnowledgeSources(
            queries, {}, runtime.knowledge.history_source_ledger,
            runtime.knowledge._legacy_authority,
        )
        native = queries.native_message(chat_id=chat_id, native_id=native_id)
        assert native is not None
        source = authority.verify_source_ref(native["message_id"], revision)
        audience = (
            authority.evidence_audience(source, basis="observed_source_batch")
            if source is not None else None
        )
        return source, audience


def test_group_observation_registers_proven_native_audience_without_a_turn(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER, OTHER))
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m1")
        source, audience = _source(runtime, GROUP, "m1")
        assert source is not None and source.author_principal == "whatsapp:491757070305"
        assert audience is not None and audience.status == "known"
        assert set(audience.members) == {"whatsapp:491757070305", "whatsapp:491511"}
        assert runtime.registration_results[event_id] is True
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
        assert runtime.store.count_events() == 1
    finally:
        runtime.close()


def test_direct_observation_is_proven_author_only(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        _append_message(runtime, chat_id=DIRECT, message_id="m2")
        source, audience = _source(runtime, DIRECT, "m2")
        assert source is not None
        assert audience is not None and audience.status == "author_only"
        assert runtime.store.get_event_source_authority("wa_m2")["audience_status"] == "unknown"
    finally:
        runtime.close()


def test_group_without_native_membership_stays_durable_and_unknown(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m3")
        assert runtime.store.get_event(event_id) is not None
        source, _ = _source(runtime, GROUP, "m3")
        assert source is None
        assert runtime.registration_results[event_id] is False
        assert runtime.store.get_event_source_authority(event_id)["audience_status"] == "unknown"
    finally:
        runtime.close()


def test_registration_uses_native_event_time_and_canonical_source(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER,))
    try:
        occurred_ms = NOW - 60_000
        _append_message(runtime, chat_id=GROUP, message_id="m4", occurred_ms=occurred_ms)
        issued, _ = _source(runtime, GROUP, "m4")
        assert issued is not None and issued.occurred_at_ms == occurred_ms
        assert issued.author_principal == "whatsapp:491757070305"
        assert runtime.store.count_events() == 1
    finally:
        runtime.close()


def test_unknown_event_is_never_registered(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, history_members=(SENDER,))
    try:
        assert asyncio.run(runtime.sink.register_observed_source_live("does-not-exist")) is False
    finally:
        runtime.close()
