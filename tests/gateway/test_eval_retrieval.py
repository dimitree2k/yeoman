from __future__ import annotations

from yeoman_gateway.evaluation.retrieval import (
    RETRIEVAL_CATEGORIES,
    JudgeView,
    generate_retrieval_set,
    score_retrieval,
    today_participation_view,
)


def test_generated_set_is_deterministic_and_complete() -> None:
    first = generate_retrieval_set(seed=7)
    second = generate_retrieval_set(seed=7)
    assert [s.id for s in first] == [s.id for s in second]
    assert [m.text for m in first[3].messages] == [m.text for m in second[3].messages]
    assert len(first) == 6 * len(RETRIEVAL_CATEGORIES) + 1
    assert {s.category for s in first} == set(RETRIEVAL_CATEGORIES)
    s16 = next(s for s in first if s.id == "S16")
    assert len(s16.expected.needed_evidence) == 3
    assert len(s16.messages) >= 30


def test_every_generated_scenario_has_distractors_and_a_present_subject() -> None:
    for scenario in generate_retrieval_set():
        assert len(scenario.messages) >= 20
        trigger_chat = scenario.chats[scenario.trigger.chat]
        subject = scenario.title.rsplit(": ", 1)[-1].casefold()
        assert subject in trigger_chat.members
        for needed in scenario.expected.needed_evidence:
            sender = next(m.sender for m in scenario.messages if m.id == needed)
            assert sender in trigger_chat.members


async def test_today_view_finds_only_recent_same_chat_evidence() -> None:
    summary = await score_retrieval(generate_retrieval_set(), today_participation_view)
    assert summary.by_category["single_recent"]["needed_recall"] == 1.0
    assert summary.by_category["single_old"]["needed_recall"] == 0.0
    assert summary.by_category["cross_chat"]["needed_recall"] == 0.0
    assert summary.overall["memory_items_mean"] == 0.0
    assert summary.by_category["abstention"]["abstention_correct"] == -1.0


async def test_scorer_uses_provider_evidence_and_abstention() -> None:
    scenarios = generate_retrieval_set()

    async def perfect(scenario):
        return JudgeView(
            frozenset(scenario.expected.needed_evidence),
            3,
            900,
            1.0,
            abstained=scenario.expected.abstain,
        )

    summary = await score_retrieval(scenarios, perfect)
    assert summary.overall["needed_recall"] == 1.0
    assert summary.by_category["abstention"]["abstention_correct"] == 1.0
