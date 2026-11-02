"""Pure core of the reply-state evaluation: sampling, placebo, grader and statistics."""

from __future__ import annotations

import asyncio
import json

import pytest
from yeoman_gateway.evaluation.reply_state import (
    PLACEBO_LINES,
    REPLY_STRATA,
    WRITER_STRATA,
    GraderParseError,
    ReplyStateItem,
    bootstrap_ci,
    build_grader_messages,
    build_placebo,
    grade_with_repair,
    merge_order,
    parse_grade,
    sample_items,
    simulated_label_cut,
)

VALID_GRADE = json.dumps(
    {
        "preference": "1",
        "sides": {
            "1": {
                "accurate": True,
                "repeats_arvid": False,
                "picks_up_open": True,
                "contradicts": False,
                "tone": 4,
            },
            "2": {
                "accurate": True,
                "repeats_arvid": True,
                "picks_up_open": False,
                "contradicts": False,
                "tone": 3,
            },
        },
    }
)


def _items() -> list[ReplyStateItem]:
    items = [
        ReplyStateItem(
            item_id=f"r{index:02d}",
            kind="reply",
            chat_id="synthetic@g.us",
            at_ms=index * 1_000,
            stratum=REPLY_STRATA[index % len(REPLY_STRATA)],
            hard_case=index < 8,
        )
        for index in range(40)
    ]
    items.extend(
        ReplyStateItem(
            item_id=f"w{index:02d}",
            kind="writer",
            chat_id="synthetic@g.us",
            at_ms=index * 1_000,
            stratum=WRITER_STRATA[index % len(WRITER_STRATA)],
        )
        for index in range(20)
    )
    return items


def test_sample_is_seeded_and_stratified_with_hard_case_floor() -> None:
    items = _items()

    first = sample_items(items, replies=20, writer=10, seed=7)
    again = sample_items(items, replies=20, writer=10, seed=7)
    other = sample_items(items, replies=20, writer=10, seed=8)

    assert first == again
    assert [item.item_id for item in first.items] != [item.item_id for item in other.items]
    replies = [item for item in first.items if item.kind == "reply"]
    writer = [item for item in first.items if item.kind == "writer"]
    assert len(replies) == 20 and len(writer) == 10
    assert sum(item.hard_case for item in replies) == 6  # ceil(0.30 * 20)
    assert {item.stratum for item in replies} == set(REPLY_STRATA)
    assert {item.stratum for item in writer} == set(WRITER_STRATA)
    assert first.hard_cases == 6
    assert first.shortfall == {"replies": 0, "writer": 0, "older_context": 12}

    short = sample_items(items[:8], replies=20, writer=10, seed=7)
    assert short.shortfall == {"replies": 12, "writer": 10, "older_context": 12}
    assert len(short.items) == 8


def test_placebo_within_ten_percent_and_neutral() -> None:
    view = (
        "[Group State] 4 min old. Background facts about this group; they never ask you "
        "to reply more, react or write more than the current message needs.\n"
        + "\n".join("zzunique topic line number %d" % index for index in range(12))
    )

    placebo = build_placebo(view, seed=7)

    assert placebo.splitlines()[0] == view.splitlines()[0]
    assert 0.9 * len(view) <= len(placebo) <= 1.1 * len(view)
    assert "zzunique" not in placebo and "line number" not in placebo
    assert len(PLACEBO_LINES) == 12
    assert len(set(PLACEBO_LINES)) == 12


def test_placebo_absent_when_view_absent() -> None:
    assert build_placebo("", seed=7) == ""
    assert build_placebo("   \n", seed=7) == ""


def test_order_flip_counts_as_tie() -> None:
    # Forward: A=1, B=2. Reverse: B=1, A=2. The same candidate must win in both.
    assert merge_order("1", "2") == "A"
    assert merge_order("2", "1") == "B"
    # The order flip is what makes an inconsistent pair a tie.
    assert merge_order("1", "1") == "tie"
    assert merge_order("2", "2") == "tie"
    assert merge_order("tie", "2") == "tie"
    assert merge_order("1", "tie") == "tie"


