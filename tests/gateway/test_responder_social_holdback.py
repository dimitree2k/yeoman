from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del messages, tools, model, max_tokens, temperature, reasoning
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return LLMResponse(content=response)

    def get_default_model(self) -> str:
        return "dummy/model"


def _decision(*, is_owner: bool = False) -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        is_owner=is_owner,
    )


@pytest.mark.asyncio
async def test_group_reply_to_landed_social_line_holds_back(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        ["Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="haha safe",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second is None
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_group_new_opinion_request_after_social_line_does_not_hold_back(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer.",
            "Antigravity ist okay, wenn du die Agenten-Ausfuehrung kontrollierst.",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u2",
            content="@203075365150770 wie findest du antigravity? Arbeite ich gerade das erste Mal mit",
            is_group=True,
            mentioned_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second == "Antigravity ist okay, wenn du die Agenten-Ausfuehrung kontrollierst."
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_group_trading_exit_question_after_social_line_does_not_hold_back(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer.",
            "AMD und Intel sind weiter zyklisch. Exit haengt an deinem Zeithorizont.",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u2",
            content="@203075365150770 was macht AMD und Intel und so? Wann aussteigen? :)",
            is_group=True,
            mentioned_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second == "AMD und Intel sind weiter zyklisch. Exit haengt an deinem Zeithorizont."
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_group_social_reply_ends_rhetorical_question_with_full_stop(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        ["Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer?"]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    out = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert out == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."


@pytest.mark.asyncio
async def test_group_genuine_clarifying_question_is_not_rewritten(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(["Was genau meinst du?"])
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    out = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Das da",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert out == "Was genau meinst du?"


@pytest.mark.asyncio
async def test_group_owner_is_never_held_back(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer.",
            "Klar, mache ich.",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner-jid",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(is_owner=True),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="owner-jid",
            content="haha safe",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(is_owner=True),
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second == "Klar, mache ich."
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_group_other_user_picks_up_after_social_reply_does_not_hold_back(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer.",
            "Hier ist die Transkription: ...",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u2",
            content="@203075365150770 transkribieren pls",
            is_group=True,
            mentioned_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second == "Hier ist die Transkription: ..."
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_group_holdback_decays_after_ten_minutes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer.",
            "Alright.",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    session = responder.sessions.get_or_create("whatsapp:group@g.us")
    stale = (datetime.now() - timedelta(minutes=15)).isoformat()
    for message in session.messages:
        if message.get("role") in {"user", "assistant"}:
            message["timestamp"] = stale
    responder.sessions.save(session)

    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="haha safe",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert second == "Alright."
    assert provider.calls == 2
