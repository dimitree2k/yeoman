"""Fish Audio Drama 3 markup reaches only the voice model, never text or history."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.send_voice import SendVoiceTool, VoiceSendRequest
from yeoman_gateway.core.intents import PersistSessionIntent, SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.media.tts import (
    VOICE_STYLE_PROMPT_PATH,
    FishAudioTTSProvider,
    is_drama_profile,
    load_voice_style_prompt,
    strip_voice_directions,
    voice_length_excess,
)
from yeoman_gateway.pipeline.outbound import OutboundMiddleware

SCRIPT = "[warm and relaxed] Hallo! Das ist <emphasis>wichtig</emphasis>. [pause] Bis dann."
SPOKEN = "Hallo! Das ist wichtig. Bis dann."


@dataclass(slots=True)
class _Profile:
    provider: str
    model: str
    kind: str = "tts"
    timeout_ms: int = 30_000


DRAMA = _Profile(provider="fish_audio", model="drama-3")
PLAIN = _Profile(provider="fish_audio", model="s2-pro")


def test_strip_voice_directions_keeps_spoken_words_and_links():
    assert strip_voice_directions(SCRIPT) == SPOKEN
    assert strip_voice_directions("Siehe [Doku](https://x.y).") == "Siehe [Doku](https://x.y)."


def test_is_drama_profile():
    assert is_drama_profile(DRAMA)
    assert not is_drama_profile(PLAIN)
    assert not is_drama_profile(_Profile(provider="openai_tts", model="drama-3"))
    assert not is_drama_profile(None)


def test_length_counts_only_spoken_words():
    script = "[calm, measured and reassuring] " + "Kurz. " * 3
    assert voice_length_excess(script, max_sentences=4, max_chars=20) is None


def test_load_voice_style_prompt(tmp_path):
    assert load_voice_style_prompt(tmp_path) == ""
    path = tmp_path / VOICE_STYLE_PROMPT_PATH
    path.parent.mkdir(parents=True)
    path.write_text("  Regeln  \n", encoding="utf-8")
    assert load_voice_style_prompt(tmp_path) == "Regeln"


@pytest.mark.asyncio
async def test_fish_sends_model_as_header(monkeypatch):
    seen: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(200, content=b"ogg")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: real_client(transport=httpx.MockTransport(_handler))
    )
    audio, error = await FishAudioTTSProvider("key").synthesize(
        text="Hallo", model="drama-3", voice="v", format="opus"
    )
    assert (audio, error) == (b"ogg", None)
    assert seen["headers"]["model"] == "drama-3"
    assert b'"model"' not in seen["body"]


def test_send_voice_description_carries_style_only_when_given():
    assert "voice script" not in SendVoiceTool().description
    tool = SendVoiceTool(style_guidance=lambda: "Nutze [pause] sparsam.")
    assert "Nutze [pause] sparsam." in tool.description


class _TTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize_with_status(self, text: str, **_: Any) -> tuple[bytes, None]:
        self.texts.append(text)
        return b"ogg", None


def _fake_responder(tmp_path: Path, profile: _Profile) -> tuple[SimpleNamespace, _TTS]:
    tts = _TTS()

    async def _dispatch(_message: Any) -> None:
        return None

    fake = SimpleNamespace(
        _voice_send_governance_error=lambda _request: None,
        _tts=tts,
        _whatsapp_tts_outgoing_dir=tmp_path,
        _resolve_tts_profile=lambda **_: profile,
        _recording_notifier=None,
        _whatsapp_tts_max_raw_bytes=1024,
        _outbound_dispatch_callback=_dispatch,
    )
    return fake, tts


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "expected"), [(DRAMA, SCRIPT), (PLAIN, SPOKEN)])
async def test_send_voice_keeps_markup_only_for_drama(tmp_path, profile, expected):
    fake, tts = _fake_responder(tmp_path, profile)
    request = VoiceSendRequest(
        channel="whatsapp", chat_id="491234567890@s.whatsapp.net", content=SCRIPT
    )

    result = await LLMResponder._send_voice_message(fake, request)

    assert result.startswith("Voice message delivered")
    assert tts.texts == [expected]


class _Router:
    def __init__(self, profile: _Profile) -> None:
        self._profile = profile

    def resolve(self, *_args: object, **_kwargs: object) -> _Profile:
        return self._profile


async def _run_outbound(tmp_path: Path, *, reply: str, tts: Any, profile: _Profile):
    ctx = PipelineContext(
        event=InboundEvent(
            channel="whatsapp",
            chat_id="123@lid",
            sender_id="456",
            content="voice",
            message_id="msg-1",
            raw_metadata={"media_kind": "audio"},
        )
    )
    ctx.decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        voice_output_mode="in_kind",
        voice_output_tts_route="whatsapp.tts.speak",
    )
    ctx.reply = reply
    middleware = OutboundMiddleware(
        tts=tts, model_router=_Router(profile), whatsapp_tts_outgoing_dir=tmp_path
    )

    async def _noop(_ctx: PipelineContext) -> None:
        return None

    await middleware(ctx, _noop)
    return ctx


def _persisted(ctx: PipelineContext) -> str:
    return next(i for i in ctx.intents if isinstance(i, PersistSessionIntent)).assistant_content


@pytest.mark.asyncio
async def test_drama_voice_reply_speaks_markup_and_stores_spoken_words(tmp_path):
    tts = _TTS()
    ctx = await _run_outbound(tmp_path, reply=SCRIPT, tts=tts, profile=DRAMA)

    assert tts.texts == [SCRIPT]
    assert _persisted(ctx) == SPOKEN


@pytest.mark.asyncio
async def test_drama_text_fallback_drops_markup(tmp_path):
    class _FailingTTS(_TTS):
        async def synthesize_with_status(self, text: str, **_: Any) -> tuple[None, str]:
            return None, "down"

    ctx = await _run_outbound(tmp_path, reply=SCRIPT, tts=_FailingTTS(), profile=DRAMA)

    outbound = next(i for i in ctx.intents if isinstance(i, SendOutboundIntent))
    assert outbound.event.content == SPOKEN
    assert outbound.event.media == ()
    assert _persisted(ctx) == SPOKEN


@pytest.mark.asyncio
async def test_non_drama_route_leaves_text_untouched(tmp_path):
    tts = _TTS()
    reply = "Antwort mit [Quelle] im Text."
    ctx = await _run_outbound(tmp_path, reply=reply, tts=tts, profile=PLAIN)

    assert tts.texts == ["Antwort mit im Text."]
    assert _persisted(ctx) == reply
