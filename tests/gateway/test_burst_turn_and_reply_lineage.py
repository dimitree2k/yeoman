"""Burst merges keep their turn; replies are recorded under their own turn (2026-11-15).

In Finanzgruppe an ask was merged with its sender's follow-up. The merged event carried the
follow-up's id and lost the ask's assignment, so it opened a new thread while the ask's turn
stayed open with no answer. And every managed reply was recorded under the chat's first open
turn, because the reply path never named the message it answered.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import OutboundEvent
from yeoman_gateway.processing.models import TurnBinding
from yeoman_shared.config.schema import WhatsAppConfig

from tests.gateway.test_processing_end_to_end import (  # noqa: F401 - fixture import
    CHAT,
    ORDERER,
    _admit,
    _event,
    _policy,
    runtime,
)


def test_a_merged_burst_keeps_the_members_gate_decisions(tmp_path: Path) -> None:
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=1), MessageBus())
    published = []

    async def publish(event):
        published.append(event)

    channel._publish_event = publish  # type: ignore[method-assign]
    parse = channel._parse_inbound_event
    ask = parse({"chatJid": "g@g.us", "messageId": "ask", "senderId": "4915", "text": "check"})
    follow = parse({"chatJid": "g@g.us", "messageId": "follow", "senderId": "4915", "text": "?"})
    assert ask is not None and follow is not None
    ask = replace(
        ask,
        thread_assignment={"thread_id": "th-ask", "turn_id": "tu-ask", "source_message_ids": []},
        processing_answer_granted=True,
    )

    asyncio.run(channel._publish_debounce_batch([ask, follow]))

    merged = published[-1]
    assert merged.message_id == "follow"
    assert merged.thread_assignment is not None
    assert merged.thread_assignment["turn_id"] == "tu-ask"
    assert merged.processing_answer_granted is True


def test_the_burst_follow_up_joins_the_ask_turn(runtime) -> None:  # noqa: F811
    runtime.reload_policy(_policy(when_to_reply="mention_only"))
    # The gate saw the unaddressed follow-up without a thread to join (live: the ask's
    # thread was not eligible), so it was only observed.
    follow = _admit(runtime, message_id="follow", content="natürlich nicht", mentioned=False)
    assert follow is not None
    ask = _admit(runtime, message_id="ask", content="zusammenfassen bitte")
    assert ask is not None and ask.assignment is not None and ask.assignment.turn_id
    assert runtime.store.event_assignment("follow")[1] is None

    merged = _event(message_id="follow", content="zusammenfassen bitte\nnatürlich nicht")
    merged = replace(
        merged,
        raw_metadata={
            "thread_id": ask.assignment.thread_id,
            "turn_id": ask.assignment.turn_id,
        },
    )
    assignment = runtime.gate.reconcile_reply(merged)

    assert assignment is not None
    assert (assignment.thread_id, assignment.turn_id) == (
        ask.assignment.thread_id,
        ask.assignment.turn_id,
    )
    open_turns = [
        turn
        for thread in runtime.store.list_threads(chat_id=CHAT, state="open")
        if (turn := runtime.store.active_turn(thread.thread_id)) is not None
    ]
    assert len(open_turns) == 1, "the follow-up must not open a second turn"


def test_a_single_message_is_not_moved_by_its_own_assignment(runtime) -> None:  # noqa: F811
    first = _admit(runtime, message_id="m1", content="erste Frage")
    assert first is not None and first.assignment is not None
    own = _event(message_id="m1", content="erste Frage")
    own = replace(
        own,
        raw_metadata={"thread_id": "th-other", "turn_id": "tu-other"},
    )

    assignment = runtime.gate.reconcile_reply(own)

    assert assignment is not None
    assert assignment.turn_id == first.assignment.turn_id


@pytest.mark.asyncio
async def test_a_reply_intent_is_recorded_under_its_frozen_turn(runtime, monkeypatch) -> None:  # noqa: F811
    first = _admit(runtime, message_id="m1")
    turn_a = runtime.store.get_turn(str(first.assignment.turn_id))
    thread_b = runtime.store.open_thread(
        channel="whatsapp", chat_id=CHAT, root_principal=ORDERER, kind="group",
        trigger_event_id="m2", now_ms=1,
    )
    turn_b = runtime.store.get_turn(
        runtime.store.open_turn(
            thread_id=thread_b, principal=ORDERER, trigger_event_id="m2", now_ms=1
        )
    )
    monkeypatch.setattr(
        runtime.router,
        "_turn_provider",
        lambda channel, chat_id: turn_b.to_ref(channel="whatsapp", chat_id=CHAT),
    )
    runtime.router.remember_turn_for_source(
        "m1", TurnBinding(turn=turn_a, trace_id=turn_a.turn_id, generation_id="gen-a")
    )
    source = runtime.store.get_event("m1")
    runtime.history.add_message(
        chat_id=CHAT, message_id=source.source_message_id or "m1", text="hi",
        sender_id=ORDERER, timestamp=source.occurred_ms or 1,
    )

    await runtime.router.submit_outbound(
        SendOutboundIntent(
            event=OutboundEvent(
                channel="whatsapp", chat_id=CHAT, content="answer",
                metadata={"message_id": "m1"},
            )
        ),
        principal=ORDERER,
    )

    effects = [effect for effect in runtime.store.list_effects() if effect.turn_id]
    assert [effect.turn_id for effect in effects] == [turn_a.turn_id]
