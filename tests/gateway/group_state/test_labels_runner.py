"""The conversation label runner: call, repair, budget, cache, re-read and write."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.group_state.labels.prompt import build_label_input
from yeoman_gateway.group_state.labels.runner import (
    LABELS_SECTION,
    ConversationLabeler,
    LabelRunOutcome,
)
from yeoman_gateway.group_state.models import Item, Section
from yeoman_gateway.group_state.store import GroupStateStore
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create
from yeoman_gateway.processing.model_route import RouteUnavailableError
from yeoman_shared.config.schema import GroupStateLabelsConfig

CHAT = "group-a@g.us"
ARVID = "arvid-contact"
NOW = 1_700_000_000.0
NOW_MS = int(NOW * 1000)
CONV = {
    "conversation_id": "conv:d",
    "start_ms": 1_000,
    "last_ms": 4_000,
    "messages": 4,
    "last_message_id": "h4",
}
VALID = json.dumps(
    {
        "topic": "Samstagsplan",
        "mood": "relaxed",
        "open_questions": [{"text": "Kommst du Samstag?", "asked_by": "p1", "source": "m1"}],
        "plans": [{"text": "Treffen am Samstag", "who": ["p1"], "when": "18:00", "source": "m3"}],
    },
    ensure_ascii=False,
)


class FakeReply:
    def __init__(
        self, content: str, *, usage: dict[str, int] | None = None, finish_reason: str = ""
    ) -> None:
        self.content = content
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}
        self.finish_reason = finish_reason


class FakeChat:
    def __init__(
        self,
        *replies: FakeReply,
        on_call: Any = None,
        delay: float = 0.0,
    ) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.on_call = on_call
        self.delay = delay

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> FakeReply:
        self.calls.append(
            {
                "messages": messages,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "timeout_ms": timeout_ms,
            }
        )
        if self.on_call is not None:
            result = self.on_call()
            if asyncio.iscoroutine(result):
                await result
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.replies.pop(0) if self.replies else FakeReply(VALID)


class FakeProjector:
    """A real in-memory history database behind the projector's read lease."""

    generation = 1

    def __init__(self) -> None:
        self.calls = 0
        self.snapshot: HistorySnapshot | None = None
        self.db = sqlite3.connect(":memory:")
        create(self.db)
        self.add_contact(ARVID, role="assistant")

    def add_contact(self, contact_id: str, *, role: str | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO contacts"
            "(contact_id, kind, role, display_name, status, merged_into, source_refs)"
            " VALUES (?, 'person', ?, NULL, 'confirmed', NULL, '[]')",
            (contact_id, role),
        )
        self.db.commit()

    def add_message(
        self,
        message_id: str,
        *,
        text: str,
        sender: str = "contact-a",
        direction: str = "in",
        sent_ms: int,
        media: Any = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO messages(message_id, channel, chat_id, native_message_id,"
            " sender_contact_id, sender_identifier, sender_basis, direction, sent_ms,"
            " time_certainty, text, media_json, reply_to_native_id, mentions_json,"
            " provenance, source_refs)"
            " VALUES (?, 'whatsapp', ?, ?, ?, ?, 'native_identifier', ?, ?, 'native', ?, ?,"
            " NULL, NULL, 'native', '[]')",
            (
                message_id,
                CHAT,
                f"native-{message_id}",
                sender,
                sender,
                direction,
                sent_ms,
                text,
                json.dumps(media) if media is not None else None,
            ),
        )
        self.db.commit()

    def edit_text(self, message_id: str, text: str, *, occurred_ms: int = 2_500) -> None:
        self.db.execute(
            "INSERT INTO message_events(event_id, kind, channel, chat_id, target_message_id,"
            " actor_basis, occurred_ms, time_certainty, payload_json, provenance, source_refs)"
            " VALUES (?, 'edit', 'whatsapp', ?, ?, 'unknown', ?, 'native', ?, 'native', '[]')",
            (
                f"edit-{message_id}-{occurred_ms}",
                CHAT,
                message_id,
                occurred_ms,
                json.dumps({"text": text}),
            ),
        )
        self.db.commit()

    async def read_turn(self) -> HistorySnapshot:
        self.calls += 1
        db = sqlite3.connect(":memory:")
        self.db.backup(db)
        self.snapshot = HistorySnapshot(self.generation, (), db)
        return self.snapshot

    async def fingerprints(self, message_ids: tuple[str, ...]) -> dict[str, str | None]:
        snapshot = await self.read_turn()
        try:
            queries = HistoryQueries(snapshot)
            return {
                message_id: queries.content_fingerprint(message_id) for message_id in message_ids
            }
        finally:
            snapshot.close()

    def close(self) -> None:
        if self.snapshot is not None:
            self.snapshot.close()
        self.db.close()


def _projector() -> FakeProjector:
    projector = FakeProjector()
    for index, (sender, text) in enumerate(
        (
            ("contact-a", "Hallo zusammen"),
            ("contact-b", "Kommst du Samstag?"),
            ("contact-a", "Ja, um 18 Uhr"),
            ("contact-b", "Super"),
        ),
        start=1,
    ):
        projector.add_message(f"h{index}", text=text, sender=sender, sent_ms=index * 1_000)
    return projector


def _settings(**overrides: Any) -> GroupStateLabelsConfig:
    values: dict[str, Any] = {
        "mode": "on",
        "chats": [f"whatsapp:{CHAT}"],
        "min_messages": 4,
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return GroupStateLabelsConfig.model_validate(values)


def _seed_state(store: GroupStateStore) -> None:
    """The ticker's rebuild has already produced the document the writer merges into."""
    section = Section(
        key="presence",
        schema_version=1,
        trust="runtime",
        layer="live",
        produced_at_ms=1,
        ttl_ms=60_000,
        source_cursor="h4",
        period_start_ms=None,
        period_end_ms=None,
        sources=("h4",),
        data={},
    )
    assert store.merge_sections("whatsapp", CHAT, {"presence": section}, updated_ms=1) is not None


def _labeler(
    store: GroupStateStore,
    projector: FakeProjector,
    *,
    settings: GroupStateLabelsConfig | None = None,
    factory: Any = None,
    clock: float = NOW,
) -> ConversationLabeler:
    if store.get("whatsapp", CHAT) is None:
        _seed_state(store)
    return ConversationLabeler(
        store=store,
        history_projector=projector,
        settings=settings or _settings(),
        client_factory=factory or (lambda: FakeChat(FakeReply(VALID))),
        clock=lambda: clock,
    )


def _runs(store: GroupStateStore, chat_id: str = CHAT) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT run_id, kind, input_hash, outcome, prompt_tokens, completion_tokens"
        " FROM group_state_runs WHERE channel='whatsapp' AND chat_id=? ORDER BY run_id",
        (chat_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_writes_items_with_sources_and_fingerprints(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    try:
        outcome = await _labeler(store, projector).label("whatsapp", CHAT, CONV)
        assert outcome.outcome == "written"
        assert outcome.items == 4
        assert outcome.attempts == 1
        state = store.get("whatsapp", CHAT)
        assert state is not None
        section = state.sections[LABELS_SECTION]
        assert section.layer == "conversation"
        assert section.trust == "derived"
        assert section.data == {}
        by_kind = {item.kind: item for item in section.items}
        assert set(by_kind) == {"topic", "mood", "open_question", "plan"}
        assert set(by_kind["topic"].sources) == {"h1", "h2", "h3", "h4"}
        assert set(by_kind["mood"].sources) == {"h1", "h2", "h3", "h4"}
        assert by_kind["open_question"].sources == ("h1",)
        assert by_kind["plan"].sources == ("h3",)
        expected = await projector.fingerprints(("h1", "h2", "h3", "h4"))
        for item in section.items:
            assert item.trust == "derived"
            assert item.conversation_id == "conv:d"
            assert item.ttl_ms == 24 * 60 * 60 * 1000
            for source in item.sources:
                assert item.source_fingerprints[source] == expected[source]
        assert [row["outcome"] for row in _runs(store)] == ["written"]
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_cached_input_makes_no_call(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    chat = FakeChat(FakeReply(VALID))
    try:
        labeler = _labeler(store, projector, factory=lambda: chat)
        assert (await labeler.label("whatsapp", CHAT, CONV)).outcome == "written"
        chat.calls.clear()
        cached = await labeler.label("whatsapp", CHAT, CONV)
        assert cached.outcome == "cached"
        assert cached.attempts == 0
        assert chat.calls == []
        assert [row["outcome"] for row in _runs(store)] == ["written"]
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_repair_retry_counts_two_attempts_in_budget(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    chat = FakeChat(FakeReply("not json"), FakeReply(VALID))
    settings = _settings(max_per_chat_day=2, max_global_day=10)
    try:
        outcome = await _labeler(store, projector, settings=settings, factory=lambda: chat).label(
            "whatsapp", CHAT, CONV
        )
        assert outcome.outcome == "written"
        assert outcome.attempts == 2
        assert [row["outcome"] for row in _runs(store)] == ["invalid", "written"]
        assert len(chat.calls) == 2
        assert "invalid" in chat.calls[1]["messages"][-1]["content"]

        projector.edit_text("h1", "Hallo nochmal")
        skipped = await _labeler(store, projector, settings=settings, factory=lambda: chat).label(
            "whatsapp", CHAT, CONV
        )
        assert skipped.outcome == "skipped_budget"
        assert skipped.attempts == 0
        assert len(chat.calls) == 2
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "global", "tokens"])
async def test_budget_per_chat_global_tokens_each_skip(tmp_path, kind: str) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    chat = FakeChat(FakeReply(VALID))
    settings = _settings()
    if kind == "chat":
        settings = _settings(max_per_chat_day=1, max_global_day=100)
        store.record_run(
            "whatsapp",
            CHAT,
            run_id="seed",
            kind="labels",
            started_ms=NOW_MS,
            input_hash="seed",
            prompt_version="v",
            model="m",
            outcome="written",
        )
    elif kind == "global":
        settings = _settings(max_per_chat_day=100, max_global_day=1)
        store.record_run(
            "whatsapp",
            "other@g.us",
            run_id="seed",
            kind="labels",
            started_ms=NOW_MS,
            input_hash="seed",
            prompt_version="v",
            model="m",
            outcome="written",
        )
    else:
        settings = _settings(max_tokens_day=100)
        store.record_run(
            "whatsapp",
            "other@g.us",
            run_id="seed",
            kind="labels",
            started_ms=NOW_MS,
            input_hash="seed",
            prompt_version="v",
            model="m",
            outcome="invalid",
            prompt_tokens=60,
            completion_tokens=50,
        )
    try:
        outcome = await _labeler(store, projector, settings=settings, factory=lambda: chat).label(
            "whatsapp", CHAT, CONV
        )
        assert outcome.outcome == "skipped_budget"
        assert outcome.attempts == 0
        assert chat.calls == []
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_budget_counts_survive_new_runner_instance(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    settings = _settings(max_per_chat_day=1, max_global_day=100)
    try:
        first_chat = FakeChat(FakeReply(VALID))
        assert (
            await _labeler(store, projector, settings=settings, factory=lambda: first_chat).label(
                "whatsapp", CHAT, CONV
            )
        ).outcome == "written"
        projector.edit_text("h2", "Kommst du Sonntag?")
        second_chat = FakeChat(FakeReply(VALID))
        outcome = await _labeler(
            store, projector, settings=settings, factory=lambda: second_chat
        ).label("whatsapp", CHAT, CONV)
        assert outcome.outcome == "skipped_budget"
        assert second_chat.calls == []
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_source_edited_during_call_discards(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    chat = FakeChat(FakeReply(VALID), on_call=lambda: projector.edit_text("h2", "Andere Frage"))
    try:
        outcome = await _labeler(store, projector, factory=lambda: chat).label(
            "whatsapp", CHAT, CONV
        )
        assert outcome.outcome == "source_changed"
        assert outcome.items == 0
        state = store.get("whatsapp", CHAT)
        assert state is None or state.sections.get(LABELS_SECTION) is None
        assert [row["outcome"] for row in _runs(store)] == ["source_changed"]
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_purged_during_call_does_not_recreate(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    state = await _labeler(store, projector).label("whatsapp", CHAT, CONV)
    assert state.outcome == "written"
    chat = FakeChat(FakeReply(VALID), on_call=lambda: store.purge_chat("whatsapp", CHAT))
    try:
        # A different conversation id keeps the input hash different, so no cache hit.
        projector.edit_text("h3", "Ja, um 19 Uhr")
        outcome = await _labeler(store, projector, factory=lambda: chat).label(
            "whatsapp", CHAT, CONV
        )
        assert outcome.outcome == "purged"
        assert store.get("whatsapp", CHAT) is None
        assert "purged" in [row["outcome"] for row in _runs(store)]
    finally:
        store.close()
        projector.close()


def _item(item_id: str, conversation_id: str, *, produced_at_ms: int, source: str) -> Item:
    return Item(
        id=item_id,
        kind="topic",
        data={"text": item_id},
        sources=(source,),
        source_fingerprints={source: "fingerprint"},
        produced_at_ms=produced_at_ms,
        ttl_ms=24 * 60 * 60 * 1000,
        trust="derived",
        conversation_id=conversation_id,
    )


@pytest.mark.asyncio
async def test_keeps_other_conversations_items_up_to_max(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    kept = Section(
        key=LABELS_SECTION,
        schema_version=1,
        trust="derived",
        layer="conversation",
        produced_at_ms=1_000,
        ttl_ms=24 * 60 * 60 * 1000,
        source_cursor="h4",
        period_start_ms=None,
        period_end_ms=None,
        sources=("h1", "h2", "h3"),
        data={},
        items=(
            _item("conv:a:topic:1", "conv:a", produced_at_ms=1_000, source="h1"),
            _item("conv:b:topic:1", "conv:b", produced_at_ms=2_000, source="h2"),
            _item("conv:c:topic:1", "conv:c", produced_at_ms=3_000, source="h3"),
        ),
    )
    assert (
        store.merge_sections("whatsapp", CHAT, {LABELS_SECTION: kept}, updated_ms=3_000) is not None
    )
    try:
        outcome = await _labeler(store, projector, settings=_settings(max_conversations=2)).label(
            "whatsapp", CHAT, CONV
        )
        assert outcome.outcome == "written"
        state = store.get("whatsapp", CHAT)
        assert state is not None
        conversations = {item.conversation_id for item in state.sections[LABELS_SECTION].items}
        assert conversations == {"conv:c", "conv:d"}
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_route_unavailable_logs_once_no_raise(tmp_path) -> None:
    import yeoman_gateway.group_state.labels.runner as runner_module

    runner_module._UNAVAILABLE_LOGGED = False
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="INFO")

    def unavailable() -> Any:
        raise RouteUnavailableError("missing route")

    try:
        for _ in range(2):
            outcome = await _labeler(store, projector, factory=unavailable).label(
                "whatsapp", CHAT, CONV
            )
            assert outcome.outcome == "unavailable"
            assert outcome.attempts == 0
        assert sum("group_state_labels_unavailable" in line for line in messages) == 1
        assert _runs(store) == []
    finally:
        logger.remove(sink)
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_timeout_records_outcome(tmp_path) -> None:
    store = GroupStateStore(tmp_path / "state.db")
    projector = _projector()
    chat = FakeChat(FakeReply(VALID), delay=0.2)
    try:
        outcome = await _labeler(
            store, projector, settings=_settings(timeout_seconds=0.01), factory=lambda: chat
        ).label("whatsapp", CHAT, CONV)
        assert outcome.outcome == "timeout"
        assert outcome.attempts == 1
        assert [row["outcome"] for row in _runs(store)] == ["timeout"]
        state = store.get("whatsapp", CHAT)
        assert state is None or LABELS_SECTION not in state.sections
    finally:
        store.close()
        projector.close()


def test_german_conversation_prompt_requests_conversation_language() -> None:
    projector = _projector()
    try:
        rows = projector.db.execute("SELECT * FROM messages_current ORDER BY sent_ms").fetchall()
        columns = [
            item[0]
            for item in projector.db.execute("SELECT * FROM messages_current LIMIT 1").description
        ]
        mappings = [dict(zip(columns, row, strict=True)) for row in rows]
        label_input = build_label_input(
            mappings,
            conversation_id="conv:d",
            assistant_ids={ARVID},
            fingerprint_of=lambda message_id: f"fp-{message_id}",
            max_chars=10_000,
            min_messages=1,
        )
        assert label_input is not None
        assert "language of the conversation" in label_input.messages[0]["content"]
    finally:
        projector.close()


def test_outcome_dataclass_defaults() -> None:
    outcome = LabelRunOutcome("cached", 0, 0, 0, 0)
    assert outcome.outcome == "cached"
    assert LABELS_SECTION == "conversation_labels"