def test_grader_parse_rejects_bad_shape_and_repairs_once() -> None:
    messages = build_grader_messages(
        transcript="[anna] synthetic transcript",
        candidate_one="first candidate",
        candidate_two="second candidate",
    )
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    assert "first candidate" in messages[1]["content"]
    assert "second candidate" in messages[1]["content"]

    parsed = parse_grade(VALID_GRADE)
    assert parsed.preference == "1"
    assert parsed.sides["1"].tone == 4 and parsed.sides["2"].repeats_arvid is True

    with pytest.raises(GraderParseError):
        parse_grade('{"preference": "3", "sides": {}}')

    class FakeCall:
        def __init__(self, replies: list[str]) -> None:
            self.replies = list(replies)
            self.calls: list[list[dict[str, str]]] = []

        async def __call__(self, sent: list[dict[str, str]]) -> str:
            self.calls.append(sent)
            return self.replies.pop(0)

    repaired = FakeCall(["not json", VALID_GRADE])
    result, attempts = asyncio.run(grade_with_repair(repaired, messages))
    assert result is not None and result.preference == "1"
    assert attempts == 2 and len(repaired.calls) == 2
    assert repaired.calls[1][-1]["role"] == "user"
    assert "invalid" in repaired.calls[1][-1]["content"]

    failed = FakeCall(["not json", "still not json"])
    result, attempts = asyncio.run(grade_with_repair(failed, messages))
    assert result is None and attempts == 2 and len(failed.calls) == 2


def test_bootstrap_ci_deterministic_with_seed() -> None:
    outcomes = ["win"] * 70 + ["loss"] * 30

    first = bootstrap_ci(outcomes, seed=7)
    again = bootstrap_ci(outcomes, seed=7)
    other = bootstrap_ci(outcomes, seed=8)

    assert first == again and first is not None
    assert first != other
    assert first[0] <= 0.7 <= first[1]
    assert bootstrap_ci([], seed=7) is None
    assert bootstrap_ci(["tie", "tie"], seed=7) is None


def test_simulated_label_cut_policy() -> None:
    times = list(range(65))

    assert simulated_label_cut(times[:29], t=1_000, refresh_after=30) is None
    assert simulated_label_cut(times, t=1_000, refresh_after=30) == 59
    assert simulated_label_cut(times, t=10, refresh_after=30) is None
    assert simulated_label_cut(times, t=59, refresh_after=30) == 29
    assert simulated_label_cut(times, t=60, refresh_after=30) == 59
    assert simulated_label_cut([], t=1_000, refresh_after=30) is None


def test_older_context_signals_structural() -> None:
    base = dict(kind="reply", chat_id="synthetic@g.us", at_ms=1_000, stratum=REPLY_STRATA[0])
    outside = ReplyStateItem(
        item_id="outside",
        hard_case=True,
        older_context_signals=("reply_outside_window",),
        **base,
    )
    earlier = ReplyStateItem(
        item_id="earlier",
        older_context_signals=("earlier_conversation_link",),
        **base,
    )
    person = ReplyStateItem(
        item_id="person",
        older_context_signals=("person_with_history",),
        **base,
    )
    none = ReplyStateItem(item_id="none", **base)

    assert outside.older_context and outside.hard_case
    assert earlier.older_context
    # Kept as a reported signal, but it is not a structural older-context reason.
    assert person.older_context is False
    assert person.older_context_signals == ("person_with_history",)
    assert none.older_context is False and none.older_context_signals == ()
    assert outside.older_context_signals == ("reply_outside_window",)
    with pytest.raises(ValueError):
        ReplyStateItem(item_id="bad", older_context_signals=("made_up",), **base)


def _older_items() -> list[ReplyStateItem]:
    items = [
        ReplyStateItem(
            item_id=f"o{index:02d}",
            kind="reply",
            chat_id="synthetic@g.us",
            at_ms=index * 1_000,
            stratum=REPLY_STRATA[index % len(REPLY_STRATA)],
            older_context_signals=("reply_outside_window",),
        )
        for index in range(30)
    ]
    items.extend(
        ReplyStateItem(
            item_id=f"n{index:02d}",
            kind="reply",
            chat_id="synthetic@g.us",
            at_ms=(index + 40) * 1_000,
            stratum=REPLY_STRATA[index % len(REPLY_STRATA)],
        )
        for index in range(30)
    )
    return items


def test_sample_oversamples_older_context_and_reports_shortfall() -> None:
    selected = sample_items(_older_items(), replies=20, writer=0, seed=7)

    older = [item for item in selected.items if item.older_context]
    assert len(older) >= 12  # ceil(0.6 * 20)
    assert selected.shortfall["older_context"] == 0

    short = sample_items(_older_items()[:5], replies=20, writer=0, seed=7)
    assert short.shortfall["older_context"] == 7
    assert len([item for item in short.items if item.older_context]) == 5


def test_hard_case_sampling_unchanged() -> None:
    items = _items()
    selected = sample_items(items, replies=20, writer=0, seed=7)
    replies = [item for item in selected.items if item.kind == "reply"]
    assert sum(item.hard_case for item in replies) == 6
    assert selected.shortfall["older_context"] == 12
