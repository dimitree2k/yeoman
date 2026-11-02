"""The as-of state builder: frozen history and time-filtered source reads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.group_state.as_of import (
    UNAVAILABLE_IN_REPLAY,
    AsOfSources,
    build_state_as_of,
)
from yeoman_gateway.group_state.contract import ContributorRegistry, SourceReaders
from yeoman_gateway.group_state.models import Section
from yeoman_gateway.group_state.store import GroupStateStore

AT = 10_000_000
CHAT = "120000000000000042@g.us"


class FakeSpeakups:
    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        self.attempts = attempts

    async def judge_attempts_since(
        self, *, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self.attempts
            if item["chat_id"] == chat_id and int(item["created_at_ms"]) >= since_ms
        ]


class FakeProcessing:
    def __init__(
        self,
        *,
        replies: list[dict[str, Any]] | None = None,
        reactions: list[Any] | None = None,
    ) -> None:
        self.replies = replies or []
        self.reactions = reactions or []

    def short_reply_decisions_since(
        self, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        return [item for item in self.replies if int(item["created_ms"]) >= since_ms]

    def recent_reactions(
        self, *, channel: str, chat_id: str, since_ms: int, limit: int
    ) -> list[Any]:
        return [item for item in self.reactions if int(item.created_ms) >= since_ms][:limit]

    def thread_for_message(self, native_id: str) -> Any | None:
        return None


class AsOfDecisions:
    """A decisions reader that can answer what was open at a time."""

    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.calls: list[tuple[str, int]] = []

    def list_open_as_of(self, chat_id: str, at_ms: int) -> list[Any]:
        self.calls.append((chat_id, at_ms))
        return list(self.items)


class CurrentDecisions:
    """The live store shape: only the current open set, no as-of question."""

    def list_open(self, chat_id: str) -> list[Any]:
        return []


class StubContributor:
    def __init__(self, key: str, *, fail: bool = False) -> None:
        self.key = key
        self.schema_version = 1
        self.trust = "runtime"
        self.layer = "live"
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
            data={"now_ms": ctx.now_ms, "polls": ctx.sources.polls, "cron": ctx.sources.cron},
        )

    def migrate(self, from_version: int, data: Any) -> Any:
        return data


def _registry(*contributors: StubContributor) -> ContributorRegistry:
    registry = ContributorRegistry()
    for contributor in contributors:
        registry.register(contributor)
    return registry


@pytest.mark.asyncio
async def test_as_of_drops_later_judge_attempts() -> None:
    sources = SourceReaders(
        speakups=FakeSpeakups(
            [
                {"attempt_id": "before", "chat_id": CHAT, "created_at_ms": AT - 1},
                {"attempt_id": "after", "chat_id": CHAT, "created_at_ms": AT + 1},
            ]
        )
    )
    readers = AsOfSources(sources, AT).readers()
    found = await readers.speakups.judge_attempts_since(
        channel="whatsapp", chat_id=CHAT, since_ms=0
    )
    assert [item["attempt_id"] for item in found] == ["before"]
    assert readers.polls is None
    assert readers.cron is None


@pytest.mark.asyncio
async def test_as_of_drops_later_reactions_and_short_replies() -> None:
    sources = SourceReaders(
        processing=FakeProcessing(
            replies=[
                {
                    "message_id": "m1",
                    "created_ms": AT - 2,
                    "updated_ms": AT - 1,
                },
                {
                    "message_id": "m2",
                    "created_ms": AT - 2,
                    "updated_ms": AT + 1,
                },
            ],
            reactions=[
                SimpleNamespace(created_ms=AT - 1),
                SimpleNamespace(created_ms=AT + 1),
            ],
        )
    )
    processing = AsOfSources(sources, AT).readers().processing
    assert [
        item["message_id"] for item in processing.short_reply_decisions_since("whatsapp", CHAT, 0)
    ] == ["m1"]
    assert (
        len(processing.recent_reactions(channel="whatsapp", chat_id=CHAT, since_ms=0, limit=10))
        == 1
    )


@pytest.mark.asyncio
async def test_as_of_decisions_open_at_time_only() -> None:
    decisions = AsOfDecisions(
        [
            SimpleNamespace(id="before", created_at_ms=AT - 1),
            SimpleNamespace(id="after", created_at_ms=AT + 1),
        ]
    )
    readers = AsOfSources(SourceReaders(decisions=decisions), AT).readers()
    assert readers.decisions is not None
    found = readers.decisions.list_open_as_of(CHAT, AT)
    assert [item.id for item in found] == ["before"]
    assert decisions.calls == [(CHAT, AT)]


def test_as_of_decisions_reader_without_as_of_is_unavailable() -> None:
    readers = AsOfSources(SourceReaders(decisions=CurrentDecisions()), AT).readers()
    assert readers.decisions is None
    assert UNAVAILABLE_IN_REPLAY == frozenset({"commitments", "conversation_labels"})


@pytest.mark.asyncio
async def test_as_of_state_is_never_written(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_put(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("a replay state is never stored")

    monkeypatch.setattr(GroupStateStore, "put", fail_put)
    state = await build_state_as_of(
        registry=_registry(StubContributor("presence")),
        channel="whatsapp",
        chat_id=CHAT,
        at_ms=AT,
        history=object(),
        sources=SourceReaders(),
        settings=SimpleNamespace(),
        consciousness=None,
    )
    assert state.revision == 0
    assert state.updated_ms == AT
    assert state.sections["presence"].data["now_ms"] == AT


@pytest.mark.asyncio
async def test_as_of_failed_section_is_skipped() -> None:
    state = await build_state_as_of(
        registry=_registry(StubContributor("presence"), StubContributor("budgets", fail=True)),
        channel="whatsapp",
        chat_id=CHAT,
        at_ms=AT,
        history=object(),
        sources=SourceReaders(),
        settings=SimpleNamespace(),
        consciousness=None,
    )
    assert set(state.sections) == {"presence"}
    assert state.channel == "whatsapp"
    assert state.chat_id == CHAT


@pytest.mark.asyncio
async def test_as_of_skips_sections_unavailable_in_replay() -> None:
    from yeoman_gateway.group_state.as_of import UNAVAILABLE_IN_REPLAY

    built: list[str] = []

    class Recording:
        schema_version = 1
        trust = "runtime"
        layer = "live"

        def __init__(self, key: str) -> None:
            self.key = key

        def applies_to(self, channel: str, chat_kind: str) -> bool:
            return True

        async def build(self, ctx: Any) -> Any:
            built.append(self.key)
            raise RuntimeError("not needed for this test")

        def migrate(self, from_version: int, data: Any) -> Any:
            return data

    registry = ContributorRegistry()
    for key in ("presence", *sorted(UNAVAILABLE_IN_REPLAY)):
        registry.register(Recording(key))
    await build_state_as_of(
        registry=registry,
        channel="whatsapp",
        chat_id="group@g.us",
        at_ms=1_000,
        history=None,
        sources=SourceReaders(
            speakups=None, processing=None, decisions=None, polls=None, cron=None
        ),
        settings=None,
        consciousness=None,
    )
    assert built == ["presence"]
