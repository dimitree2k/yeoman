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


def _talkative_decision(*, notice: bool) -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        talkative_cooldown_enabled=True,
        talkative_cooldown_streak_threshold=2,
        talkative_cooldown_delay_seconds=0,
        talkative_cooldown_notice=notice,
    )



@pytest.fixture(autouse=True)
def _project_whatsapp_responder_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_responder_history

    install_projected_responder_history(monkeypatch, request, tmp_path)


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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10002@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10002@s.whatsapp.net",
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
async def test_group_voice_retry_refinement_after_social_line_does_not_hold_back(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(
        [
            "Voice message delivered to whatsapp:group@g.us. Do not send it again for this request.",
            "Ich kann eine Sprachnachricht sprechen, aber nicht wirklich singen.",
        ]
    )
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="10001@s.whatsapp.net",
            content="Sing die deutsche Nationalhymne kurz in einer Sprachnachricht",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="10001@s.whatsapp.net",
            content="Textsicherheit ist da\nJetzt aber mit Melodie \nDas war nicht gesungen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert first == "Voice message delivered to whatsapp:group@g.us. Do not send it again for this request."
    assert second == "Ich kann eine Sprachnachricht sprechen, aber nicht wirklich singen."
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
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
            sender_id="10002@s.whatsapp.net",
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
            sender_id="10001@s.whatsapp.net",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    history = responder._test_projected_history
    stale_ms = int((datetime.now() - timedelta(minutes=15)).timestamp() * 1000)
    for index, record in enumerate(history.records):
        record["received_ms"] = stale_ms + index
        record["native"]["payload"]["timestamp"] = stale_ms + index
    history.dirty = True

    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="10001@s.whatsapp.net",
            content="haha safe",
            is_group=True,
            reply_to_bot=True,
        ),
        _decision(),
    )

    await responder.aclose()

    assert second == "Alright."
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_group_talkative_cooldown_sets_blunt_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(["Tesla bleibt volatil."])
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="10001@s.whatsapp.net",
            content="Arvid check Tesla Aktie bitte",
            is_group=True,
            mentioned_bot=True,
        ),
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            talkative_cooldown_enabled=True,
            talkative_cooldown_streak_threshold=2,
            talkative_cooldown_delay_seconds=0,
        ),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="10001@s.whatsapp.net",
            content="Arvid check Tesla Aktie nochmal",
            is_group=True,
            mentioned_bot=True,
        ),
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            talkative_cooldown_enabled=True,
            talkative_cooldown_streak_threshold=2,
            talkative_cooldown_delay_seconds=0,
        ),
    )

    await responder.aclose()

    assert first == "Tesla bleibt volatil."
    assert second == "Bro, du nervst gerade mit dem gleichen Thema. Kurz Pause."
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_silent_cooldown_returns_none_without_provider_call(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(["Tesla bleibt volatil."])
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="telegram",
            chat_id="group",
            sender_id="10001",
            content="Arvid check Tesla Aktie bitte",
            is_group=True,
        ),
        _talkative_decision(notice=False),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="telegram",
            chat_id="group",
            sender_id="10001",
            content="Arvid check Tesla Aktie nochmal",
            is_group=True,
        ),
        _talkative_decision(notice=False),
    )
    session = responder.sessions.get_or_create("telegram:group")

    await responder.aclose()

    assert first == "Tesla bleibt volatil."
    assert second is None
    assert provider.calls == 1
    assert any(
        message.get("role") == "user"
        and message.get("content") == "Arvid check Tesla Aktie nochmal"
        for message in session.messages
    )


@pytest.mark.asyncio
async def test_notice_true_keeps_message(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _SequenceProvider(["Tesla bleibt volatil."])
    responder = LLMResponder(bus=MessageBus(), provider=provider, workspace=workspace)

    first = await responder.generate_reply(
        InboundEvent(
            channel="telegram",
            chat_id="group",
            sender_id="10001",
            content="Arvid check Tesla Aktie bitte",
            is_group=True,
        ),
        _talkative_decision(notice=True),
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="telegram",
            chat_id="group",
            sender_id="10001",
            content="Arvid check Tesla Aktie nochmal",
            is_group=True,
        ),
        _talkative_decision(notice=True),
    )

    await responder.aclose()

    assert first == "Tesla bleibt volatil."
    assert second == "Bro, du nervst gerade mit dem gleichen Thema. Kurz Pause."
    assert provider.calls == 1
