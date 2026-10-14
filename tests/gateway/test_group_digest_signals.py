"""Weekly group reaction signals (Spec B §4): deterministic, on synthetic history only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from yeoman_gateway.history.group_signals import (
    MIN_CONVERSATIONS,
    MIN_REACTIONS,
    compute_signals,
    iso_week,
    message_kind,
    previous_week,
    read_group_signals,
    week_moment_ms,
    week_of,
)
from yeoman_gateway.history.queries import HistoryQueries

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120363000000000042@g.us"
MIN = 60_000
LULL = 25 * MIN
BURST_N = 4
BURST = 15 * MIN
WEEK = iso_week("2026-W40", tz=UTC)
T0 = WEEK.start_ms + 9 * 60 * MIN  # Monday 09:00 UTC


def _row(mid: str, minute: float, sender: str = "a", *, out: bool = False,
         text: str = "synthetic text", reply_to: str | None = None,
         media: dict | None = None) -> dict[str, object]:
    return {
        "message_id": f"h-{mid}", "native_message_id": mid, "chat_id": CHAT,
        "sent_ms": T0 + int(minute * MIN), "direction": "out" if out else "in",
        "sender_contact_id": None if out else f"c-{sender}",
        "sender_identifier": None if out else f"{sender}@s.whatsapp.net",
        "reply_to_native_id": reply_to, "current_text": text,
        "media_json": json.dumps(media) if media else None,
    }


def _reaction(event: str, target: str, actor: str, emoji: str, minute: float = 1,
              removed: bool = False) -> dict[str, object]:
    return {"event_id": event, "target_message_id": f"h-{target}", "actor_contact_id": actor,
            "emoji": emoji, "removed": removed, "occurred_ms": T0 + int(minute * MIN)}


def _signals(rows, reactions=(), **kwargs):
    return compute_signals(rows, reactions, chat_id=CHAT, week=WEEK, lull_ms=LULL,
                           burst_n=BURST_N, burst_ms=BURST, tz=UTC, **kwargs)


# ── weeks ────────────────────────────────────────────────────────────────────


def test_iso_week_windows_and_previous_week() -> None:
    assert datetime.fromtimestamp(WEEK.start_ms / 1000, UTC) == datetime(2026, 9, 28, tzinfo=UTC)
    assert WEEK.end_ms - WEEK.start_ms == 7 * 24 * 60 * MIN
    assert week_of(WEEK.start_ms, tz=UTC) == WEEK
    assert week_of(WEEK.end_ms - 1, tz=UTC) == WEEK
    assert previous_week(WEEK.end_ms + 5 * MIN, tz=UTC) == WEEK
    assert week_moment_ms("2026-W41", hours=4, tz=UTC) == WEEK.end_ms + 4 * 60 * MIN
    with pytest.raises(ValueError):
        iso_week("2026-42")
    with pytest.raises(ValueError):
        iso_week("2026-W54")


def test_local_week_follows_the_timezone_across_daylight_saving() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    week = iso_week("2026-W43", tz=berlin)  # contains the switch back on 2026-10-25
    assert datetime.fromtimestamp(week.start_ms / 1000, berlin).hour == 0
    assert week.end_ms - week.start_ms == (7 * 24 + 1) * 60 * MIN


# ── approval ─────────────────────────────────────────────────────────────────


def test_reactions_rank_by_distinct_reactors_and_count_negative_emojis() -> None:
    rows = [_row("m1", 0, text="a joke"), _row("m2", 1, "b"), _row("m3", 2, "c")]
    reactions = [
        # m1: one person reacting with a second emoji replaces the first one.
        _reaction("e1", "m1", "x", "👍"), _reaction("e2", "m1", "x", "😂", 2),
        # m2: three distinct reactors, one negative.
        _reaction("e3", "m2", "x", "🔥"), _reaction("e4", "m2", "y", "🔥"),
        _reaction("e5", "m2", "z", "🙄"),
        # m3: two reactors, one of them later removed.
        _reaction("e6", "m3", "x", "👎"), _reaction("e7", "m3", "y", "😡"),
        _reaction("e8", "m3", "y", "😡", 3, removed=True),
    ]
    signals = _signals(rows, reactions)
    assert [item.message_id for item in signals.approval] == ["h-m2", "h-m1", "h-m3"]
    top = signals.approval[0]
    assert (top.reactors, top.reactions, top.negative) == (3, 3, 1)
    assert top.emojis == ("🔥", "🙄")
    assert signals.reactions == 5
    assert signals.reacted_messages == 3
    assert signals.reactors == 3
    assert signals.negative_reactions == 2  # 🙄 and 👎; the removed 😡 does not count
    assert dict(signals.emoji_counts)["🔥"] == 2


def test_reacted_messages_are_described_by_kind_length_reply_and_time() -> None:
    rows = [
        _row("m1", 0, text="see https://example.invalid/x"),
        _row("m2", 1, text="", media={"kind": "image"}),
        _row("m3", 2, text="", media={"kind": "audio"}),
        _row("m4", 3, text="x" * 250, reply_to="m1"),
        _row("o1", 4, out=True, text="Arvid here"),
    ]
    reactions = [_reaction(f"e{i}", mid, "x", "👍") for i, mid in enumerate(
        ("m1", "m2", "m3", "m4", "o1"))]
    by_id = {item.message_id: item for item in _signals(rows, reactions).approval}
    assert by_id["h-m1"].kind == "link" and by_id["h-m1"].length == "short"
    assert by_id["h-m2"].kind == "image" and by_id["h-m2"].length == "none"
    assert by_id["h-m3"].kind == "voice"
    assert by_id["h-m4"].is_reply is True and by_id["h-m4"].length == "long"
    assert len(by_id["h-m4"].excerpt) == 200
    assert by_id["h-o1"].from_arvid is True
    assert {item.time_of_day for item in by_id.values()} == {"morning"}
    assert message_kind({"media_json": json.dumps({"kind": "poll"})}) == "poll"


# ── engagement ───────────────────────────────────────────────────────────────


def test_conversations_rank_by_senders_times_duration_and_keep_openings() -> None:
    # Conversation A: 2 people over 20 min. Conversation B: 3 people over 15 min.
    first = [_row("a1", 0, "a", text="opening of a"), _row("a2", 10, "b"), _row("a3", 20, "a")]
    second = [_row("b1", 100, "a", text="opening of b"), _row("b2", 105, "b"),
              _row("b3", 110, "c"), _row("b4", 112, "a"), _row("b5", 115, "c")]
    signals = _signals([*first, *second])
    assert signals.conversations == 2
    assert [item.opening[0][1] for item in signals.engagement] == ["opening of b", "opening of a"]
    best = signals.engagement[0]
    assert (best.senders, best.duration_ms, best.messages, best.burst) == (3, 15 * MIN, 5, True)
    assert len(best.opening) == 3
    assert signals.engaging_conversations == 1


def test_rows_outside_the_week_or_chat_are_ignored() -> None:
    inside = _row("m1", 0)
    before = dict(_row("m0", 0), sent_ms=WEEK.start_ms - 1)
    after = dict(_row("m9", 0), sent_ms=WEEK.end_ms)
    other = dict(_row("mx", 1), chat_id="other@g.us")
    signals = _signals([before, inside, after, other])
    assert signals.messages == 1
    assert signals.conversations == 1


# ── Arvid's effect ───────────────────────────────────────────────────────────


def test_arvid_follow_up_reply_reaction_and_end_detection() -> None:
    rows = [
        _row("m1", 0, "a"),
        _row("o1", 1, out=True, text="answered one"),
        _row("m2", 3, "b", reply_to="o1"),
        _row("m3", 4, "c"),
        # A new conversation that Arvid ends.
        _row("m4", 100, "a"),
        _row("o2", 101, out=True, text="last word"),
    ]
    signals = _signals(rows, [_reaction("e1", "o1", "x", "👍")])
    arvid = signals.arvid
    assert arvid.messages == 2
    assert (arvid.followed, arvid.replied, arvid.reacted) == (1, 1, 1)
    assert (arvid.continued, arvid.ended) == (1, 1)
    assert [item.excerpt for item in arvid.answered_and_continued] == ["answered one"]
    assert arvid.answered_and_continued[0].others_after == 2
    assert [item.excerpt for item in arvid.ended_conversation] == ["last word"]


def test_registry_threads_mark_addressed_conversations_and_count_other_people() -> None:
    rows = [
        _row("m1", 0, "a", text="@Arvid what do you think"),
        _row("o1", 1, out=True, reply_to="m1"),
        _row("m2", 2, "a"),  # the person who asked does not count
        _row("m3", 3, "b"),
        _row("m4", 4, "c"),
    ]
    plain = _signals(rows).arvid
    assert plain.addressed_conversations == 0
    joined = _signals(rows, registry={"o1"}).arvid
    assert joined.addressed_conversations == 1
    assert joined.addressed_with_others == 1
    assert joined.others_after_addressed == 2


# ── minimum signal ───────────────────────────────────────────────────────────


def test_minimum_signal_needs_both_reactions_and_conversations() -> None:
    assert (MIN_REACTIONS, MIN_CONVERSATIONS) == (20, 5)
    rows = [_row(f"m{i}", i * 60) for i in range(5)]  # five conversations
    few = [_reaction(f"e{i}", f"m{i % 5}", f"x{i}", "👍") for i in range(19)]
    assert _signals(rows, few).meets_minimum is False
    enough = [*few, _reaction("e19", "m0", "x19", "👍")]
    assert _signals(rows, enough).meets_minimum is True
    assert _signals(rows[:4], enough[:16]).meets_minimum is False
    busy = _signals(rows[:1], [_reaction(f"e{i}", "m0", f"x{i}", "👍") for i in range(30)])
    assert busy.reactions == 30 and busy.conversations == 1
    assert busy.meets_minimum is False
    assert busy.aggregates()["minimum"] == {"reactions": 20, "conversations": 5, "met": False}


def test_aggregates_carry_no_message_text_ids_or_senders() -> None:
    rows = [_row("m1", 0, "a", text="SECRET-TEXT one"), _row("o1", 1, out=True, text="SECRET-TEXT two"),
            _row("m2", 2, "b", text="SECRET-TEXT three")]
    payload = json.dumps(_signals(rows, [_reaction("e1", "m1", "x", "👍")]).aggregates())
    assert "SECRET-TEXT" not in payload
    assert "h-m1" not in payload and "c-a" not in payload


# ── reading history ──────────────────────────────────────────────────────────


def test_read_group_signals_pages_a_full_week_from_projected_history(tmp_path: Path) -> None:
    history = ProjectedHistory(tmp_path / "history")
    seconds = WEEK.start_ms // 1000
    count = 1_050  # more than two pages of 500
    for index in range(count):
        history.add_message(
            chat_id=CHAT, message_id=f"n{index}", text=f"synthetic {index}",
            timestamp=seconds + 3600 + index * 300, sender_id=f"4915100000{index % 7:02d}@s.whatsapp.net",
            participant=f"4915100000{index % 7:02d}@s.whatsapp.net",
        )
    history.add_message(chat_id=CHAT, message_id="late", text="next week",
                        timestamp=WEEK.end_ms // 1000 + 60,
                        sender_id="491510000001@s.whatsapp.net",
                        participant="491510000001@s.whatsapp.net")
    for index in range(0, 130):
        history.append("reaction", chat_id=CHAT, timestamp=seconds + 3600 + index * 300 + 10,
                       emoji="🔥", removed=False, senderId="491519999999@s.whatsapp.net",
                       targetMessageId=f"n{index}")
    history.append("reaction", chat_id=CHAT, timestamp=WEEK.end_ms // 1000 + 120, emoji="😂",
                   removed=False, senderId="491519999998@s.whatsapp.net", targetMessageId="n1")
    history.add_contact(identifiers=["491510000001@s.whatsapp.net"], name="Bertram Example",
                        timestamp=seconds)
    snapshot = history.snapshot()
    try:
        queries = HistoryQueries(snapshot)
        signals = read_group_signals(queries, CHAT, WEEK, lull_ms=LULL, burst_n=BURST_N,
                                     burst_ms=BURST, tz=UTC)
        assert signals.messages == count
        assert signals.reactions == 130  # the reaction after the week is left out
        assert signals.truncated is False
        names, phones = queries.participant_terms(chat_id=CHAT)
        assert "Bertram Example" in names
        assert "491510000001" in phones and "491519999999" in phones
        capped = read_group_signals(queries, CHAT, WEEK, lull_ms=LULL, burst_n=BURST_N,
                                    burst_ms=BURST, tz=UTC, max_rows=600)
        assert capped.truncated is True and capped.messages == 600
    finally:
        history.close()
