"""Source registration: real canonical provenance, proven without a response turn.

Two defects are covered here.  The responder substituted wall-clock time for the event
timestamp, which conflicted with the immutable source authority (``JournalConflictError``)
so the audience was never registered.  And registration only ever happened at the end of
a response turn, so a message nobody answered stayed an unknown source forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge._capture import ObservedSourceRegistrar
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.processing.models import JournalConflictError
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

NOW = 1_700_000_000_000
GROUP = "491786127564-1611913127@g.us"
DIRECT = "491757070305@s.whatsapp.net"
SENDER = "491757070305"


class _Registry:
    """Minimal stand-in for the live chat registry (proven participants per chat)."""

    def __init__(self, chats: dict[tuple[str, str], list[str]] | None = None) -> None:
        self.chats = chats or {}

    def get_chat(self, channel: str, chat_id: str) -> Any:
        members = self.chats.get((channel, chat_id))
        if members is None:
            return None
        return {"metadata": {"participants": [f"{item}@s.whatsapp.net" for item in members]}}


class _Runtime:
    """A knowledge service whose proof owner is a real processing store."""

    def __init__(self, tmp_path: Path, *, registry: _Registry | None = None) -> None:
        self.store = ProcessingStore(tmp_path / "processing.db")
        self.registry = registry or _Registry()
        self.sources = RuntimeKnowledgeSources(processing_store=self.store)
        self.policy = RuntimeKnowledgePolicy(
            engine=None, chat_registry=self.registry, policy_revision=1
        )
        self.knowledge = open_knowledge_store(
            tmp_path / "knowledge.db",
            workspace_id=workspace_id_for(tmp_path),
            source_authority=self.sources,
            policy_authority=self.policy,
        )

    def close(self) -> None:
        self.knowledge.close()
        self.store.close()


def _append_message(
    runtime: _Runtime,
    *,
    chat_id: str,
    message_id: str,
    registrar: Any | None = None,
) -> str:
    sink = SignalJournalSink(runtime.store, clock=lambda: NOW, sources=registrar)
    stored = sink.capture(
        "message",
        {
            "chatJid": chat_id,
            "messageId": message_id,
            "senderId": SENDER,
            "text": "Wir treffen uns am Freitag um acht.",
            "isGroup": chat_id.endswith("@g.us"),
            "timestamp": NOW,
        },
        event_id=f"wa_{message_id}",
        event_key=f"whatsapp:default:{chat_id}:message:{message_id}",
        account="default",
        observed_at_ms=NOW,
        strict=True,
    )
    assert stored == f"wa_{message_id}"
    return stored


def _authority(runtime: _Runtime, event_id: str, revision: int = 1) -> dict[str, Any]:
    entry = runtime.store.get_event_source_authority(event_id, revision)
    assert entry is not None
    return entry


def test_group_observation_registers_proven_audience_without_a_turn(tmp_path: Path) -> None:
    runtime = _Runtime(
        tmp_path, registry=_Registry({("whatsapp", GROUP): ["491757070305", "491511"]})
    )
    try:
        registrar = ObservedSourceRegistrar(
            knowledge=runtime.knowledge,
            processing=runtime.store,
            chat_registry=runtime.registry,
        )
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m1", registrar=registrar)

        entry = _authority(runtime, event_id)
        assert entry["audience_status"] == "known"
        assert set(entry["audience_members"]) == {
            "491757070305@s.whatsapp.net",
            "491511@s.whatsapp.net",
        }
        # No turn exists in this scenario: the proof came from the observation boundary.
        assert runtime.store.count_events() == 1
    finally:
        runtime.close()


def test_direct_observation_is_proven_author_only(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        registrar = ObservedSourceRegistrar(
            knowledge=runtime.knowledge, processing=runtime.store, chat_registry=runtime.registry
        )
        event_id = _append_message(runtime, chat_id=DIRECT, message_id="m2", registrar=registrar)
        assert _authority(runtime, event_id)["audience_status"] == "author_only"
    finally:
        runtime.close()


def test_group_without_proven_membership_stays_a_durable_unknown_source(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    try:
        registrar = ObservedSourceRegistrar(
            knowledge=runtime.knowledge, processing=runtime.store, chat_registry=runtime.registry
        )
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m3", registrar=registrar)

        # The observation is durable and complete; only the proof is missing.
        assert runtime.store.get_event(event_id) is not None
        assert _authority(runtime, event_id)["audience_status"] == "unknown"
    finally:
        runtime.close()


def test_registration_uses_the_real_event_time_not_the_wall_clock(tmp_path: Path) -> None:
    """The review's synthetic reproduction: a timestamp substitution used to raise."""
    runtime = _Runtime(tmp_path, registry=_Registry({("whatsapp", GROUP): [SENDER]}))
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m4")
        issued = runtime.knowledge.knowledge_sources.verify_source_ref(event_id, 1)
        assert issued is not None and issued.occurred_at_ms == NOW

        # Substituting the current time contradicts the immutable authority row.
        with pytest.raises(JournalConflictError):
            runtime.store.upsert_event_source_authority(
                source=SourceRef(
                    event_id=event_id,
                    revision=1,
                    channel="whatsapp",
                    chat_id=GROUP,
                    author_principal=SENDER,
                    occurred_at_ms=NOW + 60_000,
                ),
                audience={"status": "known", "members": [SENDER]},
            )

        # The canonical provenance registers cleanly and proves the audience.
        assert runtime.knowledge.register_turn_source(
            source=issued,
            verified_members=frozenset({SENDER}),
            snapshot_id="snap-1",
        )
        assert _authority(runtime, event_id)["audience_status"] == "known"
    finally:
        runtime.close()


