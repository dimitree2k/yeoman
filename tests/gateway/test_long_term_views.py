"""Reader layer: people and past blocks inside the reply and writer views.

Synthetic only.  The reader gets a fake knowledge service and a history-queries
provider that only answers the membership query, so the tests can prove the reply
path never scans history.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.agent.untrusted import EXTERNAL_END, EXTERNAL_START
from yeoman_gateway.group_state.contract import ReaderContext
from yeoman_gateway.group_state.models import Item
from yeoman_gateway.group_state.view_reader import GroupStateViewReader
from yeoman_gateway.group_state.views import (
    ViewTarget,
    render_past_block,
    select_turn_people,
)
from yeoman_gateway.knowledge.models import PersonNoteLine, PersonNoteView
from yeoman_shared.config.loader import camel_to_snake, snake_to_camel
from yeoman_shared.config.schema import Config, GroupStateConfig

from tests.gateway.group_state.test_views import (
    CHAT,
    LULL,
    M1,
    NOW,
    _full_state,
    _section,
    _state,
)

TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
MARIA = "whatsapp:4910000000004"
PEOPLE = frozenset({TOM, ALEX, MARIA})


class _Store:
    def __init__(self, state: Any) -> None:
        self.state = state

    def get(self, channel: str, chat_id: str) -> Any:
        return self.state


class _Queries:
    """Only the membership read exists; anything else is a history scan."""

    def __init__(self, members: frozenset[str] = PEOPLE) -> None:
        self._members = members
        self.calls: list[str] = []

    def members(self, *, chat_id: str, at_ms: int) -> Any:
        self.calls.append("members")
        return SimpleNamespace(status="known", members=self._members)

    def member_principal(self, identifier: str, members: frozenset[str], *, at_ms: int) -> Any:
        self.calls.append("member_principal")
        token = identifier.partition(":")[2] or identifier
        for principal in members:
            if principal.partition(":")[2] == token.split("@", 1)[0]:
                return principal
        return None

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"reply path touched history: {name}")


class _Knowledge:
    policy_revision = 1

    def __init__(self, notes: tuple[PersonNoteView, ...] = ()) -> None:
        self.notes = notes
        self.person_notes_calls: list[tuple[str, ...]] = []
        self.names = {"p-tom": "Tom", "p-alex": "Alex", "p-maria": "Maria"}
        self.principals = {TOM: "p-tom", ALEX: "p-alex", MARIA: "p-maria"}

    def person_for_principal(self, principal: str) -> str | None:
        return self.principals.get(str(principal))

    def canonical_id(self, person_id: str) -> str:
        return str(person_id)

    def display_name(
        self, person_id: str, *, context: Any = None, for_group: bool = False
    ) -> str | None:
        return self.names.get(str(person_id))

    def person_notes(self, *, person_ids: tuple[str, ...], **kwargs: Any) -> tuple[Any, ...]:
        wanted = set(person_ids)
        self.person_notes_calls.append(tuple(person_ids))
        return tuple(note for note in self.notes if note.person_id in wanted)


def _note(
    person_id: str,
    text: str,
    *,
    status: str = "active",
    author: str | None = None,
    note_id: str | None = None,
) -> PersonNoteView:
    return PersonNoteView(
        note_id=note_id or f"note:{person_id}",
        channel="whatsapp",
        chat_id=CHAT,
        person_id=person_id,
        status=status,
        lines=(
            PersonNoteLine(
                kind="stated",
                text=text,
                author=author,
                refs=("synthetic-statement",),
            ),
        ),
    )


def _conversation_item(
    conversation_id: str,
    *,
    ended_ms: int,
    lines: tuple[str, ...],
) -> Item:
    return Item(
        id=f"{conversation_id}:summary",
        kind="conversation_summary",
        data={
            "conversation_id": conversation_id,
            "started_ms": ended_ms - 600_000,
            "ended_ms": ended_ms,
            "participants": ["c1"],
            "lines": list(lines),
        },
        sources=(),
        source_fingerprints={},
        produced_at_ms=ended_ms,
        ttl_ms=7 * 86_400_000,
        trust="derived",
        conversation_id=conversation_id,
    )


def _week_item(label: str, *, lines: tuple[str, ...]) -> Item:
    return Item(
        id=f"week:{label}",
        kind="week_summary",
        data={"iso_week": label, "lines": list(lines), "depends_on": []},
        sources=(),
        source_fingerprints={},
        produced_at_ms=NOW - 86_400_000,
        ttl_ms=7 * 86_400_000,
        trust="derived",
    )


def _past_state() -> Any:
    state = _full_state()
    items = (
        _conversation_item("conv-old", ended_ms=NOW - 3_600_000, lines=("they planned a trip",)),
        _conversation_item("conv-older", ended_ms=NOW - 7_200_000, lines=("they moved house",)),
        _week_item("2026-W05", lines=("the week they picked a date",)),
    )
    return _state({**state.sections, "past": _section("past", {}, items=items)})


def _settings(*, people: str = "on", past: str = "on", mode: str = "on") -> GroupStateConfig:
    return GroupStateConfig.model_validate(
        {
            "mode": "shadow",
            "chats": [f"whatsapp:{CHAT}"],
            "views": {
                "reply": {
                    "mode": mode,
                    "chats": [f"whatsapp:{CHAT}"],
                    "maxChars": 4_000,
                    "people": people,
                    "past": past,
                }
            },
        }
    )


def _reader(*, principal: str = TOM) -> ReaderContext:
    return ReaderContext(
        principal=principal,
        is_owner=False,
        channel="whatsapp",
        chat_id=CHAT,
        audience="group",
    )


def _render(
    *,
    settings: GroupStateConfig | None = None,
    knowledge: Any | None = None,
    queries: Any | None = None,
    target: ViewTarget | None = None,
    state: Any | None = None,
) -> tuple[str | None, Any]:
    reader = GroupStateViewReader(
        store=_Store(state if state is not None else _past_state()),  # type: ignore[arg-type]
        settings=settings or _settings(),
        lull_ms=LULL,
        clock_ms=lambda: NOW,
        knowledge=knowledge,
        history_queries=(lambda: queries) if queries is not None else None,
    )
    return reader.render(
        "reply",
        reader=_reader(),
        target=target or ViewTarget(message_id=M1, sender_principal=TOM),
    )


def test_people_not_in_turn_not_rendered() -> None:
    knowledge = _Knowledge(
        (
            _note("p-tom", "Tom: likes charts"),
            _note("p-maria", "Maria: keeps a boat"),
        )
    )

    text, _ = _render(
        knowledge=knowledge,
        queries=_Queries(),
        target=ViewTarget(
            message_id=M1,
            sender_principal=TOM,
            mentioned_principals=(ALEX,),
        ),
    )

    assert text is not None
    assert "Tom" in text and "likes charts" in text
    assert "Maria" not in text and "boat" not in text


def test_stale_note_not_rendered() -> None:
    knowledge = _Knowledge(
        (
            _note("p-tom", "Tom: likes charts"),
            _note("p-alex", "Alex: stale fact", status="stale"),
        )
    )

    text, _ = _render(
        knowledge=knowledge,
        queries=_Queries(),
        target=ViewTarget(
            message_id=M1,
            sender_principal=TOM,
            mentioned_principals=(ALEX,),
        ),
    )

    assert text is not None
    assert "likes charts" in text
    assert "stale fact" not in text


def test_blocks_inside_envelope() -> None:
    knowledge = _Knowledge((_note("p-tom", "Tom: likes charts"),))

    text, _ = _render(knowledge=knowledge, queries=_Queries())

    assert text is not None
    start = text.index(EXTERNAL_START)
    end = text.index(EXTERNAL_END)
    for marker in ("[People here]", "[Earlier in this group]", "they planned a trip"):
        assert start < text.index(marker) < end


def test_flags_off_prompt_byte_identical() -> None:
    class _ExplodingKnowledge:
        policy_revision = 1

        def person_for_principal(self, principal: str) -> str | None:
            raise AssertionError("flags off must not read Knowledge")

    class _ExplodingQueries(_Queries):
        def members(self, *, chat_id: str, at_ms: int) -> Any:
            raise AssertionError("flags off must not read history")

    text, rendered = _render(
        settings=_settings(people="off", past="off", mode="on"),
        knowledge=_ExplodingKnowledge(),
        queries=_ExplodingQueries(),
    )
    assert text is not None and rendered is not None
    assert "[People here]" not in text
    assert "[Earlier in this group]" not in text

    # The live wiring never adds the long-term content when both flags are off.
    from yeoman_gateway.agent.context import ContextBuilder

    plain: dict[str, Any] = {"ambient_context_window": []}
    empty = {**plain, "group_state_block": None}
    assert ContextBuilder._with_group_state("current", plain) == ContextBuilder._with_group_state(  # noqa: SLF001
        "current", empty
    )


def test_config_view_flags_default_off_and_camel_case_round_trip() -> None:
    views = Config().processing.group_state.views
    assert views.reply.people == "off" and views.reply.past == "off"
    assert views.writer.people == "off" and views.writer.past == "off"

    settings = GroupStateConfig.model_validate({"views": {"reply": {"people": "on", "past": "on"}}})
    assert settings.views.reply.people == "on"
    assert settings.views.reply.past == "on"
    # The flag names are single words, so the config key round trip keeps them.
    for key in ("people", "past"):
        assert camel_to_snake(snake_to_camel(key)) == key


def test_reader_does_no_history_reads() -> None:
    queries = _Queries()
    knowledge = _Knowledge((_note("p-tom", "Tom: likes charts"),))

    text, _ = _render(knowledge=knowledge, queries=queries)

    assert text is not None and "[People here]" in text
    assert set(queries.calls) <= {"members", "member_principal"}


def test_select_turn_people_orders_and_never_arvid() -> None:
    people = select_turn_people(
        ViewTarget(
            message_id=M1,
            sender_principal=TOM,
            reply_to_principal=ALEX,
            mentioned_principals=(MARIA, "arvid"),
        ),
        ("c1", "c2", "c3"),
        cap=5,
    )

    assert people == (TOM, ALEX, MARIA, "c1", "c2")
    assert "arvid" not in select_turn_people(ViewTarget(message_id=M1), ("arvid",))


def test_past_block_linked_first_then_last_three_then_week() -> None:
    items = (
        _conversation_item("linked", ended_ms=NOW - 10_000_000, lines=("the linked one",)),
        _conversation_item("new", ended_ms=NOW - 1_000, lines=("the newest one",)),
        _conversation_item("old", ended_ms=NOW - 5_000, lines=("the older one",)),
        _conversation_item("oldest", ended_ms=NOW - 9_000, lines=("the oldest one",)),
        _conversation_item("too-old", ended_ms=NOW - 20_000, lines=("the extra one",)),
        _week_item("2026-W05", lines=("the week line",)),
    )

    text = render_past_block(
        items,
        linked_conversation_ids=("linked",),
        older_than_ms=NOW,
        now_ms=NOW,
        max_chars=4_000,
    )

    assert text.startswith("[Earlier in this group]")
    assert text.index("the linked one") < text.index("the newest one")
    assert "the extra one" not in text
    assert "the week line" in text


@pytest.mark.perf
def test_render_p95_under_5ms() -> None:
    from yeoman_gateway.group_state.render import RenderedState

    state = _past_state()
    knowledge = _Knowledge(
        (
            _note("p-tom", "Tom: likes charts"),
            _note("p-alex", "Alex: plays chess"),
        )
    )
    reader = GroupStateViewReader(
        store=_Store(state),  # type: ignore[arg-type]
        settings=_settings(),
        lull_ms=LULL,
        clock_ms=lambda: NOW,
        knowledge=knowledge,
        history_queries=lambda: _Queries(),
    )
    samples: list[float] = []
    for _ in range(120):
        started = time.perf_counter()
        text, rendered = reader.render(
            "reply",
            reader=_reader(),
            target=ViewTarget(message_id=M1, sender_principal=TOM),
        )
        samples.append((time.perf_counter() - started) * 1000)
        assert isinstance(rendered, RenderedState) and text
    samples.sort()
    p95 = samples[int(0.95 * (len(samples) - 1))]
    assert p95 <= 5.0


def test_notes_built_by_runner_are_found_for_the_sender_principal(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.processing.model_route import RouteReply
    from yeoman_gateway.processing.person_notes import PersonNotesRunner

    from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

    h = ProjectedKnowledgeHarness(tmp_path)
    try:
        tom, alex = h.two_people()
        principal = h.principal_for(tom)
        h.source(tom, chat="group-a", audience={principal, h.principal_for(alex)})

        class _Client:
            async def chat_with_usage(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
                return RouteReply(
                    content=json.dumps(
                        {"lines": [{"text": "likes synthetic charts", "refs": ["m1"]}]}
                    ),
                    usage={"prompt_tokens": 4, "completion_tokens": 4},
                    model="fake",
                )

        runner = PersonNotesRunner(
            knowledge=h.service,
            history=h.history,
            client_factory=lambda: _Client(),
        )
        now_ms = h.clock.now_ms()

        async def _build() -> None:
            plan = await runner.plan(
                "120363000000000001@g.us", tom, now_ms=now_ms, channel="whatsapp"
            )
            assert plan is not None
            outcome = await runner.execute(plan)
            assert outcome.note is not None

        import asyncio

        asyncio.run(_build())

        base = _full_state()
        state = _state(
            {
                **base.sections,
                "presence": _section(
                    "presence", base.sections["presence"].data, produced_at_ms=now_ms
                ),
                "budgets": _section(
                    "budgets", base.sections["budgets"].data, produced_at_ms=now_ms
                ),
            },
            updated_ms=now_ms,
            chat_id="120363000000000001@g.us",
        )
        settings = _settings()
        settings.chats = ["whatsapp:120363000000000001@g.us"]
        settings.views.reply.chats = ["whatsapp:120363000000000001@g.us"]
        with h.history_scope() as snapshot:
            queries = HistoryQueries(snapshot)
            reader = GroupStateViewReader(
                store=_Store(state),  # type: ignore[arg-type]
                settings=settings,
                lull_ms=LULL,
                clock_ms=lambda: now_ms,
                knowledge=h.service,
                history_queries=lambda: queries,
            )
            text, _ = reader.render(
                "reply",
                reader=ReaderContext(
                    principal=principal,
                    is_owner=False,
                    channel="whatsapp",
                    chat_id="120363000000000001@g.us",
                    audience="group",
                ),
                target=ViewTarget(message_id=M1, sender_principal=principal),
            )
        assert text is not None
        assert "synthetic charts" in text
    finally:
        h.close()
