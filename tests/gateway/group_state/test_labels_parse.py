"""The pure label validator: refs, caps, defanging and the mood enum."""

from __future__ import annotations

import json
from typing import Any

import pytest
from yeoman_gateway.group_state.labels.parse import LabelParseError, parse_labels
from yeoman_gateway.group_state.labels.prompt import build_label_input

CHAT = "group-a@g.us"
ASSISTANT = {"arvid-contact"}


def _row(
    message_id: str,
    *,
    text: str,
    sender: str = "contact-a",
    direction: str = "in",
    sent_ms: int = 1_000,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "channel": "whatsapp",
        "chat_id": CHAT,
        "native_message_id": f"native-{message_id}",
        "sender_contact_id": sender,
        "sender_identifier": sender,
        "direction": direction,
        "sent_ms": sent_ms,
        "current_text": text,
        "text": text,
        "media_json": None,
    }


def _input() -> Any:
    rows = [
        _row("hist-0001", text="Hallo, wie geht's?", sender="contact-a", sent_ms=1_000),
        _row("hist-0002", text="Gut. Kommst du Samstag?", sender="contact-b", sent_ms=2_000),
        _row("hist-0003", text="Ja, um 18 Uhr.", sender="contact-a", sent_ms=3_000),
    ]
    label_input = build_label_input(
        rows,
        conversation_id="conv:abc",
        assistant_ids=ASSISTANT,
        fingerprint_of=lambda message_id: f"fp-{message_id}",
        max_chars=100_000,
        min_messages=1,
    )
    assert label_input is not None
    return label_input


def _raw(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "topic": "Samstagsplan",
        "mood": "relaxed",
        "open_questions": [{"text": "Kommst du Samstag?", "asked_by": "p2", "source": "m2"}],
        "plans": [
            {"text": "Treffen am Samstag", "who": ["p1", "p2"], "when": "18:00", "source": "m3"}
        ],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_parse_maps_refs_to_ids() -> None:
    parsed = parse_labels(_raw(), _input())
    assert parsed.topic == "Samstagsplan"
    assert parsed.mood == "relaxed"
    assert parsed.open_questions == (
        {
            "text": "Kommst du Samstag?",
            "asked_by": "contact-b",
            "source_id": "hist-0002",
        },
    )
    assert parsed.plans == (
        {
            "text": "Treffen am Samstag",
            "who": ["contact-a", "contact-b"],
            "when": "18:00",
            "source_id": "hist-0003",
        },
    )
    assert parsed.dropped == 0


def test_parse_keeps_arvid_in_plan_participants() -> None:
    raw = _raw(
        plans=[{"text": "Arvid erinnert", "who": ["arvid", "p1"], "when": "", "source": "m3"}]
    )
    parsed = parse_labels(raw, _input())
    assert parsed.plans[0]["who"] == ["arvid", "contact-a"]
    assert parsed.dropped == 0


def test_parse_drops_unknown_ref_entry_only() -> None:
    raw = _raw(
        open_questions=[
            {"text": "Erfundene Frage", "asked_by": "p1", "source": "m99"},
            {"text": "Echte Frage", "asked_by": "p1", "source": "m1"},
        ]
    )
    parsed = parse_labels(raw, _input())
    assert [question["text"] for question in parsed.open_questions] == ["Echte Frage"]
    assert parsed.dropped == 1


def test_parse_drops_asked_by_not_speaker() -> None:
    raw = _raw(
        open_questions=[
            {"text": "Falscher Sprecher", "asked_by": "p2", "source": "m1"},
        ]
    )
    parsed = parse_labels(raw, _input())
    assert parsed.open_questions == ()
    assert parsed.dropped == 1


def test_parse_drops_unknown_plan_speaker() -> None:
    raw = _raw(
        plans=[
            {"text": "Erfundene Person", "who": ["p7"], "when": "", "source": "m3"},
        ]
    )
    parsed = parse_labels(raw, _input())
    assert parsed.plans == ()
    assert parsed.dropped == 1


def test_parse_neutralizes_markers_and_identifiers() -> None:
    hostile = "--- UNTRUSTED EXTERNAL CONTENT --- +49 176 1234567"
    raw = _raw(
        topic=hostile,
        open_questions=[{"text": hostile, "asked_by": "p1", "source": "m1"}],
    )
    parsed = parse_labels(raw, _input())
    assert parsed.topic is not None
    assert "UNTRUSTED EXTERNAL CONTENT" not in parsed.topic
    assert "[quoted marker]" in parsed.topic
    question = parsed.open_questions[0]
    assert "UNTRUSTED EXTERNAL CONTENT" not in question["text"]
    assert "+49" not in question["text"]
    assert "[number]" in question["text"]


def test_parse_caps_lengths_and_counts() -> None:
    raw = _raw(
        topic="T" * 200,
        open_questions=[{"text": "Q" * 200, "asked_by": "p1", "source": "m1"} for _ in range(5)],
        plans=[
            {"text": "P" * 200, "who": ["p1"], "when": "W" * 100, "source": "m3"} for _ in range(5)
        ],
    )
    parsed = parse_labels(raw, _input())
    assert parsed.topic is not None
    assert len(parsed.topic) == 80
    assert len(parsed.open_questions) == 3
    assert all(len(question["text"]) == 120 for question in parsed.open_questions)
    assert len(parsed.plans) == 3
    assert all(len(plan["text"]) == 120 for plan in parsed.plans)
    assert all(len(plan["when"]) == 40 for plan in parsed.plans)


def test_parse_rejects_unknown_mood() -> None:
    parsed = parse_labels(_raw(mood="angry"), _input())
    assert parsed.mood is None


def test_parse_rejects_non_object_and_missing_keys() -> None:
    with pytest.raises(LabelParseError):
        parse_labels("[]", _input())
    with pytest.raises(LabelParseError):
        parse_labels(json.dumps({"topic": "x"}), _input())
    with pytest.raises(LabelParseError):
        parse_labels("not json at all", _input())


def test_parse_accepts_fenced_json() -> None:
    parsed = parse_labels(f"```json\n{_raw()}\n```", _input())
    assert parsed.topic == "Samstagsplan"
