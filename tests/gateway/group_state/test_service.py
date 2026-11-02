"""The shadow scheduler: dirty marks, bounded rebuilds, chat isolation and labels."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.group_state.contract import ContributorRegistry, SourceReaders
from yeoman_gateway.group_state.labels.runner import ConversationLabeler, LabelRunOutcome
from yeoman_gateway.group_state.models import GroupState, Item, Section
from yeoman_gateway.group_state.service import GroupStateService
from yeoman_gateway.group_state.store import GroupStateStore
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create
from yeoman_shared.config.schema import GroupStateConfig, GroupStateLabelsConfig

CHAT_A = "group-a@g.us"
CHAT_B = "group-b@g.us"
DAY_MS = 24 * 60 * 60 * 1000


class FakeProjector:
    """A real in-memory history database behind the projector's read lease."""

    generation = 1

    def __init__(self) -> None:
        self.calls = 0
        self.paused = False
        self.snapshot: HistorySnapshot | None = None
        self.db = sqlite3.connect(":memory:")
        create(self.db)

    def add_message(
        self, message_id: str, *, chat_id: str = CHAT_A, text: str = "synthetic text"
    ) -> None:
        self.db.execute(
            "INSERT INTO messages VALUES (?, 'whatsapp', ?, ?, NULL, NULL,"
            " 'unknown', 'in', 1, 'native', ?, NULL, NULL, NULL, 'native', '[]')",
            (message_id, chat_id, message_id, text),
        )
        self.db.commit()

    def add_event(
        self,
        event_id: str,
        kind: str,
        occurred_ms: int,
        *,
        target_message_id: str,
        chat_id: str = CHAT_A,
    ) -> None:
        self.db.execute(
            "INSERT INTO message_events (event_id,kind,channel,chat_id,target_message_id,"
            "actor_basis,occurred_ms,time_certainty,payload_json,provenance,source_refs)"
            " VALUES (?,?,'whatsapp',?,?,'unknown',?,'native',?,'native','[]')",
            (
                event_id,
                kind,
                chat_id,
                target_message_id,
                occurred_ms,
                json.dumps({"text": "edited" if kind == "edit" else ""}),
            ),
        )
        self.db.commit()

    async def read_turn(self) -> HistorySnapshot:
        self.calls += 1
        if self.paused:
            raise HistoryPaused("paused")
        db = sqlite3.connect(":memory:")
        self.db.backup(db)
        self.snapshot = HistorySnapshot(self.generation, (), db)
        return self.snapshot

    def close(self) -> None:
        if self.snapshot is not None:
            self.snapshot.close()
        self.db.close()


