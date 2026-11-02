"""The pure label-input builder: refs, envelopes, caps and the input hash."""

from __future__ import annotations

import json
from typing import Any

from yeoman_gateway.agent.untrusted import EXTERNAL_END, EXTERNAL_START
from yeoman_gateway.group_state.labels.prompt import (
    PROMPT_VERSION,
    build_label_input,
)

CHAT = "group-a@g.us"
ASSISTANT = {"arvid-contact"}


def _row(
    message_id: str,
    *,
    text: str = "",
    sender: str = "contact-a",
    direction: str = "in",
    sent_ms: int = 1_000,
    media: Any = None,
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
        "media_json": json.dumps(media) if media is not None else None,
    }


def _fingerprints(rows: list[dict[str, Any]]) -> Any:
    return lambda message_id: next(
        (f"fp-{row['message_id']}" for row in rows if row["message_id"] == message_id), None
    )


def _build(rows: list[dict[str, Any]], *, max_chars: int = 100_000, min_messages: int = 1):
    return build_label_input(
        rows,
        conversation_id="conv:abc",
        assistant_ids=ASSISTANT,
        fingerprint_of=_fingerprints(rows),
        max_chars=max_chars,
        min_messages=min_messages,
    )


def _all_content(label_input: Any) -> str:
    return "\n".join(str(message["content"]) for message in label_input.messages)


def test_input_has_no_identifiers() -> None:
    rows = [
        _row(
            "hist-0001",
            text=("Hi +49 176 1234567, schreib an 123456789012345@g.us, mention @123456789"),
            sender="contact-15551234567",
        ),
        _row(
            "hist-0002",
            text="Antwort von 4915112345678@s.whatsapp.net",
            sender="contact-15559876543",
            sent_ms=2_000,
        ),
    ]
    label_input = _build(rows)
    assert label_input is not None
    content = _all_content(label_input)
    for identifier in (
        "contact-15551234567",
        "contact-15559876543",
        "hist-0001",
        "hist-0002",
        "native-hist-0001",
        "@g.us",
        "@s.whatsapp.net",
        "@lid",
        "+49 176 1234567",
        "1761234567",
        "@123456789",
    ):
        assert identifier not in content
    assert set(label_input.speaker_refs.values()) == {
        "contact-15551234567",
        "contact-15559876543",
    }
    assert set(label_input.message_refs.values()) == {"hist-0001", "hist-0002"}


def test_input_wraps_text_in_external_envelope() -> None:
    rows = [
        _row("hist-0001", text="Hallo zusammen", sent_ms=1_000),
        _row("hist-0002", text="Wie geht's?", sender="contact-b", sent_ms=2_000),
    ]
    label_input = _build(rows)
    assert label_input is not None
    user = label_input.messages[-1]["content"]
    assert EXTERNAL_START in user
    assert EXTERNAL_END in user
    # One envelope around the whole transcript, with every message inside it.
    assert user.count(EXTERNAL_START) == 1
    assert user.count(EXTERNAL_END) == 1
    inside = user.split(EXTERNAL_START, 1)[1].split(EXTERNAL_END, 1)[0]
    assert "Hallo zusammen" in inside
    assert "Wie geht" in inside


def test_input_contains_language_instruction() -> None:
    label_input = _build([_row("hist-0001", text="Hallo")])
    assert label_input is not None
    assert "language of the conversation" in label_input.messages[0]["content"]


def test_input_uses_media_summary_when_text_is_missing() -> None:
    rows = [
        _row(
            "hist-0001",
            text="",
            media={"kind": "image", "description": {"text": "Ein Hund im Park"}},
        ),
        _row("hist-0002", text="Süß", sender="contact-b", sent_ms=2_000),
    ]
    label_input = _build(rows)
    assert label_input is not None
    assert "Ein Hund im Park" in label_input.messages[-1]["content"]


def test_input_drops_oldest_over_char_cap() -> None:
    rows = [
        _row("hist-0001", text="a" * 300, sent_ms=1_000),
        _row("hist-0002", text="b" * 300, sent_ms=2_000),
        _row("hist-0003", text="c" * 300, sent_ms=3_000),
        _row("hist-0004", text="d" * 300, sent_ms=4_000),
    ]
    label_input = _build(rows, max_chars=400)
    assert label_input is not None
    assert set(label_input.message_refs) == {"m1"}
    assert label_input.message_refs["m1"] == "hist-0004"


def test_input_none_below_min_messages() -> None:
    rows = [
        _row("hist-0001", text="one", sent_ms=1_000),
        _row("hist-0002", text="two", sender="contact-b", sent_ms=2_000),
        _row("hist-0003", text="three", sent_ms=3_000),
        _row("hist-0004", text="arvid", sender="arvid-contact", direction="out", sent_ms=4_000),
    ]
    assert _build(rows, min_messages=4) is None
    assert _build(rows, min_messages=3) is not None


def test_input_hash_changes_on_edit() -> None:
    rows = [_row("hist-0001", text="Hallo"), _row("hist-0002", text="Hi", sent_ms=2_000)]
    first = _build(rows)
    assert first is not None
    first_hash = first.input_hash

    edited = {
        "hist-0002": "fp-edited",
    }
    second = build_label_input(
        rows,
        conversation_id="conv:abc",
        assistant_ids=ASSISTANT,
        fingerprint_of=lambda message_id: edited.get(message_id, f"fp-{message_id}"),
        max_chars=100_000,
        min_messages=1,
    )
    assert second is not None
    assert second.input_hash != first_hash
    assert first.input_hash == _build(rows).input_hash
    assert PROMPT_VERSION == "group-state-labels-v1"
