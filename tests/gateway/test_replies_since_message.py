"""A message that waited behind another answer sees that answer (live 2026-11-15).

Five messages arrived while Arvid read a TikTok; three of them became separate turns that
waited for the shared generation slot. Each froze its context on arrival, so none saw the
answers the others had sent in the meantime, and one fact-check was answered three times.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "120363000000000001@g.us"
T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _projected_actor_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_thread_history

    install_projected_thread_history(monkeypatch, request, tmp_path)


def _thread_with_turn(store: ProcessingStore, event_id: str, text: str) -> None:
    thread_id = store.open_thread(
        channel="whatsapp", chat_id=CHAT, root_principal="asker",
        kind="group", trigger_event_id=event_id, now_ms=T0,
    )
    turn_id = store.open_turn(
        thread_id=thread_id, principal="asker", trigger_event_id=event_id, now_ms=T0,
    )
    store.append_event(
        event_key=event_id, event_id=event_id, trace_id=f"tr-{event_id}",
        payload={"text": text}, now_ms=T0,
    )
    store.add_turn_source(
        turn_id=turn_id, event_id=event_id, source_message_id=event_id,
        role="trigger", revision_at_join=1, now_ms=T0,
    )
    store.attach_event_assignment(
        event_id=event_id, thread_id=thread_id, turn_id=turn_id, now_ms=T0,
    )


def _event(event_id: str, text: str, *, sent_ms: int) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="asker",
        content=text,
        message_id=event_id,
        is_group=True,
        mentioned_bot=True,
        timestamp=datetime.fromtimestamp(sent_ms / 1000, tz=UTC),
    )


@pytest.mark.asyncio
async def test_a_waiting_message_sees_the_answer_sent_after_it(tmp_path: Path) -> None:
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import (
        REPLIES_SINCE_MESSAGE_KEY,
        ThreadActorResponder,
    )

    store = ProcessingStore(tmp_path / "processing.db")
    _thread_with_turn(store, "ask", "zusammenfassen und faktchecken")
    _thread_with_turn(store, "hey", "@Arvid hey")
    _thread_with_turn(store, "later", "und was ist mit Bayern?")

    clock = {"now": T0 + 60_000}

    class _Config:
        threads = type("T", (), {"pending_inputs_per_thread": 8})()

    class _Inner:
        def __init__(self) -> None:
            self.seen: dict[str, Any] = {}

        async def generate_reply(self, event, decision, *, session_key=None) -> str:
            self.seen[str(event.message_id)] = event.raw_metadata.get(REPLIES_SINCE_MESSAGE_KEY)
            return f"answer to {event.message_id}"

    inner = _Inner()
    wrapper = ThreadActorResponder(
        inner=inner,
        actors=ThreadActorRegistry(store=store, config=_Config(), clock=lambda: clock["now"]),
        store=store,
        clock=lambda: clock["now"],
    )

    # Both messages were sent before the first answer was generated.
    await wrapper.generate_reply(_event("ask", "zusammenfassen", sent_ms=T0), None)
    clock["now"] += 30_000
    await wrapper.generate_reply(_event("hey", "@Arvid hey", sent_ms=T0 + 5_000), None)
    # A message sent after both answers already has them in its ordinary history.
    clock["now"] += 30_000
    await wrapper.generate_reply(
        _event("later", "und was ist mit Bayern?", sent_ms=clock["now"] - 1), None
    )

    assert inner.seen["ask"] is None
    assert inner.seen["hey"] == ["answer to ask"]
    assert inner.seen["later"] is None
    store.close()


def test_the_prompt_shows_already_sent_replies_and_offers_skip() -> None:
    text = ContextBuilder._with_replies_since_message(
        "@Arvid hey",
        {"replies_since_message": ["Kurz gecheckt, der Kern stimmt"]},
    )

    assert "[Already Sent]" in text
    assert "1. Kurz gecheckt, der Kern stimmt" in text
    assert "[SKIP]" in text
    assert ContextBuilder._with_replies_since_message("hi", {}) == "hi"
    assert ContextBuilder._with_replies_since_message("hi", {"replies_since_message": []}) == "hi"


@pytest.fixture
def _responder_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_responder_history

    install_projected_responder_history(monkeypatch, request, tmp_path)


async def _reply(tmp_path: Path, metadata: dict[str, Any]) -> tuple[str | None, list[str]]:
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.providers.base import LLMProvider, LLMResponse

    shown: list[str] = []

    class _Provider(LLMProvider):
        async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                       temperature=0.7, reasoning=None) -> LLMResponse:
            shown.append(str(messages[-1].get("content")))
            return LLMResponse(content="[SKIP]", finish_reason="stop")

        def get_default_model(self) -> str:
            return "test/provider"

    responder = LLMResponder(
        bus=MessageBus(), provider=_Provider(), workspace=tmp_path, max_iterations=2,
    )
    event = InboundEvent(
        channel="whatsapp",
        chat_id="10002@s.whatsapp.net",
        sender_id="10001@s.whatsapp.net",
        content="hey",
        raw_metadata=metadata,
    )
    decision = PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=frozenset(), reason="test",
    )
    try:
        return await responder.generate_reply(event, decision), shown
    finally:
        await responder.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_responder_history")
async def test_skip_after_already_sent_replies_sends_nothing(tmp_path: Path) -> None:
    result, shown = await _reply(tmp_path, {"replies_since_message": ["Kurz gecheckt"]})

    assert result is None
    assert any("[Already Sent]" in content for content in shown)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_responder_history")
async def test_skip_is_not_a_decline_without_already_sent_replies(tmp_path: Path) -> None:
    """[SKIP] is only offered next to already-sent replies; elsewhere it is ordinary text."""
    result, _shown = await _reply(tmp_path, {})

    assert result == "[SKIP]"


@pytest.mark.asyncio
async def test_a_nudge_while_another_answer_is_written_gets_a_holding_line(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import ThreadActorResponder

    store = ProcessingStore(tmp_path / "processing.db")
    _thread_with_turn(store, "ask", "zusammenfassen und faktchecken")
    _thread_with_turn(store, "hey", "@Arvid hey")
    _thread_with_turn(store, "other", "was kostet ein Bitcoin?")

    class _Config:
        threads = type("T", (), {"pending_inputs_per_thread": 8})()

    registry = ThreadActorRegistry(store=store, config=_Config(), clock=lambda: T0)
    busy = registry.actor_for(store.event_assignment("ask")[0])
    assert busy.freeze_snapshot() is not None
    busy._generation_in_flight = True  # the fact-check is still being written

    class _Inner:
        def __init__(self) -> None:
            self.working_on: list[str] = []
            self.full_answers: list[str] = []

        async def generate_busy_reply(self, event, decision, *, working_on):
            self.working_on.append(working_on)
            return "" if "Bitcoin" in event.content else "Geduld, bin dran."

        async def generate_reply(self, event, decision, *, session_key=None):
            self.full_answers.append(str(event.message_id))
            return "full answer"

    inner = _Inner()
    wrapper = ThreadActorResponder(inner=inner, actors=registry, store=store, clock=lambda: T0)

    nudge = await wrapper.generate_reply(_event("hey", "@Arvid hey", sent_ms=T0), None)
    other = await wrapper.generate_reply(
        _event("other", "was kostet ein Bitcoin?", sent_ms=T0), None
    )

    assert nudge == "Geduld, bin dran."
    assert inner.working_on[0] == "zusammenfassen und faktchecken"
    assert other == "full answer", "a separate request still gets its own answer"
    assert inner.full_answers == ["other"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_responder_history")
async def test_the_busy_writer_hands_a_new_request_back(tmp_path: Path) -> None:
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.providers.base import LLMProvider, LLMResponse

    shown: list[str] = []
    answers = ["Moment, bin dran.", "[NEW]"]

    class _Provider(LLMProvider):
        async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                       temperature=0.7, reasoning=None) -> LLMResponse:
            shown.append(str(messages[-1].get("content")))
            return LLMResponse(content=answers[len(shown) - 1], finish_reason="stop")

        def get_default_model(self) -> str:
            return "test/provider"

    from types import SimpleNamespace

    provider = _Provider()
    responder = LLMResponder(
        bus=MessageBus(), provider=provider, workspace=tmp_path, max_iterations=2,
    )
    responder._profile_for_name = lambda name: SimpleNamespace(model="test/provider")
    responder._provider_for_profile = lambda profile: provider
    decision = PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=frozenset(), reason="test",
        model_profile="assistantDefault",
    )
    # A channel without the WhatsApp History lease: the writer itself is under test here.
    event = InboundEvent(channel="telegram", chat_id="-1001", sender_id="42", content="hey")
    try:
        holding = await responder.generate_busy_reply(event, decision, working_on="faktcheck")
        passed = await responder.generate_busy_reply(event, decision, working_on="faktcheck")
    finally:
        await responder.aclose()

    assert holding == "Moment, bin dran."
    assert passed == ""
    assert "[WORKING ON]" in shown[0] and "faktcheck" in shown[0]
