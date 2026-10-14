"""Paired shadow of the weekly group notes (Spec B step B3, section 8.2).

Synthetic only: a temporary ledger, fake Judges and a temporary eval directory. The
shadow call must record both decisions and change nothing live.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.evaluation.participation_groupnotes_ab import (
    REPORT_DIR,
    GroupNotesShadowRecorder,
    read_records,
)
from yeoman_gateway.processing.participation_group_notes import SHADOW_NOTE_KEY

NOTE = "Short dry jokes land; long explainers end threads."


# ── paired shadow ────────────────────────────────────────────────────────────


def _runtime_with_note(tmp_path: Path, judge: Any, *, mode: str):
    from tests.gateway.test_participation_integration import _Context, _runtime

    note = {"digest_id": "digest-1", "chat_id": "group@g.us", "week": "2026-W16",
            "text": NOTE}

    class NoteContext(_Context):
        async def build(self, opportunity, *, inputs):
            context = await super().build(opportunity, inputs=inputs)
            if mode == "shadow":
                context[SHADOW_NOTE_KEY] = dict(note)
            elif mode == "on":
                context.update(group_notes_text=NOTE, group_notes_week="2026-W16")
            return context

    from tests.gateway.test_participation_integration import SILENCE

    runtime, _judge, _context, log = _runtime(tmp_path, decision=SILENCE)
    runtime._judge = judge  # noqa: SLF001
    runtime._context_builder = NoteContext()  # noqa: SLF001
    recorder = GroupNotesShadowRecorder(tmp_path / "var" / Path(*REPORT_DIR))
    runtime._group_notes_shadow = recorder  # noqa: SLF001
    return runtime, recorder, log


class _NoteSensitiveJudge:
    """Silence without the note, a reaction with it; records what each call saw."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def decide(self, opportunity, context):
        from tests.gateway.test_participation_integration import REACT, SILENCE

        self.seen.append(dict(context))
        return REACT if context.get("group_notes_text") else SILENCE


@pytest.mark.asyncio
async def test_shadow_records_both_decisions_and_has_no_live_effect(tmp_path: Path) -> None:
    from tests.gateway.test_participation_integration import CHANNEL as LIVE_CHANNEL
    from tests.gateway.test_participation_integration import CHAT as LIVE_CHAT
    from tests.gateway.test_participation_integration import _opportunity as live_opportunity

    baseline, _recorder, baseline_log = _runtime_with_note(
        tmp_path / "off", _NoteSensitiveJudge(), mode="off")
    try:
        live_result = await baseline.evaluate_participation(live_opportunity())
    finally:
        baseline_log.close()
    judge = _NoteSensitiveJudge()
    runtime, recorder, log = _runtime_with_note(tmp_path / "shadow", judge, mode="shadow")
    try:
        result = await runtime.evaluate_participation(live_opportunity())
        await runtime.wait_group_notes_shadow()
        assert result == live_result
        assert runtime._reactor.calls == []  # noqa: SLF001 - the shadow reaction never ran
        assert runtime._submission.calls == 0  # noqa: SLF001
        assert len(judge.seen) == 2
        live_seen, shadow_seen = judge.seen
        assert "group_notes_text" not in live_seen
        assert shadow_seen["group_notes_text"] == NOTE
        assert SHADOW_NOTE_KEY not in shadow_seen
        # One charged judge attempt: the shadow call takes no quota and writes no ledger row.
        attempts = await log.judge_attempts_since(channel=LIVE_CHANNEL, chat_id=LIVE_CHAT,
                                                  since_ms=0)
        assert len(attempts) == 1
        records = read_records(recorder.directory)
        assert len(records) == 1
        record = records[0]
        assert (record["opportunity_id"], record["digest_id"], record["week"]) == (
            "opp-1", "digest-1", "2026-W16")
        assert (record["live"]["status"], record["live"]["action"]) == ("decided", "silence")
        assert (record["with_notes"]["status"], record["with_notes"]["action"]) == (
            "decided", "react")
        assert record["same_action"] is False
        assert {"estimated_prompt_tokens"} <= set(record["live"]) & set(record["with_notes"])
        raw = "".join(path.read_text() for path in recorder.directory.glob("*.jsonl"))
        assert NOTE not in raw
        assert "a short synthetic reply" not in raw  # no message text
        assert "nothing to add" not in raw  # no Judge reasons or purposes
        assert not {"reason", "purpose"} & (set(record["live"]) | set(record["with_notes"]))
    finally:
        log.close()


@pytest.mark.asyncio
async def test_on_and_off_make_no_second_call(tmp_path: Path) -> None:
    from tests.gateway.test_participation_integration import _opportunity as live_opportunity

    for mode in ("on", "off"):
        judge = _NoteSensitiveJudge()
        runtime, recorder, log = _runtime_with_note(tmp_path / mode, judge, mode=mode)
        try:
            await runtime.evaluate_participation(live_opportunity())
            await runtime.wait_group_notes_shadow()
            assert len(judge.seen) == 1
            assert bool(judge.seen[0].get("group_notes_text")) is (mode == "on")
            assert read_records(recorder.directory) == []
        finally:
            log.close()


@pytest.mark.asyncio
async def test_shadow_failure_is_recorded_and_never_reaches_live(tmp_path: Path) -> None:
    from yeoman_gateway.processing.participation import ParticipationDecisionError

    from tests.gateway.test_participation_integration import SILENCE
    from tests.gateway.test_participation_integration import _opportunity as live_opportunity

    class FailingWithNote:
        calls = 0

        async def decide(self, opportunity, context):
            self.calls += 1
            if context.get("group_notes_text"):
                raise ParticipationDecisionError("timeout")
            return SILENCE

    judge = FailingWithNote()
    runtime, recorder, log = _runtime_with_note(tmp_path, judge, mode="shadow")
    try:
        result = await runtime.evaluate_participation(live_opportunity())
        await runtime.wait_group_notes_shadow()
        assert result.get("status") != "judge_failed"
        assert judge.calls == 2
        [record] = read_records(recorder.directory)
        assert record["with_notes"] == {"estimated_prompt_tokens": None,
                                        "group_notes_rendered": False,
                                        "status": "judge_error", "error": "timeout"}
    finally:
        log.close()
