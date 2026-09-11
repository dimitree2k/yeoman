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
