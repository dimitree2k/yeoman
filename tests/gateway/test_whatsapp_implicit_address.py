"""Plan 07, criterion 5: a plain-name request is a direct address before routing.

The classic pipeline classifies "Arvid, ..." too, but only after the fast gate has already
journalled the event and chosen its thread - so routing saw an unaddressed message and
treated it as ambient. These tests pin the channel-side marking that closes that gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent, WhatsAppChannel
from yeoman_shared.config.schema import WhatsAppConfig

CHAT = "gruppe@g.us"


class _RecordingGate:
    """Captures what the fast gate is asked about, and answers nothing."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def admit(self, request):  # noqa: ANN001 - stub
        self.requests.append(request)
        return None

    def reconcile_reply(self, event):  # noqa: ANN001 - stub
        return None


def _event(*, content: str, mentioned: bool = False, chat: str = CHAT, is_group: bool = True):
    return InboundEvent(
        message_id="m1",
        chat_jid=chat,
        participant_jid="111@s.whatsapp.net",
        sender_id="111",
        sender_phone_jid=None,
        is_group=is_group,
        text=content,
        timestamp=103,
        mentioned_jids=[],
        mentioned_bot=mentioned,
        reply_to_bot=False,
        reply_to_message_id=None,
        reply_to_participant=None,
        reply_to_text=None,
        media_kind=None,
        media_type=None,
        media_path=None,
        media_bytes=None,
        media_file_name=None,
        media_description=None,
        voice_transcript=None,
    )


def _channel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[WhatsAppChannel, _RecordingGate]:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, bridge_url="ws://localhost:3001", bridge_token="secret"),
        MessageBus(),
    )
    gate = _RecordingGate()
    channel._processing_gate = gate
    return channel, gate


@pytest.mark.asyncio
async def test_criterion_5_a_plain_name_request_reaches_the_gate_as_addressed(
    monkeypatch, tmp_path
) -> None:
    channel, gate = _channel(monkeypatch, tmp_path)

    await channel._ingest_inbound_event(
        _event(content="Arvid, kannst du das bitte zusammenfassen?")
    )

    assert gate.requests, "the gate was never asked"
    request = gate.requests[0]
    assert request.event.mentioned_bot is True, "the plain name was not marked as addressed"
    assert request.event.raw_metadata.get("implicit_bot_address") == "plain_name_request"
    assert str(request.event.content).startswith("Arvid,")


@pytest.mark.asyncio
async def test_criterion_5_a_message_without_the_name_stays_unaddressed(monkeypatch, tmp_path) -> None:
    channel, gate = _channel(monkeypatch, tmp_path)

    await channel._ingest_inbound_event(_event(content="kannst du das bitte zusammenfassen?"))

    request = gate.requests[0]
    assert request.event.mentioned_bot is False


@pytest.mark.asyncio
async def test_criterion_5_a_real_mention_is_left_alone(monkeypatch, tmp_path) -> None:
    channel, gate = _channel(monkeypatch, tmp_path)

    await channel._ingest_inbound_event(_event(content="Arvid, hilf mir", mentioned=True))

    request = gate.requests[0]
    assert request.event.mentioned_bot is True
    assert "implicit_bot_address" not in dict(request.event.raw_metadata or {})


@pytest.mark.asyncio
async def test_criterion_5_a_direct_chat_needs_no_address(monkeypatch, tmp_path) -> None:
    """A DM is always an order; the marking is only about groups."""
    channel, gate = _channel(monkeypatch, tmp_path)

    await channel._ingest_inbound_event(
        _event(content="Arvid, hilf mir", chat="111@s.whatsapp.net", is_group=False)
    )

    request = gate.requests[0]
    assert "implicit_bot_address" not in dict(request.event.raw_metadata or {})


# the processing core owns the reaction in a managed chat ---------------------------------


def _bait_ctx(*, metadata: dict, content: str = "ok"):
    """A short reply to the bot, i.e. the classic acknowledgement case."""
    from yeoman_gateway.core.models import InboundEvent as CoreEvent
    from yeoman_gateway.core.pipeline import PipelineContext

    event = CoreEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="111",
        content=content,
        message_id="m1",
        is_group=True,
        reply_to_bot=True,
        raw_metadata=dict(metadata),
    )
    ctx = PipelineContext(event=event)
    ctx.decision = type(
        "D",
        (),
        {
            "accept_message": True,
            "should_respond": True,
            "reason": "allowed",
            "when_to_reply_mode": "all",
        },
    )()
    return ctx


@pytest.mark.asyncio
async def test_the_core_reaction_is_not_overwritten_by_an_acknowledgement() -> None:
    """A short reply to the bot gets exactly one reaction: the core's, not a second face."""
    from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware

    ctx = _bait_ctx(metadata={"processing_reacted": True}, content="ok")
    reached: list[str] = []

    async def _next(_ctx) -> None:
        reached.append("next")

    await ImplicitBotAddressMiddleware()(ctx, _next)

    assert reached == ["next"], "the pipeline must continue, not halt"
    assert ctx.intents == [], "no second reaction on the same message"


@pytest.mark.asyncio
async def test_a_granted_ambient_answer_is_not_halted_by_an_acknowledgement() -> None:
    from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware

    ctx = _bait_ctx(metadata={"processing_answer_granted": True}, content="ok")
    reached: list[str] = []

    async def _next(_ctx) -> None:
        reached.append("next")

    await ImplicitBotAddressMiddleware()(ctx, _next)

    assert reached == ["next"], "a granted answer must reach the responder"
    assert ctx.intents == []


@pytest.mark.asyncio
async def test_without_a_processing_decision_the_classic_ack_still_works() -> None:
    """Unmanaged chats keep the cheap behaviour: a short reply is acknowledged."""
    from yeoman_gateway.core.intents import SendReactionIntent
    from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware

    ctx = _bait_ctx(metadata={}, content="ok")
    reached: list[str] = []

    async def _next(_ctx) -> None:
        reached.append("next")

    await ImplicitBotAddressMiddleware()(ctx, _next)

    reactions = [i for i in ctx.intents if isinstance(i, SendReactionIntent)]
    assert [reaction.emoji for reaction in reactions] == ["👍"]
    assert reached == [], "the classic path still ends the pipeline with its reaction"


@pytest.mark.asyncio
async def test_the_channel_marks_a_message_the_core_reacted_to(monkeypatch, tmp_path) -> None:
    """The marker has to reach the pipeline, otherwise the double reaction comes back."""
    channel, _gate = _channel(monkeypatch, tmp_path)

    class _Verdict:
        denied = False
        react = True
        ambient_candidate = False
        reply_action = "react"
        assignment = None
        reason = "allow"
        decision = None

    class _Gate(_RecordingGate):
        def admit(self, request):  # noqa: ANN001 - stub
            self.requests.append(request)
            return _Verdict()

    class _Reaction:
        async def __call__(self, **_kwargs) -> str:
            return "👍"

    published: list[object] = []

    async def _record(message) -> None:
        published.append(message)

    monkeypatch.setattr(channel.bus, "publish_inbound", _record)
    channel._processing_gate = _Gate()
    channel.set_reaction_action(_Reaction())

    await channel._ingest_inbound_event(_event(content="das ist witzig"))

    assert published, "the message must still reach the pipeline"
    metadata = dict(getattr(published[0], "metadata", {}) or {})
    assert metadata.get("processing_reacted") is True, (
        "the classic acknowledgement would send a second reaction"
    )
