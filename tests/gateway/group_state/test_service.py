"""The shadow scheduler: dirty marks, bounded rebuilds and chat isolation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.group_state.contract import ContributorRegistry, SourceReaders
from yeoman_gateway.group_state.models import GroupState, Section
from yeoman_gateway.group_state.service import GroupStateService
from yeoman_gateway.group_state.store import GroupStateStore
from yeoman_gateway.history.live import HistoryPaused
from yeoman_shared.config.schema import GroupStateConfig

CHAT_A = "group-a@g.us"
CHAT_B = "group-b@g.us"


class FakeSnapshot:
    generation = 1
    _closed = False

    def __init__(self) -> None:
        self.connection = SimpleNamespace(total_changes=0)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeProjector:
    def __init__(self) -> None:
        self.calls = 0
        self.paused = False
        self.snapshot: FakeSnapshot | None = None

    async def read_turn(self) -> FakeSnapshot:
        self.calls += 1
        if self.paused:
            raise HistoryPaused("paused")
        self.snapshot = FakeSnapshot()
        return self.snapshot


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
) -> tuple[GroupStateService, GroupStateStore, FakeProjector]:
    store = GroupStateStore(tmp_path / "group-state.db")
    projector = FakeProjector()
    service = GroupStateService(
        store=store,
        registry=registry,
        settings=settings or _settings(),
        history_projector=projector,
        sources=SourceReaders(),
        clock_ms=lambda: clock[0],
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
async def test_cas_conflict_retries_once(tmp_path) -> None:
    clock = [1_000_000]
    service, store, _ = _service(
        tmp_path, registry=_registry(StubContributor("presence")), clock=clock
    )
    real_put = store.put
    raced = [False]

    def racing_put(state, *, expected_revision):
        if not raced[0]:
            raced[0] = True
            competitor = GroupState(
                channel=state.channel,
                chat_id=state.chat_id,
                schema_version=1,
                revision=1,
                history_cursor="competing",
                updated_ms=clock[0],
                sections={},
            )
            assert real_put(competitor, expected_revision=None) is True
            return False
        return real_put(state, expected_revision=expected_revision)

    store.put = racing_put  # type: ignore[method-assign]
    try:
        state = await service.rebuild("whatsapp", CHAT_A)
        assert state is not None
        assert state.revision == 2
        assert store.get("whatsapp", CHAT_A) == state
    finally:
        store.close()