class StubContributor:
    def __init__(
        self,
        key: str,
        *,
        layer: str = "live",
        version: int = 1,
        fail: bool = False,
    ) -> None:
        self.key = key
        self.schema_version = 1
        self.trust = "runtime"
        self.layer = layer
        self.version = version
        self.fail = fail

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: Any) -> Section:
        if self.fail:
            raise RuntimeError("contributor exploded")
        return Section(
            key=self.key,
            schema_version=1,
            trust=self.trust,
            layer=self.layer,
            produced_at_ms=ctx.now_ms,
            ttl_ms=60_000,
            source_cursor="m1",
            period_start_ms=None,
            period_end_ms=None,
            sources=("m1",),
            data={"chat_id": ctx.chat_id, "version": self.version},
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - v1 only
        return data


class ConversationsStub:
    """A deterministic conversations section, so label scheduling has a newest chat."""

    key = "conversations"
    schema_version = 1
    trust = "runtime"
    layer = "conversation"

    def __init__(self, conversations: list[dict[str, Any]]) -> None:
        self.conversations = conversations

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: Any) -> Section:
        return Section(
            key=self.key,
            schema_version=1,
            trust=self.trust,
            layer=self.layer,
            produced_at_ms=ctx.now_ms,
            ttl_ms=60_000,
            source_cursor="h4",
            period_start_ms=None,
            period_end_ms=None,
            sources=("h4",),
            data={"conversations": self.conversations},
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - v1 only
        return data


class FakeLabeler:
    """Records one label call; optionally blocks so a task can be observed."""

    def __init__(self, outcome: str = "written", *, block: bool = False) -> None:
        self.outcome = outcome
        self.block = block
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def label(
        self, channel: str, chat_id: str, conversation: Any, *, lock: Any = None
    ) -> LabelRunOutcome:
        self.calls.append(
            {
                "channel": channel,
                "chat_id": chat_id,
                "conversation": dict(conversation),
            }
        )
        self.entered.set()
        if self.block:
            await self.release.wait()
        return LabelRunOutcome(self.outcome, 1, 1, 10, 5)


class BlockingChat:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        self.entered.set()
        await asyncio.Event().wait()


def _labels_config(**overrides: Any) -> GroupStateLabelsConfig:
    values: dict[str, Any] = {
        "mode": "on",
        "chats": [f"whatsapp:{CHAT_A}"],
    }
    values.update(overrides)
    return GroupStateLabelsConfig.model_validate(values)


def _conversation(
    *,
    last_ms: int,
    messages: int = 10,
    last_message_id: str = "h10",
    conversation_id: str = "conv:1",
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "start_ms": 1,
        "last_ms": last_ms,
        "messages": messages,
        "last_message_id": last_message_id,
        "participants": [],
        "thread_ids": [],
    }


def _section_with_item(
    *, item_id: str = "label-1", source: str = "m1", key: str = "conversations"
) -> Section:
    item = Item(
        id=item_id,
        kind="label",
        data={"label": "synthetic"},
        sources=(source,),
        source_fingerprints={source: "fingerprint"},
        produced_at_ms=1_000_000,
        ttl_ms=600_000,
        trust="derived",
        conversation_id="conversation-1",
    )
    return Section(
        key=key,
        schema_version=1,
        trust="derived",
        layer="conversation",
        produced_at_ms=1_000_000,
        ttl_ms=600_000,
        source_cursor=source,
        period_start_ms=None,
        period_end_ms=None,
        sources=(source,),
        data={"conversations": []},
        items=(item,),
    )


def _registry(*contributors: StubContributor) -> ContributorRegistry:
    registry = ContributorRegistry()
    for contributor in contributors:
        registry.register(contributor)
    return registry


def _settings(**overrides: Any) -> GroupStateConfig:
    values = {"mode": "shadow", "chats": [f"whatsapp:{CHAT_A}", f"whatsapp:{CHAT_B}"]}
    values.update(overrides)
    return GroupStateConfig.model_validate(values)


def _service(
    tmp_path,
    *,
    registry: ContributorRegistry,
    clock: list[int],
    settings: GroupStateConfig | None = None,
    projector: FakeProjector | None = None,
    labeler: Any = None,
    consciousness: Any = None,
    store: GroupStateStore | None = None,
) -> tuple[GroupStateService, GroupStateStore, FakeProjector]:
    store = store or GroupStateStore(tmp_path / "group-state.db")
    projector = projector or FakeProjector()
    service = GroupStateService(
        store=store,
        registry=registry,
        settings=settings or _settings(),
        history_projector=projector,
        sources=SourceReaders(),
        clock_ms=lambda: clock[0],
        consciousness=consciousness,
        labeler=labeler,
    )
    return service, store, projector


@pytest.mark.asyncio
async def test_mark_dirty_unmanaged_noop(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    try:
        assert service.manages("whatsapp", CHAT_A) is True
        assert service.manages("whatsapp", "other@g.us") is False
        service.mark_dirty("whatsapp", "other@g.us")
        assert service._dirty == set()
        await service.tick_once()
        assert store.get("whatsapp", "other@g.us") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rebuild_writes_all_sections(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence"), StubContributor("budgets")),
        clock=clock,
    )
    try:
        state = await service.rebuild("whatsapp", CHAT_A)
        assert state is not None
        assert state.revision == 1
        assert state.history_cursor == "m1"
        assert set(state.sections) == {"presence", "budgets"}
        stored = store.get("whatsapp", CHAT_A)
        assert stored == state
        assert store.snapshots("whatsapp", CHAT_A, limit=5) == [state]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_contributor_keeps_previous_section(tmp_path) -> None:
    clock = [1_000_000]
    good = StubContributor("presence")
    flaky = StubContributor("budgets")
    service, store, _ = _service(tmp_path, registry=_registry(good, flaky), clock=clock)
    try:
        first = await service.rebuild("whatsapp", CHAT_A)
        assert first is not None
        previous = first.sections["budgets"]
        good.version = 2
        flaky.fail = True
        clock[0] += 60_000
        second = await service.rebuild("whatsapp", CHAT_A)
        assert second is not None
        assert second.revision == 2
        assert second.sections["presence"].data["version"] == 2
        assert second.sections["budgets"] == previous
    finally:
        store.close()


@pytest.mark.asyncio
async def test_history_paused_keeps_dirty(tmp_path) -> None:
    clock = [1_000_000]
    service, store, projector = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    try:
        service.mark_dirty("whatsapp", CHAT_A)
        projector.paused = True
        assert await service.rebuild("whatsapp", CHAT_A) is None
        assert ("whatsapp", CHAT_A) in service._dirty
        assert store.get("whatsapp", CHAT_A) is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_min_interval_coalesces(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        settings=_settings(min_interval_seconds=30),
    )
    try:
        service.mark_dirty("whatsapp", CHAT_A)
        await service.tick_once()
        assert store.get("whatsapp", CHAT_A).revision == 1
        service.mark_dirty("whatsapp", CHAT_A)
        clock[0] += 10_000
        await service.tick_once()
        assert store.get("whatsapp", CHAT_A).revision == 1
        clock[0] += 21_000
        await service.tick_once()
        assert store.get("whatsapp", CHAT_A).revision == 2
        assert service._dirty == set()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_chats_isolated(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    try:
        a = await service.rebuild("whatsapp", CHAT_A)
        clock[0] += 1_000
        b = await service.rebuild("whatsapp", CHAT_B)
        assert a is not None and b is not None
        assert a.sections["presence"].data["chat_id"] == CHAT_A
        assert b.sections["presence"].data["chat_id"] == CHAT_B
        assert store.get("whatsapp", CHAT_A) == a
        assert store.get("whatsapp", CHAT_B) == b
        assert store.list_chats() == [
            ("whatsapp", CHAT_A, 1, 1_000_000),
            ("whatsapp", CHAT_B, 1, 1_001_000),
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rebuild_does_not_overwrite_foreign_section(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    real_merge = store.merge_sections
    raced = [False]

    def racing_merge(channel, chat_id, sections, **kwargs):
        if not raced[0]:
            raced[0] = True
            foreign = _section_with_item(item_id="foreign-label", source="m9")
            assert real_merge(channel, chat_id, {"label": foreign}, updated_ms=clock[0]) is not None
        return real_merge(channel, chat_id, sections, **kwargs)

    store.merge_sections = racing_merge  # type: ignore[method-assign]
    try:
        state = await service.rebuild("whatsapp", CHAT_A)
        assert state is not None
        assert state.revision == 2
        assert set(state.sections) == {"label", "presence"}
        assert state.sections["label"].items[0].id == "foreign-label"
        assert store.get("whatsapp", CHAT_A) == state
    finally:
        store.close()


@pytest.mark.asyncio
async def test_rebuild_after_purge_inserts_fresh(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    real_merge = store.merge_sections

    stale = GroupState(
        channel="whatsapp",
        chat_id=CHAT_A,
        schema_version=2,
        revision=4,
        history_cursor="m9",
        updated_ms=clock[0] - 1_000,
        sections={"stale": _section_with_item(item_id="stale-label", source="m9")},
    )
    assert store.put(stale, expected_revision=None) is True

    def purging_merge(channel, chat_id, sections, **kwargs):
        store.purge_chat(channel, chat_id)
        return real_merge(channel, chat_id, sections, **kwargs)

    store.merge_sections = purging_merge  # type: ignore[method-assign]
    try:
        state = await service.rebuild("whatsapp", CHAT_A)
        assert state is not None
        assert state.revision == 1
        assert set(state.sections) == {"presence"}
        assert store.get("whatsapp", CHAT_A) == state
    finally:
        store.close()


def _item_state(*, item_id: str = "label-1", source: str = "m1") -> Section:
    return _section_with_item(item_id=item_id, source=source)


@pytest.mark.asyncio
async def test_invalidation_scrubs_deleted_source(tmp_path) -> None:
    clock = [1_000_000]
    projector = FakeProjector()
    projector.add_message("m1")
    projector.add_event("e-delete", "delete", 500, target_message_id="m1")
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        projector=projector,
    )
    try:
        assert (
            store.merge_sections(
                "whatsapp",
                CHAT_A,
                {"conversations": _item_state()},
                updated_ms=clock[0],
            )
            is not None
        )
        await service.tick_once()
        state = store.get("whatsapp", CHAT_A)
        assert state is not None
        assert state.sections["conversations"].items == ()
        assert state.revision == 2
        assert store.invalidation_cursor("whatsapp", CHAT_A) == (500, "e-delete")
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_invalidation_scrubs_edited_source(tmp_path) -> None:
    clock = [1_000_000]
    projector = FakeProjector()
    projector.add_message("m1")
    projector.add_event("e-edit", "edit", 600, target_message_id="m1")
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        projector=projector,
    )
    try:
        assert (
            store.merge_sections(
                "whatsapp",
                CHAT_A,
                {"conversations": _item_state()},
                updated_ms=clock[0],
            )
            is not None
        )
        await service.tick_once()
        state = store.get("whatsapp", CHAT_A)
        assert state is not None
        assert state.sections["conversations"].items == ()
        assert store.invalidation_cursor("whatsapp", CHAT_A) == (600, "e-edit")
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_invalidation_scrubs_purged_source(tmp_path) -> None:
    clock = [1_000_000]
    projector = FakeProjector()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        projector=projector,
    )
    try:
        assert (
            store.merge_sections(
                "whatsapp",
                CHAT_A,
                {"conversations": _item_state(source="gone")},
                updated_ms=clock[0],
            )
            is not None
        )
        await service.tick_once()
        state = store.get("whatsapp", CHAT_A)
        assert state is not None
        assert state.sections["conversations"].items == ()
        assert store.invalidation_cursor("whatsapp", CHAT_A) == (0, "")
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_invalidation_scrubs_purged_source_kept_only_in_snapshot(tmp_path) -> None:
    clock = [1_000_000]
    projector = FakeProjector()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        projector=projector,
    )
    try:
        store.merge_sections(
            "whatsapp", CHAT_A, {"conversations": _item_state(source="gone")}, updated_ms=clock[0]
        )
        # The item leaves the document but stays in the first snapshot.
        store.merge_sections("whatsapp", CHAT_A, {}, remove=("conversations",), updated_ms=clock[0])
        assert store.item_sources("whatsapp", CHAT_A) == {"gone"}
        await service.tick_once()
        assert store.item_sources("whatsapp", CHAT_A) == set()
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_invalidation_cursor_advances_and_is_idempotent(tmp_path) -> None:
    clock = [1_000_000]
    projector = FakeProjector()
    projector.add_message("m1")
    projector.add_event("e-delete", "delete", 500, target_message_id="m1")
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        projector=projector,
    )
    try:
        assert (
            store.merge_sections(
                "whatsapp",
                CHAT_A,
                {"conversations": _item_state()},
                updated_ms=clock[0],
            )
            is not None
        )
        await service.tick_once()
        first = store.get("whatsapp", CHAT_A)
        assert first is not None
        assert first.revision == 2
        assert store.invalidation_cursor("whatsapp", CHAT_A) == (500, "e-delete")
        await service.tick_once()
        second = store.get("whatsapp", CHAT_A)
        assert second is not None
        assert second.revision == 2
        assert store.invalidation_cursor("whatsapp", CHAT_A) == (500, "e-delete")
    finally:
        store.close()
        projector.close()


def _label_rows(store: GroupStateStore) -> list[dict[str, Any]]:
    rows = store._conn.execute(
        "SELECT run_id, outcome, started_ms FROM group_state_runs WHERE kind='labels'"
        " ORDER BY run_id"
    ).fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_labels_off_makes_no_calls_and_no_rows(tmp_path) -> None:
    clock = [1_700_000_000_000]
    labels = _labels_config(mode="off")
    fake = FakeLabeler()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=labels),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        assert fake.calls == []
        assert service._label_tasks == {}
        assert _label_rows(store) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_labels_on_quiet_conversation_triggers_once(tmp_path) -> None:
    clock = [1_700_000_000_000]
    labels = _labels_config()
    fake = FakeLabeler()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=labels),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        assert len(service._label_tasks) == 1
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 1

        clock[0] += 10_000
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 1
    finally:
        await service.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, calls_after", [("no_input", 1), ("skipped_budget", 1)])
