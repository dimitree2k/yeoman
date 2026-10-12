"""Regression proposal: effect dispatch resolves WhatsApp mentions in its turn scope."""


import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.processing.dispatch import BusEffectExecutor
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectTarget,
    MediaPayload,
    TextPayload,
)


class _Bus:
    def __init__(self) -> None:
        self.outbound: list[OutboundMessage] = []

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self.outbound.append(message)

    async def publish_reaction(self, message) -> None:
        raise AssertionError("no reaction expected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_media"),
    [
        (TextPayload(text="Hallo @alice"), []),
        (MediaPayload(media=("image-ref",), caption="Hallo @alice"), ["image-ref"]),
    ],
)
async def test_whatsapp_text_and_media_resolve_mentions_in_effect_turn_scope(
    monkeypatch, tmp_path, payload, expected_media
) -> None:
    import yeoman_gateway.channels.whatsapp as whatsapp

    from tests.gateway.history_test_helpers import ProjectedHistory

    history = ProjectedHistory(tmp_path / "mentions-history")
    calls = []

    def resolve(text, metadata, *, chat_id, snapshot):
        calls.append((text, metadata, chat_id, snapshot))
        return text.replace("@alice", "@49123"), {
            "historyMentionsResolved": True,
            "mentions": ["49123@s.whatsapp.net"],
        }

    monkeypatch.setattr(whatsapp, "resolve_history_mentions", resolve)
    bus = _Bus()
    executor = BusEffectExecutor(bus=bus)

    async with history.turn() as snapshot:
        await executor.execute(
            EffectEnvelope(
                effect_id="fx1",
                operation_key="mentions:1",
                payload=payload,
                target=EffectTarget(channel="whatsapp", chat_id="chat@g.us"),
            )
        )

    assert len(calls) == 1
    assert calls[0][2:] == ("chat@g.us", snapshot)
    assert len(bus.outbound) == 1
    sent = bus.outbound[0]
    assert sent.content == "Hallo @49123"
    assert sent.media == expected_media
    assert sent.metadata["historyMentionsResolved"] is True
    assert sent.metadata["mentions"] == ["49123@s.whatsapp.net"]

    history.close()
