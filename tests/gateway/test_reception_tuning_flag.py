"""The tuning switch is off unless both per-chat flags are on."""

from __future__ import annotations

from pathlib import Path

from yeoman_gateway.adapters.responder_llm import (
    LLMResponder,
    _render_participation_transcript,
)
from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.participation import (
    ParticipationJudge,
    ParticipationOpportunity,
)
from yeoman_gateway.reception.lessons import LessonItem, lessons_path, render_lessons_file
from yeoman_gateway.reception.stance import STANCE_BLOCK_HEADER, StanceNotesReader

CHAT = "120363000000000071@g.us"
OTHER = "120363000000000072@g.us"


def _policy() -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "channels": {
                "whatsapp": {
                    "chats": {
                        CHAT: {"receptionMetrics": True, "receptionTuning": True},
                        OTHER: {"receptionTuning": True},
                        "120363000000000073@g.us": {"receptionMetrics": True},
                    }
                }
            }
        }
    )


def test_tuning_needs_metrics_and_tuning(tmp_path: Path) -> None:
    engine = PolicyEngine(_policy(), tmp_path)
    assert engine.reception_tuning_enabled("whatsapp", CHAT) is True
    assert engine.reception_tuning_enabled("whatsapp", OTHER) is False
    assert engine.reception_tuning_enabled("whatsapp", "120363000000000073@g.us") is False
    assert engine.reception_tuning_chats("whatsapp") == (CHAT,)
    assert engine.resolve_policy("whatsapp", CHAT).reception_tuning is True


def _opportunity() -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="op-1",
        channel="whatsapp",
        chat_id=CHAT,
        trigger="addressed",
        source_event_ids=("m1",),
        observed_revision=1,
        activation_epoch=1,
        created_at_ms=1,
    )


def test_judge_render_only_changes_when_a_stance_key_exists() -> None:
    judge = ParticipationJudge(client=object())  # type: ignore[arg-type]
    base = {
        "messages": [
            {
                "event_id": "m1",
                "sender": "Tom",
                "sender_id": "p1",
                "text": "synthetic",
                "timestamp": 1,
            }
        ],
        "current_source_ids": ["m1"],
        "allowed_actions": ["silence"],
        "allowed_intents": ["initiate"],
    }
    from yeoman_gateway.processing.participation import _JudgeContext

    without = judge._build_messages(_opportunity(), _JudgeContext.from_mapping(base))
    tuned = dict(base)
    tuned["reception_stance_text"] = STANCE_BLOCK_HEADER + "\nTom: hostile"
    with_stance = judge._build_messages(_opportunity(), _JudgeContext.from_mapping(tuned))
    assert STANCE_BLOCK_HEADER not in without[1]["content"]
    assert STANCE_BLOCK_HEADER in with_stance[1]["content"]


def test_writer_transcript_drops_stance_whole_when_over_budget() -> None:
    context = {
        "target_message_id": "m1",
        "messages": [
            {
                "event_id": "m1",
                "sender": "Tom",
                "sender_id": "p1",
                "text": "hi",
            }
        ],
    }
    baseline = _render_participation_transcript(context, limit=4000)
    assert STANCE_BLOCK_HEADER not in baseline
    context["reception_stance_text"] = STANCE_BLOCK_HEADER + "\nTom: hostile"
    rendered = _render_participation_transcript(context, limit=4000)
    assert STANCE_BLOCK_HEADER in rendered
    assert context["reception_stance_rendered_to_writer"] is True
    # A budget that only fits the target drops the optional block whole.
    context.pop("reception_stance_status", None)
    tight = _render_participation_transcript(context, limit=20)
    assert STANCE_BLOCK_HEADER not in tight
    assert context["reception_stance_status"] == "budget_dropped"


def test_direct_context_renders_stance_only_when_metadata_carries_it(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)
    common = dict(
        history=[],
        current_message="hi",
        persona_text="PERSONA",
        channel="whatsapp",
        chat_id=CHAT,
    )
    without = builder.build_messages(current_metadata={}, **common)
    with_stance = builder.build_messages(
        current_metadata={"_reception_stance_text": STANCE_BLOCK_HEADER + "\nTom: hostile"},
        **common,
    )
    assert not any(STANCE_BLOCK_HEADER in str(message.get("content") or "") for message in without)
    assert any(STANCE_BLOCK_HEADER in str(message.get("content") or "") for message in with_stance)


def test_lessons_are_loaded_for_the_direct_path_only_when_tuned(tmp_path: Path) -> None:
    path = lessons_path(tmp_path, CHAT)
    path.parent.mkdir(parents=True)
    path.write_text(
        render_lessons_file([LessonItem("Lage", "Wirkung", "Änderung", ("e1",))]),
        encoding="utf-8",
    )

    class Fake:
        workspace = tmp_path
        _reception_tuning = staticmethod(lambda channel, chat: False)
        _reception_enabled = LLMResponder._reception_enabled
        _reception_persona = LLMResponder._reception_persona

    fake = Fake()
    assert fake._reception_persona("PERSONA", "whatsapp", CHAT) == "PERSONA"
    fake._reception_tuning = staticmethod(lambda channel, chat: True)
    loaded = fake._reception_persona("PERSONA", "whatsapp", CHAT)
    assert loaded is not None and "Lessons from reception" in loaded


def test_stance_reader_is_off_when_tuning_is_off() -> None:
    reader = StanceNotesReader(
        knowledge=object(),
        tuning=lambda channel, chat: False,
    )
    assert (
        reader.block_for_principals("whatsapp", CHAT, ["491510000001"], now_ms=1, snapshot=object())
        == ""
    )
