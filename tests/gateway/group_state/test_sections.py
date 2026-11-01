"""The six deterministic group-state sections, built from a synthetic snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.group_state.contract import BuildContext, SourceReaders
from yeoman_gateway.group_state.sections import default_contributors
from yeoman_gateway.group_state.sections.arvid_actions import ArvidActionsSection
from yeoman_gateway.group_state.sections.budgets import BudgetsSection
from yeoman_gateway.group_state.sections.commitments import CommitmentsSection
from yeoman_gateway.group_state.sections.conversations import ConversationsSection
from yeoman_gateway.group_state.sections.people_activity import PeopleActivitySection
from yeoman_gateway.group_state.sections.presence import PresenceSection
from yeoman_gateway.history.conversations import conversation_id

CHAT = "group@g.us"
HOUR = 3_600_000
MINUTE = 60_000
DAY = 24 * HOUR


def row(
    message_id: str,
    *,
    chat_id: str = CHAT,
    sent_ms: int,
    direction: str = "in",
    contact: str | None = None,
    native: str | None = None,
    reply_to: str | None = None,
    text: str = "fixture message text",
    deleted: int = 0,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel": "whatsapp",
        "chat_id": chat_id,
        "native_message_id": native or f"native-{message_id}",
        "direction": direction,
        "sent_ms": sent_ms,
        "sender_contact_id": contact,
        "sender_identifier": contact,
        "reply_to_native_id": reply_to,
        "text": text,
        "deleted": deleted,
    }


class FakeHistory:
    """The narrow HistoryQueries surface the sections use."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        assistant: tuple[str, ...] = ("arvid",),
        names: dict[str, str] | None = None,
        reactions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows
        self.assistant = frozenset(assistant)
        self.names = names or {}
        self.reaction_events = reactions or []

    def recent(
        self,
        *,
        chat_id: str,
        limit: int,
        before_id: str | None = None,
        after_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        visible = [
            item
            for item in self.rows
            if item["chat_id"] == chat_id
            and item.get("deleted", 0) == 0
            and (after_ms is None or (item.get("sent_ms") or 0) > after_ms)
            and (until_ms is None or (item.get("sent_ms") or 0) <= until_ms)
        ]
        visible.sort(key=lambda item: (item.get("sent_ms") or 0, item["message_id"]))
        if before_id is not None:
            anchor = next((item for item in self.rows if item["message_id"] == before_id), None)
            if anchor is None:
                return []
            edge = (anchor.get("sent_ms") or 0, before_id)
            visible = [
                item for item in visible if (item.get("sent_ms") or 0, item["message_id"]) < edge
            ]
        return visible[-max(1, int(limit)) :]

    def reactions(
        self, *, message_ids: tuple[str, ...], until_ms: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        wanted = set(message_ids)
        return [
            event
            for event in self.reaction_events
            if event["target_message_id"] in wanted and int(event["occurred_ms"]) <= until_ms
        ][:limit]

    def assistant_contact_ids(self) -> frozenset[str]:
        return self.assistant

    def contact_names(self, contact_ids: Any) -> dict[str, str]:
        return {item: self.names[item] for item in contact_ids if item in self.names}


@dataclass
class FakeSpeakups:
    attempts: list[dict[str, Any]]

    async def judge_attempts_since(
        self, *, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        return [item for item in self.attempts if int(item["created_at_ms"]) >= since_ms]


@dataclass
class FakeProcessing:
    short_replies: list[dict[str, Any]] | None = None
    reactions: list[Any] | None = None
    threads: dict[str, Any] | None = None

    def short_reply_decisions_since(
        self, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        return [item for item in self.short_replies or [] if int(item["updated_ms"]) >= since_ms]

    def recent_reactions(
        self, *, channel: str, chat_id: str, since_ms: int, limit: int
    ) -> tuple[Any, ...]:
        return tuple(item for item in self.reactions or [] if int(item.created_ms) >= since_ms)[
            :limit
        ]

    def thread_for_message(self, native_id: str) -> Any | None:
        return (self.threads or {}).get(native_id)


class FakeDecisions:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def list_open(self, chat_id: str) -> list[Any]:
        return list(self.items)


class FakePolls:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def open_for(self, chat_id: str, now_ms: int) -> list[dict[str, Any]]:
        return [dict(item) for item in self.items]


class FakeCron:
    def __init__(self, jobs: list[Any]) -> None:
        self.jobs = jobs

    def list_jobs(self, *, chat_id: str | None = None, **_: Any) -> list[Any]:
        return list(self.jobs)


SETTINGS = SimpleNamespace(window_hours=24, people_window_days=7, actions_limit=20)
CONSCIOUSNESS = SimpleNamespace(
    lull_silence_minutes=25,
    burst_threshold_messages=8,
    burst_window_minutes=15,
)


def context(
    history: FakeHistory,
    *,
    now_ms: int,
    speakups: Any = None,
    processing: Any = None,
    decisions: Any = None,
    polls: Any = None,
    cron: Any = None,
) -> BuildContext:
    return BuildContext(
        channel="whatsapp",
        chat_id=CHAT,
        now_ms=now_ms,
        history=history,
        sources=SourceReaders(
            speakups=speakups,
            processing=processing,
            decisions=decisions,
            polls=polls,
            cron=cron,
        ),
        settings=SETTINGS,
        consciousness=CONSCIOUSNESS,
    )


async def build(contributor: Any, ctx: BuildContext) -> Any:
    return await contributor.build(ctx)


def test_default_contributor_order() -> None:
    assert [item.key for item in default_contributors()] == [
        "presence",
        "arvid_actions",
        "conversations",
        "commitments",
        "budgets",
        "people_activity",
    ]


@pytest.mark.asyncio
async def test_presence_section() -> None:
    now = 10_000_000
    history = FakeHistory(
        [
            row("m1", sent_ms=now - 10 * MINUTE, contact="c1"),
            row("m2", sent_ms=now - 15 * MINUTE, contact="c1"),
            row("m3", sent_ms=now - 25 * MINUTE, contact="c2"),
            row("m4", sent_ms=now - 16 * MINUTE, direction="out", contact="arvid"),
            row("m5", sent_ms=now - 66 * MINUTE, contact="c3"),
        ],
        names={"c1": "Alice", "c2": "Bob", "c3": "Cara"},
    )
    section = await build(PresenceSection(), context(history, now_ms=now))
    assert section.key == "presence"
    assert section.layer == "live"
    assert section.trust == "runtime"
    assert section.ttl_ms == 10 * MINUTE
    assert section.period_start_ms is None
    assert section.source_cursor == "m1"
    assert section.sources == ("m5", "m3", "m4", "m2", "m1")
    assert section.data == {
        "active_30m": [
            {"contact_id": "c1", "name": "Alice", "messages": 2},
            {"contact_id": "c2", "name": "Bob", "messages": 1},
        ],
        "active_120m": [
            {"contact_id": "c1", "name": "Alice", "messages": 2},
            {"contact_id": "c2", "name": "Bob", "messages": 1},
            {"contact_id": "c3", "name": "Cara", "messages": 1},
        ],
        "messages_30m": 3,
        "messages_120m": 4,
        "last_message_ms": now - 10 * MINUTE,
        "arvid_last_ms": now - 16 * MINUTE,
    }


@pytest.mark.asyncio
async def test_arvid_actions_section() -> None:
    now = 10_000_000
    history = FakeHistory(
        [
            row("m10", sent_ms=now - 100_000, direction="out", contact="arvid", reply_to="n5"),
            row("m11", sent_ms=now - 200_000, direction="out", contact="arvid"),
            row("m12", sent_ms=now - 300_000, contact="c1"),
        ]
    )
    speakups = FakeSpeakups(
        [
            {
                "attempt_id": "a1",
                "opportunity_id": "o1",
                "created_at_ms": now - 300_000,
                "outcome": "comment",
                "detail_code": "",
                "hourly_limit": 6,
            },
            {
                "attempt_id": "a2",
                "opportunity_id": "o2",
                "created_at_ms": now - 400_000,
                "outcome": "provider_error",
                "detail_code": "timeout",
                "hourly_limit": 6,
            },
        ]
    )
    processing = FakeProcessing(
        short_replies=[
            {
                "message_id": "m12",
                "status": "react",
                "emoji": "👍",
                "created_ms": now - 500_000,
                "updated_ms": now - 500_000,
            },
            {
                "message_id": "m12",
                "status": "failed",
                "emoji": None,
                "created_ms": now - 600_000,
                "updated_ms": now - 600_000,
            },
        ],
        reactions=[SimpleNamespace(emoji="👍", created_ms=now - 700_000) for _ in range(2)],
    )
    section = await build(
        ArvidActionsSection(),
        context(history, now_ms=now, speakups=speakups, processing=processing),
    )
    assert section.layer == "live"
    assert section.ttl_ms == 24 * HOUR
    assert section.sources == ("m11", "m10")
    assert section.source_cursor == "m10"
    assert section.data == {
        "items": [
            {
                "kind": "sent",
                "message_id": "m10",
                "reply_to_native_id": "n5",
                "at_ms": now - 100_000,
            },
            {
                "kind": "sent",
                "message_id": "m11",
                "reply_to_native_id": None,
                "at_ms": now - 200_000,
            },
            {
                "kind": "judge",
                "outcome": "comment",
                "detail_code": "",
                "at_ms": now - 300_000,
                "opportunity_id": "o1",
            },
            {
                "kind": "judge",
                "outcome": "provider_error",
                "detail_code": "timeout",
                "at_ms": now - 400_000,
                "opportunity_id": "o2",
            },
            {
                "kind": "short_reply",
                "status": "react",
                "emoji": "👍",
                "at_ms": now - 500_000,
            },
            {
                "kind": "short_reply",
                "status": "failed",
                "emoji": None,
                "at_ms": now - 600_000,
            },
        ],
        "counts_24h": {
            "sent": 2,
            "comments": 1,
            "reactions": 2,
            "silences": 0,
            "failures": 1,
        },
    }


@pytest.mark.asyncio
async def test_conversations_section() -> None:
    now = 12_000_000
    history = FakeHistory(
        [
            row("m1", sent_ms=8_000_000, contact="c1", native="n1"),
            row("m2", sent_ms=8_100_000, contact="c2", native="n2"),
            row(
                "m3",
                sent_ms=8_200_000,
                direction="out",
                contact="arvid",
                native="n3",
                reply_to="n2",
            ),
            row("m4", sent_ms=8_300_000, contact="c1", native="n4"),
            row("m5", sent_ms=10_100_000, contact="c2", native="n5"),
            row("m6", sent_ms=10_200_000, contact="c1", native="n6"),
        ]
    )
    processing = FakeProcessing(threads={"n3": ("t1", CHAT)})
    section = await build(
        ConversationsSection(), context(history, now_ms=now, processing=processing)
    )
    first = conversation_id(CHAT, "m1")
    second = conversation_id(CHAT, "m5")
    assert section.layer == "conversation"
    assert section.ttl_ms == 2 * HOUR
    assert section.period_start_ms == now - 24 * HOUR
    assert section.period_end_ms == now
    assert section.sources == ("m1", "m2", "m3", "m4", "m5", "m6")
    assert section.source_cursor == "m6"
    assert section.data == {
        "conversations": [
            {
                "conversation_id": first,
                "start_ms": 8_000_000,
                "last_ms": 8_300_000,
                "state": "ended",
                "messages": 4,
                "participants": ["c1", "c2"],
                "burst": False,
                "arvid_messages": 1,
                "arvid_addressed": True,
                "thread_ids": ["t1"],
                "first_message_id": "m1",
                "last_message_id": "m4",
            },
            {
                "conversation_id": second,
                "start_ms": 10_100_000,
                "last_ms": 10_200_000,
                "state": "ended",
                "messages": 2,
                "participants": ["c2", "c1"],
                "burst": False,
                "arvid_messages": 0,
                "arvid_addressed": False,
                "thread_ids": [],
                "first_message_id": "m5",
                "last_message_id": "m6",
            },
        ]
    }


@pytest.mark.asyncio
async def test_commitments_section() -> None:
    now = 10_000_000
    history = FakeHistory([])
    decisions = FakeDecisions(
        [
            SimpleNamespace(
                id="d1", created_by="c1", due_at_ms=now + HOUR, status="open", text="secret"
            )
        ]
    )
    polls = FakePolls(
        [{"id": "client-1", "poll_id": "p1", "opened_ms": now - HOUR, "closed_ms": None}]
    )
    cron = FakeCron(
        [
            SimpleNamespace(
                id="j1",
                schedule=SimpleNamespace(at_ms=now + 2 * HOUR),
                state=SimpleNamespace(next_run_at_ms=now + 2 * HOUR),
            )
        ]
    )
    section = await build(
        CommitmentsSection(),
        context(history, now_ms=now, decisions=decisions, polls=polls, cron=cron),
    )
    assert section.layer == "live"
    assert section.ttl_ms == HOUR
    assert section.sources == ()
    assert section.source_cursor == ""
    assert section.data == {
        "decisions": [{"id": "d1", "created_by": "c1", "due_at_ms": now + HOUR, "status": "open"}],
        "polls": [{"poll_id": "p1", "opened_ms": now - HOUR, "closed_ms": None}],
        "reminders": [{"job_id": "j1", "at_ms": now + 2 * HOUR}],
    }


@pytest.mark.asyncio
async def test_budgets_section() -> None:
    now = 10_000_000
    history = FakeHistory([])
    speakups = FakeSpeakups(
        [
            {
                "attempt_id": "old",
                "opportunity_id": "o0",
                "created_at_ms": now - 2 * HOUR,
                "outcome": "comment",
                "detail_code": "",
                "hourly_limit": 4,
            },
            {
                "attempt_id": "a1",
                "opportunity_id": "o1",
                "created_at_ms": now - 25 * MINUTE,
                "outcome": "comment",
                "detail_code": "",
                "hourly_limit": 6,
            },
            {
                "attempt_id": "a2",
                "opportunity_id": "o2",
                "created_at_ms": now - 10 * MINUTE,
                "outcome": "silence",
                "detail_code": "",
                "hourly_limit": 6,
            },
        ]
    )
    processing = FakeProcessing(
        reactions=[SimpleNamespace(emoji="👍", created_ms=now - 5 * MINUTE) for _ in range(2)]
    )
    section = await build(
        BudgetsSection(),
        context(history, now_ms=now, speakups=speakups, processing=processing),
    )
    assert section.layer == "live"
    assert section.ttl_ms == 10 * MINUTE
    assert section.data == {
        "judge_calls_last_hour": 2,
        "judge_hourly_limit": 6,
        "last_comment_ms": now - 25 * MINUTE,
        "comments_last_30m": 1,
        "reactions_last_30m": 2,
    }


@pytest.mark.asyncio
async def test_people_activity_section() -> None:
    now = 200_000_000
    history = FakeHistory(
        [
            row("m1", sent_ms=199_900_000, contact="c1", reply_to="n3"),
            row("m2", sent_ms=199_800_000, contact="c1", native="n2"),
            row("m3", sent_ms=199_700_000, contact="c2", native="n3"),
            row("m4", sent_ms=199_600_000, direction="out", contact="arvid", native="n4"),
            row("m5", sent_ms=100_000_000, contact="c1"),
            row("m6", sent_ms=100_100_000, contact="c3"),
        ],
        names={"c1": "Alice", "c2": "Bob", "c3": "Cara"},
        reactions=[
            {
                "event_id": "r1",
                "target_message_id": "m1",
                "actor_contact_id": "c2",
                "occurred_ms": now - 10_000,
                "emoji": "👍",
                "removed": 0,
            },
            {
                "event_id": "r2",
                "target_message_id": "m2",
                "actor_contact_id": "c1",
                "occurred_ms": now - 9_000,
                "emoji": "❤️",
                "removed": 0,
            },
            {
                "event_id": "r3",
                "target_message_id": "m3",
                "actor_contact_id": "c1",
                "occurred_ms": now - 8_000,
                "emoji": "👍",
                "removed": 1,
            },
        ],
    )
    # Arvid's own native id is n4, so m1 (reply to n3) is not a reply to Arvid.
    section = await build(PeopleActivitySection(), context(history, now_ms=now))
    assert section.layer == "day"
    assert section.ttl_ms == DAY
    assert section.period_start_ms == now - 7 * DAY
    assert section.period_end_ms == now
    assert section.source_cursor == "m1"
    assert section.sources == ("m5", "m6", "m3", "m2", "m1")
    assert section.data == {
        "people": [
            {
                "contact_id": "c1",
                "name": "Alice",
                "messages_24h": 2,
                "messages_7d": 3,
                "last_seen_ms": 199_900_000,
                "conversations_24h": 1,
                "reactions_given_7d": 1,
                "reactions_received_7d": 2,
                "replies_to_arvid_7d": 0,
            },
            {
                "contact_id": "c2",
                "name": "Bob",
                "messages_24h": 1,
                "messages_7d": 1,
                "last_seen_ms": 199_700_000,
                "conversations_24h": 1,
                "reactions_given_7d": 1,
                "reactions_received_7d": 0,
                "replies_to_arvid_7d": 0,
            },
            {
                "contact_id": "c3",
                "name": "Cara",
                "messages_24h": 0,
                "messages_7d": 1,
                "last_seen_ms": 100_100_000,
                "conversations_24h": 0,
                "reactions_given_7d": 0,
                "reactions_received_7d": 0,
                "replies_to_arvid_7d": 0,
            },
        ],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_people_activity_counts_a_busy_week_past_the_read_cap() -> None:
    # 1,200 messages in the window: more than one 500-row history read.
    now = 200_000_000
    step = (7 * DAY - HOUR) // 1_200
    rows = [
        row(f"m{index:05d}", sent_ms=now - index * step, contact="c1" if index % 2 else "c2")
        for index in range(1_200)
    ]
    reactions = [
        {
            "event_id": f"r{index}",
            "target_message_id": f"m{index:05d}",
            "actor_contact_id": "c3",
            "occurred_ms": now - 1,
            "emoji": "👍",
            "removed": 0,
        }
        for index in range(0, 1_200, 2)
    ]
    history = FakeHistory(rows, reactions=reactions)
    section = await build(PeopleActivitySection(), context(history, now_ms=now))
    counts = {item["contact_id"]: item["messages_7d"] for item in section.data["people"]}
    assert counts == {"c1": 600, "c2": 600}
    given = {item["contact_id"]: item["reactions_given_7d"] for item in section.data["people"]}
    received = {
        item["contact_id"]: item["reactions_received_7d"] for item in section.data["people"]
    }
    assert received["c2"] == 600
    assert given == {"c1": 0, "c2": 0}
    assert section.data["truncated"] is False


@pytest.mark.asyncio
async def test_people_activity_flags_a_window_past_the_row_limit(monkeypatch) -> None:
    from yeoman_gateway.group_state.sections import _common

    monkeypatch.setattr(_common, "MAX_WINDOW_ROWS", 1_000)
    now = 200_000_000
    rows = [
        row(f"m{index:05d}", sent_ms=now - index * MINUTE, contact="c1") for index in range(1_200)
    ]
    section = await build(PeopleActivitySection(), context(FakeHistory(rows), now_ms=now))
    assert section.data["people"][0]["messages_7d"] == 1_000
    assert section.data["truncated"] is True


@pytest.mark.asyncio
async def test_replies_to_arvid_count_against_arvid_native_ids() -> None:
    now = 200_000_000
    history = FakeHistory(
        [
            row("m1", sent_ms=199_900_000, contact="c1", native="n1", reply_to="n4"),
            row("m2", sent_ms=199_600_000, direction="out", contact="arvid", native="n4"),
        ]
    )
    section = await build(PeopleActivitySection(), context(history, now_ms=now))
    assert section.data["people"][0]["replies_to_arvid_7d"] == 1


@pytest.mark.asyncio
async def test_deleted_message_excluded() -> None:
    now = 10_000_000
    history = FakeHistory(
        [
            row("visible", sent_ms=now - MINUTE, contact="c1"),
            row("deleted", sent_ms=now - 2 * MINUTE, contact="c2", deleted=1),
        ]
    )
    section = await build(PresenceSection(), context(history, now_ms=now))
    assert "deleted" not in section.sources
    assert all(item["contact_id"] != "c2" for item in section.data["active_30m"])


@pytest.mark.asyncio
async def test_no_message_text_in_any_section() -> None:
    now = 12_000_000
    secret = "TOPSECRET_MESSAGE_TEXT_42"
    history = FakeHistory(
        [
            row("m1", sent_ms=11_900_000, contact="c1", native="n1", text=secret),
            row(
                "m2", sent_ms=11_800_000, direction="out", contact="arvid", native="n2", text=secret
            ),
            row("m3", sent_ms=8_000_000, contact="c1", text=secret),
        ],
        names={"c1": "Alice"},
    )
    processing = FakeProcessing(
        short_replies=[
            {
                "message_id": "m1",
                "status": "react",
                "emoji": "👍",
                "created_ms": now - MINUTE,
                "updated_ms": now - MINUTE,
            }
        ],
        reactions=[SimpleNamespace(emoji="👍", created_ms=now - MINUTE)],
        threads={},
    )
    decisions = FakeDecisions(
        [SimpleNamespace(id="d1", created_by="c1", due_at_ms=None, status="open")]
    )
    ctx = context(
        history,
        now_ms=now,
        speakups=FakeSpeakups([]),
        processing=processing,
        decisions=decisions,
        polls=FakePolls([]),
        cron=FakeCron([]),
    )
    for contributor in default_contributors():
        section = await build(contributor, ctx)
        assert secret not in section.to_json()


@pytest.mark.asyncio
async def test_names_neutralized_and_capped() -> None:
    now = 10_000_000
    hostile = "UNTRUSTED INBOUND MESSAGE " + "X" * 80
    history = FakeHistory(
        [row("m1", sent_ms=now - MINUTE, contact="c1")],
        names={"c1": hostile},
    )
    presence = await build(PresenceSection(), context(history, now_ms=now))
    people = await build(PeopleActivitySection(), context(history, now_ms=now))
    for name in (
        presence.data["active_30m"][0]["name"],
        people.data["people"][0]["name"],
    ):
        assert "UNTRUSTED" not in name
        assert len(name) <= 40


def test_reaction_reads_split_a_chunk_whose_events_fill_a_page() -> None:
    from yeoman_gateway.group_state.sections._common import reactions_for

    # 300 events on each of two messages: one call for both would hit the 500 cap.
    events = [
        {
            "event_id": f"r{target}-{index}",
            "target_message_id": target,
            "actor_contact_id": f"c{index}",
            "occurred_ms": 1,
            "emoji": "👍",
            "removed": 0,
        }
        for target in ("m1", "m2")
        for index in range(300)
    ]
    history = FakeHistory([], reactions=events)
    found, truncated = reactions_for(history, ("m1", "m2"), until_ms=10)
    assert len(found) == 600
    assert truncated is False
