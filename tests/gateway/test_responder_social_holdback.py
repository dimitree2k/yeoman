from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse



@pytest.fixture(autouse=True)
def _history_scope_for_whatsapp_responder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Give responder unit tests a real, isolated SQLite History snapshot per turn."""
    import sqlite3
    from types import SimpleNamespace

    from yeoman_gateway.history.context import history_knowledge_scope, history_turn
    from yeoman_gateway.history.reader import HistorySnapshot
    from yeoman_gateway.history.schema import create as create_history

    history_path = tmp_path / "synthetic-history.db"
    with sqlite3.connect(history_path) as connection:
        create_history(connection)

    async def read_turn():
        connection = sqlite3.connect(f"file:{history_path}?mode=ro", uri=True)
        return HistorySnapshot(1, (), connection)

    projector = SimpleNamespace(
        read_turn=read_turn,
        health=lambda: {"status": "ready", "generation": 1},
    )
    original_init = LLMResponder.__init__
    original_generate = LLMResponder.generate_reply

    def init_with_history(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._history_projector = projector
        self._history_knowledge = None

    async def generate_in_history(self, event, decision, *args, **kwargs):
        if getattr(event, "channel", "") != "whatsapp":
            return await original_generate(self, event, decision, *args, **kwargs)
        timestamp = getattr(event, "timestamp", None)
        occurred_ms = int(timestamp.timestamp() * 1000) if timestamp is not None else 0
        event_id = str(getattr(event, "message_id", "") or f"unit-{occurred_ms}")
        with sqlite3.connect(history_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO messages (message_id,channel,chat_id,native_message_id,"
                "sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,"
                "time_certainty,text,provenance,source_refs) "
                "VALUES (?, 'whatsapp', ?, ?, NULL, ?, 'unknown', 'in', ?, 'unknown', ?, 'native', '[]')",
                (
                    f"unit:{event_id}", str(getattr(event, "chat_id", "")), event_id,
                    str(getattr(event, "sender_id", "")), occurred_ms,
                    str(getattr(event, "content", "") or ""),
                ),
            )
        async with history_turn(projector) as snapshot:
            with history_knowledge_scope(snapshot, self._history_knowledge):
                kwargs.setdefault("history_snapshot", snapshot)
                return await original_generate(self, event, decision, *args, **kwargs)

    monkeypatch.setattr(LLMResponder, "__init__", init_with_history)
    monkeypatch.setattr(LLMResponder, "generate_reply", generate_in_history)

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
            sender_id="u1",
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
            sender_id="u1",
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
            sender_id="u1",
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
            sender_id="u1",
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