async def test_labels_not_retried_each_tick_for_short_or_budget(
    tmp_path, outcome: str, calls_after: int
) -> None:
    clock = [1_700_000_000_000]
    fake = FakeLabeler(outcome)
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=_labels_config()),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        # Past the 30 s retry throttle, still inside the 10 min rebuild interval.
        clock[0] += 60_000
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == calls_after
    finally:
        await service.stop()
        store.close()


@pytest.mark.asyncio
async def test_labels_refresh_after_n_messages(tmp_path) -> None:
    clock = [1_700_000_000_000]
    labels = _labels_config(refresh_after_messages=30)
    stub = ConversationsStub(
        [_conversation(last_ms=clock[0] - 120_000, messages=10, last_message_id="h10")]
    )
    fake = FakeLabeler()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(stub),
        clock=clock,
        settings=_settings(labels=labels),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 1

        stub.conversations = [
            _conversation(last_ms=clock[0] - 1_000, messages=40, last_message_id="h40")
        ]
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 2
        assert fake.calls[-1]["conversation"]["messages"] == 40
    finally:
        await service.stop()
        store.close()


@pytest.mark.asyncio
async def test_label_task_coalesced_per_chat(tmp_path) -> None:
    clock = [1_700_000_000_000]
    fake = FakeLabeler(block=True)
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=_labels_config()),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await fake.entered.wait()
        await service.tick_once()
        assert len(fake.calls) == 1
        assert len(service._label_tasks) == 1
        fake.release.set()
        await asyncio.gather(*list(service._label_tasks.values()))
    finally:
        await service.stop()
        store.close()


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_label_without_write(tmp_path) -> None:
    clock = [1_700_000_000_000]
    projector = FakeProjector()
    for index in range(1, 5):
        projector.add_message(f"h{index}", text=f"synthetic {index}")
    labels = _labels_config()
    chat = BlockingChat()
    store = GroupStateStore(tmp_path / "group-state.db")
    labeler = ConversationLabeler(
        store=store,
        history_projector=projector,
        settings=labels,
        client_factory=lambda: chat,
        clock=lambda: clock[0] / 1000,
    )
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=labels),
        projector=projector,
        labeler=labeler,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
        store=store,
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await chat.entered.wait()
        await service.stop()
        assert service._label_tasks == {}
        state = store.get("whatsapp", CHAT_A)
        assert state is not None
        assert "conversation_labels" not in state.sections
        assert _label_rows(store) == []
    finally:
        store.close()
        projector.close()


