"""Voice notes stay short because they are written short, never cut mid-sentence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.send_voice import VoiceSendRequest
from yeoman_gateway.media.tts import (
    VOICE_MAX_CHARS,
    split_sentences,
    truncate_for_voice,
    voice_length_excess,
)


def test_split_sentences_ignores_abbreviations_decimals_and_ordinals():
    text = "Das kostet z.B. 3.5 Euro am 3. Oktober. Dr. Müller sagt Ja! Wirklich? Ok…"
    assert split_sentences(text) == [
        "Das kostet z.B. 3.5 Euro am 3. Oktober.",
        "Dr. Müller sagt Ja!",
        "Wirklich?",
        "Ok…",
    ]


def test_split_sentences_keeps_unterminated_tail():
    assert split_sentences("Erst das. Dann noch was") == ["Erst das.", "Dann noch was"]


def test_truncate_keeps_whole_sentences_only():
    first = "Er kennt das Thema schon von sich aus und hat es öfter erwähnt."
    second = "Sein Punkt ist, dass die meisten einfach ChatGPT oder Gemini nutzen."
    result = truncate_for_voice(f"{first} {second}", max_sentences=4, max_chars=len(first) + 5)
    assert result == first


def test_truncate_respects_sentence_cap():
    result = truncate_for_voice("Eins. Zwei. Drei. Vier. Fünf.", max_sentences=4, max_chars=750)
    assert result == "Eins. Zwei. Drei. Vier."


def test_truncate_keeps_an_overlong_first_sentence_whole():
    sentence = "A" * 50 + " " + "B" * 50 + "."
    assert truncate_for_voice(sentence, max_sentences=4, max_chars=40) == sentence


def test_truncate_is_a_noop_when_text_fits():
    text = "Kurz und gut. Fertig."
    assert truncate_for_voice(text, max_sentences=4, max_chars=750) == text


def test_voice_length_excess():
    assert voice_length_excess("Kurz.", max_sentences=4, max_chars=750) is None
    assert (
        voice_length_excess("Eins. Zwei. Drei. Vier. Fünf.", max_sentences=4, max_chars=750)
        is not None
    )
    assert voice_length_excess("x" * 751, max_sentences=4, max_chars=750) is not None


class _FakeTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize_with_status(self, text: str, **_: Any) -> tuple[bytes, None]:
        self.texts.append(text)
        return b"ogg", None


def _fake_responder(tmp_path: Path) -> tuple[SimpleNamespace, _FakeTTS, list[Any]]:
    tts = _FakeTTS()
    sent: list[Any] = []

    async def _dispatch(message: Any) -> None:
        sent.append(message)

    fake = SimpleNamespace(
        _voice_send_governance_error=lambda _request: None,
        _tts=tts,
        _whatsapp_tts_outgoing_dir=tmp_path,
        _resolve_tts_profile=lambda **_: object(),
        _recording_notifier=None,
        _whatsapp_tts_max_raw_bytes=1024,
        _outbound_dispatch_callback=_dispatch,
    )
    return fake, tts, sent


def _request(content: str, **kwargs: Any) -> VoiceSendRequest:
    return VoiceSendRequest(
        channel="whatsapp", chat_id="491234567890@s.whatsapp.net", content=content, **kwargs
    )


@pytest.mark.asyncio
async def test_model_written_overlong_note_is_rejected_for_rewrite(tmp_path):
    fake, tts, sent = _fake_responder(tmp_path)
    long_text = "Das ist ein ziemlich langer Satz über allerlei Dinge. " * 20

    result = await LLMResponder._send_voice_message(fake, _request(long_text))

    assert result.startswith("Error: Voice note too long")
    assert "Rewrite it shorter" in result
    assert tts.texts == []
    assert sent == []


@pytest.mark.asyncio
async def test_model_written_note_within_ceiling_is_spoken_whole(tmp_path):
    fake, tts, sent = _fake_responder(tmp_path)
    # Longer than the old 260-character cut, within the new ceiling.
    text = " ".join(
        [
            "Er sagt, das Thema kennt er schon von sich aus und hat es öfter erwähnt.",
            "Sein Punkt: Die Leute, die meistens etwas dazu sagen, nutzen einfach ChatGPT.",
            "Oder Gemini, ohne groß darüber nachzudenken, was dahinter eigentlich steckt.",
            "Deshalb findet er die Diskussion hier im Moment ehrlich gesagt etwas schief.",
        ]
    )
    assert 260 < len(text) <= VOICE_MAX_CHARS

    result = await LLMResponder._send_voice_message(fake, _request(text))

    assert result.startswith("Voice message delivered")
    assert tts.texts == [text]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_explicit_cap_trims_to_whole_sentences(tmp_path):
    fake, tts, _ = _fake_responder(tmp_path)

    result = await LLMResponder._send_voice_message(
        fake, _request("Guten Morgen. Zeit für Kaffee. Und noch was.", max_sentences=2)
    )

    assert result.startswith("Voice message delivered")
    assert tts.texts == ["Guten Morgen. Zeit für Kaffee."]
