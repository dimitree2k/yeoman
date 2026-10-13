"""Group conversations: deterministic segmentation of synthetic history rows."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.history.conversations import (
    ConversationBounds,
    current_conversation,
    render_header,
    segment,
)
from yeoman_gateway.history.queries import HistoryQueries

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120000000000000009@g.us"
MIN = 60_000
T0 = 1_790_000_000_000
LULL = 25 * MIN
BURST_N = 4
BURST = 15 * MIN


def _row(mid: str, minute: float, sender: str = "a", *, out: bool = False,
         reply_to: str | None = None) -> dict[str, object]:
    return {
        "message_id": f"h-{mid}", "native_message_id": mid, "chat_id": CHAT,
        "sent_ms": T0 + int(minute * MIN), "direction": "out" if out else "in",
        "sender_contact_id": None if out else f"c-{sender}",
        "sender_identifier": None if out else f"{sender}@s.whatsapp.net",
        "reply_to_native_id": reply_to,
    }


def _segment(rows, reactions=(), **kwargs):
    return segment(rows, reactions, lull_ms=LULL, burst_n=BURST_N, burst_ms=BURST, **kwargs)


@pytest.mark.parametrize(
    ("gap", "count"),
    [(25, 1), (25.001, 2), (24.999, 1)],
    ids=["exactly_at_lull", "above_lull", "below_lull"],
)
def test_gap_at_above_and_below_the_lull(gap: float, count: int) -> None:
    conversations = _segment([_row("m1", 0), _row("m2", gap)])
    assert len(conversations) == count


def test_burst_detection_at_the_threshold() -> None:
    at_threshold = [_row(f"m{i}", i * 5) for i in range(4)]  # 4 messages within 15 min
    below = [_row(f"m{i}", i * 5.1) for i in range(4)]  # 4th lands just past 15 min
    assert _segment(at_threshold)[0].burst is True
    assert _segment(below)[0].burst is False
    assert _segment(at_threshold[:3])[0].burst is False


def test_id_is_stable_and_follows_the_first_message() -> None:
    rows = [_row("m1", 0), _row("m2", 3), _row("m3", 60)]
    first = _segment(rows)
    again = _segment(list(reversed(rows)))
    assert [item.id for item in first] == [item.id for item in again]
    expected = "conv:" + hashlib.sha256(f"{CHAT}\nh-m1".encode()).hexdigest()[:16]
    assert first[0].id == expected
    assert first[1].message_ids == ("h-m3",)


def test_late_message_merges_two_conversations_and_keeps_the_earlier_id() -> None:
    rows = [_row("m1", 0), _row("m3", 40)]
    before = _segment(rows)
    assert len(before) == 2
    after = _segment([*rows, _row("m2", 20)])
    assert len(after) == 1
    assert after[0].id == before[0].id
    assert after[0].message_ids == ("h-m1", "h-m2", "h-m3")


def test_reply_into_an_earlier_conversation_is_recorded_not_merged() -> None:
    rows = [_row("m1", 0), _row("m2", 2), _row("m3", 60, "b", reply_to="m1"),
            _row("m4", 61, "c", reply_to="m3")]
    old, new = _segment(rows)
    assert old.message_ids == ("h-m1", "h-m2")
    assert new.message_ids == ("h-m3", "h-m4")
    assert new.outside_replies == (("h-m3", "m1"),)
    assert old.outside_replies == ()


def test_arvid_follow_up_flag_and_counts() -> None:
    unanswered = _segment([_row("m1", 0, "a"), _row("o1", 1, out=True)])[0]
    assert unanswered.arvid_messages == 1
    assert unanswered.answered_after_arvid is False
    answered = _segment([_row("m1", 0, "a"), _row("o1", 1, out=True), _row("m2", 2, "b")])[0]
    assert answered.answered_after_arvid is True
    assert answered.sender_count == 3  # Arvid counts like everyone else
    assert answered.message_count == 3
    absent = _segment([_row("m1", 0)])[0]
    assert absent.arvid_messages == 0 and absent.answered_after_arvid is False


def test_arvid_addressed_follows_registry_threads_only() -> None:
    rows = [_row("m1", 0), _row("o1", 1, out=True), _row("m2", 2)]
    assert _segment(rows)[0].arvid_addressed is False
    assert _segment(rows, registry_ids={"o1"})[0].arvid_addressed is True
    # A registry id on someone else's message does not mark the conversation.
    assert _segment(rows, registry_ids={"m1"})[0].arvid_addressed is False


def test_states_active_slowing_and_ended() -> None:
    rows = [_row(f"m{i}", i) for i in range(4)]
    assert _segment(rows, now_ms=T0 + 4 * MIN)[0].state == "active"
    assert _segment(rows, now_ms=T0 + 17 * MIN)[0].state == "slowing"
    assert _segment(rows, now_ms=T0 + 3 * MIN + LULL)[0].state == "ended"
    earlier, _ = _segment([*rows, _row("m9", 90)], now_ms=T0 + 91 * MIN)
    assert earlier.state == "ended"


def test_reactions_latest_per_actor_and_top_three() -> None:
    rows = [_row("m1", 0), _row("m2", 1)]

    def reaction(event, target, actor, emoji, minute, removed=False):
        return {"event_id": event, "target_message_id": target, "actor_contact_id": actor,
                "emoji": emoji, "removed": removed, "occurred_ms": T0 + minute * MIN}

    reactions = [
        reaction("e1", "h-m1", "x", "👍", 1),
        reaction("e2", "h-m1", "x", "😂", 2),  # replaces x's 👍
        reaction("e3", "h-m1", "y", "😂", 2),
        reaction("e4", "h-m2", "z", "🔥", 3),
        reaction("e5", "h-m2", "w", "🎉", 3),
        reaction("e6", "h-m2", "v", "🔥", 4),
        reaction("e7", "h-m2", "u", "👍", 4),
        reaction("e8", "h-m2", "u", "👍", 5, removed=True),
        reaction("e9", "other", "u", "👍", 5),
    ]
    conversation = _segment(rows, reactions)[0]
    assert conversation.reaction_count == 5
    assert conversation.top_emojis == ("😂", "🔥", "🎉")


def test_rows_without_time_are_skipped_and_chats_are_not_mixed() -> None:
    untimed = dict(_row("m0", 0), sent_ms=None)
    assert _segment([untimed, _row("m1", 1)])[0].message_ids == ("h-m1",)
    assert _segment([]) == []
    with pytest.raises(ValueError):
        _segment([_row("m1", 0), dict(_row("m2", 1), chat_id="other@g.us")])


def test_header_rendering() -> None:
    rows = [_row("m1", 0, "a"), _row("m2", 10, "b"), _row("o1", 20, out=True),
            _row("m3", 52, "c")]
    conversation = _segment(
        rows,
        [{"event_id": "e1", "target_message_id": "h-m1", "actor_contact_id": "x",
          "emoji": "😂", "occurred_ms": T0}],
        now_ms=T0 + 60 * MIN,
    )
    assert len(conversation) == 2  # the 32-minute gap ends the first one
    first = conversation[0]
    header = render_header(first, tz=UTC)
    start = datetime.fromtimestamp(T0 / 1000, UTC)
    assert header == (
        f"Conversation since {start:%H:%M} (20 min) · 3 people · 3 messages · "
        "1 reaction (😂) · Arvid: 1 message, unanswered · state: ended"
    )
    assert "Arvid: not in it" in render_header(conversation[1], tz=UTC)


def test_current_conversation_reads_history_as_of_a_moment(tmp_path: Path) -> None:
    history = ProjectedHistory(tmp_path)
    seconds = T0 // 1000
    for index, minute in enumerate((0, 2, 40, 41, 42, 43, 90)):
        history.add_message(
            chat_id=CHAT, message_id=f"n{index}", text=f"synthetic {index}",
            timestamp=seconds + minute * 60, sender_id=f"1000{index % 3}@s.whatsapp.net",
            participant=f"1000{index % 3}@s.whatsapp.net",
        )
    history.append("reaction", chat_id=CHAT, timestamp=seconds + 44 * 60, emoji="🔥",
                   removed=False, senderId="10009@s.whatsapp.net", targetMessageId="n2")
    history.append("reaction", chat_id=CHAT, timestamp=seconds + 50 * 60, emoji="😂",
                   removed=False, senderId="10008@s.whatsapp.net", targetMessageId="n3")
    bounds = ConversationBounds(window_ms=120 * MIN, max_rows=160, lull_ms=LULL,
                                burst_n=BURST_N, burst_ms=BURST)
    snapshot = history.snapshot()
    try:
        queries = HistoryQueries(snapshot)
        as_of = current_conversation(queries, CHAT, T0 + 45 * MIN, bounds)
        assert as_of is not None
        assert as_of.native_ids == ("n2", "n3", "n4", "n5")
        assert as_of.reaction_count == 1  # the later 😂 is after the moment
        assert as_of.burst is True and as_of.state == "active"
        later = current_conversation(queries, CHAT, T0 + 95 * MIN, bounds)
        assert later is not None and later.native_ids == ("n6",)
        addressed = current_conversation(
            queries, CHAT, T0 + 45 * MIN, bounds, thread_lookup=lambda native: None
        )
        assert addressed is not None and addressed.arvid_addressed is False
        assert current_conversation(queries, "nobody@g.us", T0, bounds) is None
    finally:
        history.close()