@pytest.mark.asyncio
async def test_unmanaged_chat_in_labels_chats_ignored(tmp_path) -> None:
    clock = [1_700_000_000_000]
    fake = FakeLabeler()
    labels = _labels_config(chats=[f"whatsapp:{CHAT_B}"])
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=labels),
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        assert fake.calls == []
        assert service._label_tasks == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_prune_runs_daily(tmp_path) -> None:
    clock = [1_700_000_000_000]
    service, store, _ = _service(
        tmp_path,
        registry=_registry(StubContributor("presence")),
        clock=clock,
        settings=_settings(labels=_labels_config(chats=[])),
    )

    def seed(run_id: str) -> None:
        store.record_run(
            "whatsapp",
            CHAT_A,
            run_id=run_id,
            kind="labels",
            started_ms=clock[0] - 40 * DAY_MS,
            input_hash=run_id,
            prompt_version="v",
            model="m",
            outcome="written",
        )

    def present(run_id: str) -> bool:
        row = store._conn.execute(
            "SELECT 1 FROM group_state_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row is not None

    try:
        seed("old-1")
        await service.tick_once()
        assert not present("old-1")

        seed("old-2")
        await service.tick_once()
        assert present("old-2")

        clock[0] += DAY_MS
        seed("old-3")
        await service.tick_once()
        assert not present("old-2")
        assert not present("old-3")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_labels_relabels_quiet_chat_after_scrub(tmp_path) -> None:
    clock = [1_700_000_000_000]
    projector = FakeProjector()
    projector.add_message("h1", text="synthetic")
    fake = FakeLabeler()
    service, store, _ = _service(
        tmp_path,
        registry=_registry(ConversationsStub([_conversation(last_ms=clock[0] - 120_000)])),
        clock=clock,
        settings=_settings(labels=_labels_config()),
        projector=projector,
        labeler=fake,
        consciousness=SimpleNamespace(lull_silence_minutes=1),
    )
    try:
        await service.rebuild("whatsapp", CHAT_A)
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 1

        # The ticker wrote labels, then an edit invalidated one source and scrubbed it.
        labelled = _section_with_item(
            item_id="conv:1:topic:1", source="h1", key="conversation_labels"
        )
        assert (
            store.merge_sections(
                "whatsapp", CHAT_A, {"conversation_labels": labelled}, updated_ms=clock[0]
            )
            is not None
        )
        projector.add_event("e-edit", "edit", 500, target_message_id="h1")
        await service.tick_once()
        await asyncio.gather(*list(service._label_tasks.values()))
        assert len(fake.calls) == 2
    finally:
        await service.stop()
        store.close()
        projector.close()