def test_responder_source_registration_uses_canonical_provenance(tmp_path: Path) -> None:
    """The production method registers the event's own provenance, not a substitution."""
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    class _Probe(LLMResponder):
        def __init__(self, *, knowledge: Any, processing: Any, chat_registry: Any) -> None:
            self.knowledge = knowledge
            self.chat_registry = chat_registry
            self._processing = processing
            self.metrics: list[tuple[str, int]] = []

        def _shared_fact_runtime(self) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(processing=self._processing)

        def _metric(self, name: str, value: int) -> None:
            self.metrics.append((name, int(value)))

    runtime = _Runtime(tmp_path, registry=_Registry({("whatsapp", GROUP): [SENDER, "491511"]}))
    try:
        event_id = _append_message(runtime, chat_id=GROUP, message_id="m5")
        probe = _Probe(
            knowledge=runtime.knowledge,
            processing=runtime.store,
            chat_registry=runtime.registry,
        )

        assert probe._register_knowledge_sources(  # noqa: SLF001 - production method under test
            channel="whatsapp", chat_id=GROUP, sources=[(event_id, 1)]
        )
        entry = _authority(runtime, event_id)
        assert entry["audience_status"] == "known"
        assert entry["occurred_at_ms"] == NOW
    finally:
        runtime.close()


def test_direct_turn_registration_falls_back_to_author_only(tmp_path: Path) -> None:
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    class _Probe(LLMResponder):
        def __init__(self, *, knowledge: Any, processing: Any) -> None:
            self.knowledge = knowledge
            self.chat_registry = None
            self._processing = processing

        def _shared_fact_runtime(self) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(processing=self._processing)

        def _metric(self, name: str, value: int) -> None:
            return None

    runtime = _Runtime(tmp_path)
    try:
        event_id = _append_message(runtime, chat_id=DIRECT, message_id="m6")
        probe = _Probe(knowledge=runtime.knowledge, processing=runtime.store)
        assert probe._register_knowledge_sources(  # noqa: SLF001 - production method under test
            channel="whatsapp", chat_id=DIRECT, sources=[(event_id, 1)]
        )
        assert _authority(runtime, event_id)["audience_status"] == "author_only"
    finally:
        runtime.close()


def test_unknown_event_is_never_registered(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path, registry=_Registry({("whatsapp", GROUP): [SENDER]}))
    try:
        registrar = ObservedSourceRegistrar(
            knowledge=runtime.knowledge, processing=runtime.store, chat_registry=runtime.registry
        )
        assert registrar("does-not-exist") is False
    finally:
        runtime.close()
