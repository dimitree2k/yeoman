from __future__ import annotations

from dataclasses import replace

from yeoman_gateway.evaluation.discretion import (
    DISCRETION_FILE,
    grade_decision,
    leaked_terms,
    run_discretion,
)
from yeoman_gateway.evaluation.harness import StaticClient, make_judge
from yeoman_gateway.evaluation.scenarios import load_scenario_file
from yeoman_gateway.processing.participation import ParticipationDecision

SCENARIOS = {s.id: s for s in load_scenario_file(DISCRETION_FILE)}
SILENCE = '{"action": "silence", "intent": "initiate", "reason": "q"}'
EXPECTED_IDS = {
    *(f"S{i}" for i in range(1, 9)), "S10",
    "S17", "S18", "S19", "S20", "S21", "S22a", "S22b", "S22c", "S23", "S24",
    "C1", "C2", "C3",
}


def test_file_holds_only_behavioral_scenarios_and_controls() -> None:
    assert set(SCENARIOS) == EXPECTED_IDS
    assert {sid for sid, scenario in SCENARIOS.items() if not scenario.requires} == {
        "S1", "S8", "C1", "C2", "C3"
    }


def test_leak_check_flags_private_terms_but_ignores_terms_visible_in_chat() -> None:
    s1 = SCENARIOS["S1"]
    assert leaked_terms(s1, "Keine Witze über Bruno heute") == ["Bruno"]
    assert leaked_terms(s1, "Noch ein Hundewitz?") == []

    visible_message = replace(s1.messages[-1], text="Bruno ist hier erwähnt")
    visible = replace(s1, messages=(*s1.messages[:-1], visible_message))
    assert leaked_terms(visible, "Keine Witze über Bruno heute") == []


def test_grading_checks_action_emoji_and_leaks() -> None:
    s1 = SCENARIOS["S1"]
    laugh = ParticipationDecision(
        action="react", intent="initiate", reason="", emoji="😂", target_message_id="b2"
    )
    assert grade_decision(s1, laugh)[0] is False
    silent = ParticipationDecision(action="silence", intent="initiate", reason="")
    assert grade_decision(s1, silent) == (True, "ok")

    comment = replace(s1, expected=replace(s1.expected, actions=("comment",)))
    leaking = ParticipationDecision(
        action="comment", intent="initiate", reason="", purpose="Bruno is gone", target_message_id="b2"
    )
    assert grade_decision(comment, leaking) == (False, "leak:Bruno")
    visible_message = replace(s1.messages[-1], text="Bruno is visible here")
    visible = replace(s1, messages=(*s1.messages[:-1], visible_message))
    visible_comment = replace(visible, expected=replace(visible.expected, actions=("comment",)))
    assert grade_decision(visible_comment, leaking) == (True, "ok")


async def test_runner_runs_today_scenarios_and_marks_future_features_pending() -> None:
    results = await run_discretion(
        list(SCENARIOS.values()), lambda: make_judge(StaticClient(SILENCE), allowed_emojis=("👍",)), runs=2
    )
    by_id = {result.scenario_id: result for result in results}
    assert by_id["S1"].status == "pass" and by_id["S1"].runs == 2
    assert by_id["C1"].status == "fail"
    assert by_id["S2"].status == "pending"


async def test_runner_reports_builder_errors_per_scenario() -> None:
    broken = replace(SCENARIOS["C3"], now_ms=SCENARIOS["C3"].now_ms + 6 * 3600_000)
    [result] = await run_discretion(
        [broken], lambda: make_judge(StaticClient(SILENCE), allowed_emojis=("👍",))
    )
    assert result.status == "error" and "source_expired" in result.detail
