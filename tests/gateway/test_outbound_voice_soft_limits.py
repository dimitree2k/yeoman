from __future__ import annotations

from dataclasses import dataclass

import pytest
from yeoman_gateway.core.intents import (
    PersistSessionIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SendReactionIntent,
)
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.outbound import OutboundMiddleware


@dataclass(slots=True)
class _TTSProfile:
    kind: str = "tts"
    provider: str = "fake"
    model: str = "fake-tts"
    timeout_ms: int = 30_000


class _Router:
    def resolve(self, *_args: object, **_kwargs: object) -> _TTSProfile:
        return _TTSProfile()


class _TTS:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def synthesize_with_status(
        self,
        text: str,
        *,
        profile: _TTSProfile,
        voice: str,
        format: str,
    ) -> tuple[bytes | None, str | None]:
        del profile, voice, format
        self.inputs.append(text)
        return b"ogg audio", None


def _voice_decision() -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        voice_output_mode="in_kind",
        voice_output_tts_route="whatsapp.tts.speak",
        voice_output_max_sentences=3,
        voice_output_max_chars=500,
    )


def _voice_event() -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="123@lid",
        sender_id="456",
        content="voice transcript",
        message_id="msg-1",
        raw_metadata={"media_kind": "audio"},
    )


async def _run_outbound(
    *,
    reply: str,
    tmp_path,
    tts: _TTS,
) -> PipelineContext:
    ctx = PipelineContext(event=_voice_event())
    ctx.decision = _voice_decision()
    ctx.reply = reply
    middleware = OutboundMiddleware(
        tts=tts,
        model_router=_Router(),
        whatsapp_tts_outgoing_dir=tmp_path,
    )

    async def _noop(_ctx: PipelineContext) -> None:
        return None

    await middleware(ctx, _noop)
    return ctx


def _metric_names(ctx: PipelineContext) -> set[str]:
    return {i.name for i in ctx.intents if isinstance(i, RecordMetricIntent)}


@pytest.mark.asyncio
async def test_inline_code_reaction_marker_detected(tmp_path):
    tts = _TTS()

    ctx = await _run_outbound(reply="`::reaction::🤙`", tmp_path=tmp_path, tts=tts)

    reactions = [i for i in ctx.intents if isinstance(i, SendReactionIntent)]
    assert len(reactions) == 1
    assert reactions[0].emoji == "🤙"
    assert reactions[0].message_id == "msg-1"
    sends = [i for i in ctx.intents if isinstance(i, SendOutboundIntent)]
    assert sends == []
    persist = [i for i in ctx.intents if isinstance(i, PersistSessionIntent)]
    assert len(persist) == 1
    assert "[reacted with 🤙]" in persist[0].assistant_content
    assert "reaction_sent" in _metric_names(ctx)
    assert tts.inputs == []


@pytest.mark.asyncio
async def test_voice_reply_uses_complete_model_reply_instead_of_hard_truncating(tmp_path):
    tts = _TTS()
    reply = (
        "Das ist im Kern eine politische Kostenverschiebung, keine echte Vereinfachung. "
        "Man kann das vertreten, aber dann sollte man ehrlich sagen, wer am Ende mehr zahlt."
    )

    ctx = await _run_outbound(reply=reply, tmp_path=tmp_path, tts=tts)

    assert tts.inputs == [reply]
    outbound = next(intent for intent in ctx.intents if isinstance(intent, SendOutboundIntent))
    assert outbound.event.content == ""
    assert len(outbound.event.media) == 1


@pytest.mark.asyncio
async def test_voice_reply_sends_text_when_reply_is_too_structured_for_audio(tmp_path):
    tts = _TTS()
    reply = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "worker.py", line 42, in run',
            "    process(payload)",
            "ValueError: invalid payload",
            "```json",
            '{"error": "invalid_payload", "retry": false}',
            "```",
        ]
    )

    ctx = await _run_outbound(reply=reply, tmp_path=tmp_path, tts=tts)

    assert tts.inputs == []
    outbound = next(intent for intent in ctx.intents if isinstance(intent, SendOutboundIntent))
    assert outbound.event.content == reply
    assert outbound.event.media == ()


@pytest.mark.asyncio
async def test_voice_reply_sends_text_when_reply_exceeds_hard_audio_length(tmp_path):
    tts = _TTS()
    reply = "x" * 501

    ctx = await _run_outbound(reply=reply, tmp_path=tmp_path, tts=tts)

    assert tts.inputs == []
    outbound = next(intent for intent in ctx.intents if isinstance(intent, SendOutboundIntent))
    assert outbound.event.content == reply
    assert outbound.event.media == ()
