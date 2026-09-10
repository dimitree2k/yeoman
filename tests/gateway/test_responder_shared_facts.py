"""Plan 05 / Aufgabe 5: the responder hook reads its context from the frozen turn."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.memory.shared_facts import FactSource, SharedFact
from yeoman_gateway.processing.dispatch import CURRENT_TURN
from yeoman_gateway.processing.models import StoredTurn, TurnBinding
from yeoman_shared.config.schema import Config

T0 = 1_700_000_000_000
CHAT = "34596062240904@lid"
SECRET = "SYNTHETISCHES-GEHEIMTOKEN-4712"


class _Runtime:
    """Minimal stand-in for SharedFactRuntime."""

    def __init__(self, extraction=None, *, require_known: bool = True, processing=None) -> None:
        self.extraction = extraction
        self.processing = processing
        self.chat_registry = None
        self.policy = None
        self.config = type("Cfg", (), {"require_known_membership": require_known})()

    @property
    def extraction_enabled(self) -> bool:
        return self.extraction is not None


class _Queue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "job-1"


def _memory(tmp_path: Path):
    from yeoman_gateway.memory.service import MemoryService

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "memory.db")
    cfg.memory.capture.enabled = False
    cfg.memory.embedding.enabled = False
    cfg.memory.shared.enabled = True
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=cfg.memory)


def _processing(tmp_path: Path):
    from yeoman_gateway.processing.store import ProcessingStore

    return ProcessingStore(tmp_path / "processing.db")


def _responder(memory, runtime) -> LLMResponder:
    responder = LLMResponder.__new__(LLMResponder)
    responder.memory = memory
    responder.shared_facts = runtime
    responder._metric = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return responder


def _turn_binding(principal: str = "member-old", turn_id: str = "tu_1") -> TurnBinding:
    turn = StoredTurn(turn_id=turn_id, thread_id="th_1", principal=principal, revision=3)
    return TurnBinding(turn=turn, trace_id="tr_1", generation_id="gen_1")


def _fact(*, fact_id: str, workspace_id: str, content: str, audience: set[str]) -> SharedFact:
    return SharedFact(
        fact_id=fact_id,
        workspace_id=workspace_id,
        chat_scope_key=f"channel:whatsapp:chat:{CHAT}",
        content=content,
        author_principal="member-old",
        assertion_status="assertion",
        visibility_scope="chat_shared",
        group_rule="chat_members_at_source",
        valid_from_ms=T0,
        extractor_version="v1",
        sources=(FactSource(source_event_id="ev-1", source_revision=1),),
        audience=frozenset(audience),
        created_ms=T0,
        updated_ms=T0,
    )


def test_without_a_runtime_the_hook_is_inert(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    responder = _responder(memory, None)
    token = CURRENT_TURN.set(_turn_binding())
    try:
        assert responder._shared_fact_context(
            query="Stammtisch", channel="whatsapp", chat_id=CHAT, is_owner=False
        ) == ""
        assert responder._enqueue_shared_extraction(channel="whatsapp", chat_id=CHAT) is False
    finally:
        CURRENT_TURN.reset(token)
    memory.close()


def test_without_a_frozen_turn_nothing_is_read_or_queued(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    queue = _Queue()
    responder = _responder(memory, _Runtime(extraction=queue))

    assert responder._shared_fact_context(
        query="Stammtisch", channel="whatsapp", chat_id=CHAT, is_owner=False
    ) == ""
    assert responder._enqueue_shared_extraction(channel="whatsapp", chat_id=CHAT) is False
    assert queue.calls == []
    memory.close()


def test_read_path_uses_the_turn_principal_and_filters_by_permission(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    responder = _responder(memory, _Runtime())
    memory.store.upsert_fact(
        _fact(
            fact_id="visible",
            workspace_id=memory.workspace_id,
            content="Der Stammtisch ist donnerstags.",
            audience={"member-old"},
        )
    )
    memory.store.upsert_fact(
        _fact(
            fact_id="hidden",
            workspace_id=memory.workspace_id,
            content=f"Der Stammtisch ist donnerstags. {SECRET}",
            audience={"member-new"},
        )
    )
    token = CURRENT_TURN.set(_turn_binding("member-old"))
    try:
        text = responder._shared_fact_context(
            query="Wann ist der Stammtisch?", channel="whatsapp", chat_id=CHAT, is_owner=False
        )
    finally:
        CURRENT_TURN.reset(token)

    assert "donnerstags" in text
    assert SECRET not in text
    memory.close()


def test_unknown_membership_reads_nothing_when_required(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    responder = _responder(memory, _Runtime(require_known=True))
    memory.extraction = None
    token = CURRENT_TURN.set(_turn_binding("member-old"))
    try:
        # A direct chat with the reader as the only known member still has membership.
        text = responder._shared_fact_context(
            query="Stammtisch", channel="whatsapp", chat_id=CHAT, is_owner=False
        )
    finally:
        CURRENT_TURN.reset(token)

    assert text == ""
    memory.close()


def test_write_path_queues_the_frozen_turn_sources(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    queue = _Queue()
    processing = _processing(tmp_path)
    responder = _responder(memory, _Runtime(extraction=queue, processing=processing))
    processing.append_event(
        event_key="k1", event_id="ev-1", trace_id="tr_1", payload={"text": "hallo"}, now_ms=T0
    )
    thread_id = processing.open_thread(
        channel="whatsapp", chat_id=CHAT, root_principal="member-old", kind="direct",
        trigger_event_id="ev-1", now_ms=T0,
    )
    turn_id = processing.open_turn(
        thread_id=thread_id, principal="member-old", trigger_event_id="ev-1", now_ms=T0
    )
    processing.add_turn_source(
        turn_id=turn_id, event_id="ev-1", source_message_id="3EB0", role="trigger",
        revision_at_join=1, now_ms=T0,
    )
    token = CURRENT_TURN.set(_turn_binding(turn_id=turn_id))
    try:
        assert responder._enqueue_shared_extraction(channel="whatsapp", chat_id=CHAT) is True
    finally:
        CURRENT_TURN.reset(token)

    assert len(queue.calls) == 1
    call = queue.calls[0]
    assert call["turn_ref"] == turn_id
    assert call["source_refs"] == [("ev-1", 1)]
    assert call["workspace_id"] == memory.workspace_id
    assert call["chat_scope_key"] == f"channel:whatsapp:chat:{CHAT}"
    processing.close()
    memory.close()


def test_write_path_skips_a_turn_without_sources(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    queue = _Queue()
    responder = _responder(memory, _Runtime(extraction=queue))
    token = CURRENT_TURN.set(_turn_binding())
    try:
        assert responder._enqueue_shared_extraction(channel="whatsapp", chat_id=CHAT) is False
    finally:
        CURRENT_TURN.reset(token)

    assert queue.calls == []
    memory.close()
