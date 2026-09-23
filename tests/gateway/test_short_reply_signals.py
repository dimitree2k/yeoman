from __future__ import annotations

import pytest
from yeoman_gateway.short_reply.signals import (
    compute_signals,
    emojis_in,
    grapheme_count,
    has_question_punct,
    is_emoji_only,
    truncate_graphemes,
)


def _signals(content: str, *, reply_to_text: str = "Starker Trade.", **metadata: object):
    return compute_signals(
        content=content, reply_to_bot=True, reply_to_text=reply_to_text, metadata=metadata
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jepp, hatte Glück 😊", 19),
        ("lol that was pure luck", 22),
        ("ありがとう！", 6),
        ("👨‍👩‍👧", 1),
        ("👍🏽", 1),
        ("🇩🇪", 1),
        ("  viel   Platz  ", 10),
    ],
)
def test_graphemes_are_counted_per_user_perceived_character(text: str, expected: int) -> None:
    assert grapheme_count(text) == expected


@pytest.mark.parametrize(
    "text",
    ["what?", "wie？", "¿en serio", "شكرا؟", "τι\u037e", "ինչ՞", "ምን፧", "wait⁉"],
)
def test_question_punctuation_of_many_scripts_is_recognised(text: str) -> None:
    assert has_question_punct(text) is True


def test_a_plain_semicolon_is_not_a_question() -> None:
    assert has_question_punct("ok; danke") is False


def test_emojis_are_found_by_unicode_property() -> None:
    assert emojis_in("Jepp 😊 und 👨‍👩‍👧 🇩🇪") == ("😊", "👨‍👩‍👧", "🇩🇪")
    assert is_emoji_only("😂 😂") is True
    assert is_emoji_only("1😂") is False
    assert is_emoji_only("123") is False


@pytest.mark.parametrize("component", ["\U0001F3FB", "\u200d"])
def test_standalone_emoji_modifier_and_zwj_allow_surrounding_whitespace(component: str) -> None:
    assert is_emoji_only(f" \t{component}\n") is True


def test_emoji_only_rejects_ordinary_text() -> None:
    assert is_emoji_only("ordinary text") is False


def test_truncation_never_splits_a_grapheme() -> None:
    assert truncate_graphemes("ab👨‍👩‍👧cd", 3) == "ab👨‍👩‍👧"


@pytest.mark.parametrize(
    "content",
    ["Jepp, hatte Glück 😊", "lol that was pure luck", "ありがとう！", "شكرا", "😂😂"],
)
def test_short_replies_in_any_language_are_candidates(content: str) -> None:
    assert _signals(content).is_candidate(max_chars=80) is True


def test_a_question_is_not_a_candidate() -> None:
    assert _signals("¿en serio?").is_candidate(max_chars=80) is False


def test_length_limit_is_inclusive() -> None:
    assert _signals("x" * 80).is_candidate(max_chars=80) is True
    assert _signals("x" * 81).is_candidate(max_chars=80) is False


def test_own_messages_and_non_replies_are_never_candidates() -> None:
    assert _signals("ok", from_me=True).is_candidate(max_chars=80) is False
    not_reply = compute_signals(content="ok", reply_to_bot=False, reply_to_text=None, metadata={})
    assert not_reply.is_candidate(max_chars=80) is False


def test_voice_transcript_remains_media_and_supplies_the_grapheme_count() -> None:
    voice = _signals("[Voice Message]", media_kind="audio", voice_transcript="ありがとう！")
    assert voice.has_media is True
    assert voice.media_text == "ありがとう！"
    assert voice.graphemes == 6
    assert voice.is_candidate(max_chars=80) is True


def test_audio_without_a_transcript_is_not_a_candidate() -> None:
    voice = _signals("[Voice Message]", media_kind="audio")
    assert voice.has_media is True and voice.media_text == ""
    assert voice.is_candidate(max_chars=80) is False


def test_enrichment_text_does_not_count_as_the_reply() -> None:
    signals = _signals(
        "nice\n[image_description] A long description of a chart with many words in it",
        media_kind="image",
    )
    assert signals.text == "nice"
    assert signals.graphemes == 4
    assert signals.media_text.startswith("A long description")
    assert signals.is_candidate(max_chars=80) is True


def test_a_bare_image_without_description_is_not_a_candidate() -> None:
    signals = _signals("[Image]", media_kind="image")
    assert signals.text == ""
    assert signals.is_candidate(max_chars=80) is False


def test_a_described_image_without_caption_is_still_not_a_candidate() -> None:
    # Spec §6.1: without a caption there is no reply text; the answer path handles it.
    signals = _signals("[Image]", media_kind="image", media_description="A thumbs up photo")
    assert signals.media_text == "A thumbs up photo"
    assert signals.is_candidate(max_chars=80) is False


def test_a_sticker_is_a_gesture_candidate() -> None:
    signals = _signals("[Sticker]", media_kind="sticker")
    assert signals.media_text == "sticker"
    assert signals.is_candidate(max_chars=80) is True


def test_a_described_sticker_uses_bounded_media_text_and_remains_a_candidate() -> None:
    description = "a dancing cat"
    signals = _signals(
        f"[Sticker]\n[sticker_description] {description}",
        media_kind="sticker",
        media_description=description,
    )
    assert signals.text == ""
    assert signals.media_text == description
    assert grapheme_count(signals.media_text) <= 200
    assert signals.is_candidate(max_chars=80) is True


def test_a_voice_reply_is_measured_on_its_transcript() -> None:
    signals = _signals("[Voice Message]", media_kind="audio", voice_transcript="yes, thanks")
    assert signals.text == ""  # the bridge placeholder is not reply text
    assert signals.media_text == "yes, thanks"
    assert signals.graphemes == 11
    assert signals.is_candidate(max_chars=80) is True


def test_enriched_voice_transcript_is_not_duplicated_as_reply_text() -> None:
    transcript = "yes, thanks"
    signals = _signals(transcript, media_kind="audio", voice_transcript=transcript)
    assert signals.text == ""
    assert signals.media_text == transcript
    assert signals.graphemes == 11
    assert signals.is_candidate(max_chars=80) is True


@pytest.mark.parametrize(
    ("bot_text", "asked"),
    [
        ("Wie lief der Trade?", True),
        ("How did it go?", True),
        ("どうでしたか？", True),
        ("Starker Trade.", False),
        ("", False),
    ],
)
def test_bot_asked_is_read_from_punctuation_only(bot_text: str, asked: bool) -> None:
    assert _signals("ok", reply_to_text=bot_text).bot_asked is asked


def test_log_fields_carry_no_text() -> None:
    fields = _signals("geheimer Inhalt 😊").log_fields()
    assert "geheimer" not in repr(fields)
    assert fields["graphemes"] == 17 and fields["emoji_only"] is False
