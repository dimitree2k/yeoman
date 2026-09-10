"""Tests for implicit bot-address handling in mention-only chats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from yeoman_gateway.core.intents import QueueMemoryNotesCaptureIntent, SendReactionIntent
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.access import AccessControlMiddleware
from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware


def _event(**overrides: object) -> InboundEvent:
    payload: dict[str, object] = {
        "channel": "whatsapp",
        "chat_id": "group@g.us",
        "sender_id": "user@s.whatsapp.net",
        "content": "hello",
        "message_id": "msg-1",
        "timestamp": datetime(2026, 5, 2, 18, 0, tzinfo=UTC),
        "participant": "user@s.whatsapp.net",
        "is_group": True,
    }
    payload.update(overrides)
    return InboundEvent(**payload)  # type: ignore[arg-type]


def _mention_only_decision(**overrides: object) -> PolicyDecision:
    payload: dict[str, object] = {
        "accept_message": True,
        "should_respond": False,
        "allowed_tools": frozenset(),
        "reason": "when_to_reply:mention_only_group",
        "when_to_reply_mode": "mention_only",
    }
    payload.update(overrides)
    return PolicyDecision(**payload)  # type: ignore[arg-type]


async def _tracking_next(ctx: PipelineContext) -> None:
    ctx.reply = "downstream reached"


async def _noop_next(ctx: PipelineContext) -> None:
    del ctx


@dataclass
class _Session:
    messages: list[dict[str, object]]


class _Sessions:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = messages

    def get_or_create(self, key: str) -> _Session:
        assert key == "whatsapp:group@g.us"
        return _Session(messages=self._messages)


@pytest.mark.asyncio
async def test_plain_arvid_request_wakes_mention_only_policy() -> None:
    ctx = PipelineContext(
        event=_event(content="Arvid kannst du Nokia kurz checken"),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.mentioned_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "plain_name_request"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_plain_name_request"


@pytest.mark.asyncio
async def test_plain_arvid_mach_mal_request_wakes_mention_only_policy() -> None:
    ctx = PipelineContext(
        event=_event(content="Arvid, mach mal Bewertung von dem Portfolio bitte"),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.mentioned_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "plain_name_request"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "plain_name_request"
    assert state["preferred_action"] == "answer"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True


@pytest.mark.asyncio
async def test_arvid_request_with_quoted_image_context_wakes_mention_only_policy() -> None:
    ctx = PipelineContext(
        event=_event(
            content="Arvid, mach mal Bewertung von dem Portfolio bitte",
            reply_to_message_id="img-1",
            raw_metadata={
                "reply_to_message_id": "img-1",
                "reply_to_text": (
                    "[Image]\n"
                    "[image_description] This image shows a digital spreadsheet titled "
                    '"Dividenden und Zinsen 2026" with companies, share counts, '
                    "monthly payouts, and annual dividend totals."
                ),
                "reply_context_source": "archive",
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.mentioned_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "quoted_context_request"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "quoted_context_request"
    assert state["preferred_action"] == "answer"
    assert state["answer_shape"] == "short_take"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_quoted_context_request"


@pytest.mark.asyncio
async def test_deictic_arvid_reply_to_quoted_image_wakes_mention_only_policy() -> None:
    ctx = PipelineContext(
        event=_event(
            content="von dem hier, Arvid",
            reply_to_message_id="img-1",
            raw_metadata={
                "reply_to_message_id": "img-1",
                "reply_to_text": (
                    "[Image]\n"
                    "[image_description] This image shows a digital spreadsheet titled "
                    '"Dividenden und Zinsen 2026" with companies, share counts, '
                    "monthly payouts, and annual dividend totals."
                ),
                "reply_context_source": "archive",
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.mentioned_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "quoted_context_request"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "quoted_context_request"
    assert state["preferred_action"] == "answer"
    assert state["answer_shape"] == "short_take"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_quoted_context_request"


@pytest.mark.asyncio
async def test_explicit_mention_gets_conversation_state_without_extra_promotion() -> None:
    ctx = PipelineContext(
        event=_event(content="@203075365150770 check mal eBay", mentioned_bot=True),
        decision=_mention_only_decision(should_respond=True, reason="when_to_reply:mentioned_bot"),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["addressed_to_bot"] is True
    assert state["address_mode"] == "explicit_mention"
    assert state["preferred_action"] == "answer"
    assert state["answer_shape"] == "short_take"
    assert ctx.decision is not None
    assert ctx.decision.reason == "when_to_reply:mentioned_bot"


@pytest.mark.asyncio
async def test_explicit_mention_on_social_image_without_question_gets_social_one_liner() -> None:
    ctx = PipelineContext(
        event=_event(
            content=(
                "[Image] @203075365150770\n"
                "[image_description] This image is a screenshot of a social media post "
                'featuring side-by-side photos. The text above the images reads, '
                '"Andrej Karpathy is the Sydney Sweeney of AI."'
            ),
            mentioned_bot=True,
            raw_metadata={"media_kind": "image"},
        ),
        decision=_mention_only_decision(should_respond=True, reason="when_to_reply:mentioned_bot"),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["addressed_to_bot"] is True
    assert state["address_mode"] == "explicit_social_mention"
    assert state["preferred_action"] == "answer"
    assert state["answer_shape"] == "social_one_liner"
    assert ctx.decision is not None
    assert ctx.decision.reason == "when_to_reply:mentioned_bot"


@pytest.mark.asyncio
async def test_explicit_mention_on_social_image_with_question_keeps_short_take() -> None:
    ctx = PipelineContext(
        event=_event(
            content=(
                "[Image] @203075365150770 wie findest du das?\n"
                "[image_description] This image is a screenshot of a social media post "
                'featuring side-by-side photos. The text above the images reads, '
                '"Andrej Karpathy is the Sydney Sweeney of AI."'
            ),
            mentioned_bot=True,
            raw_metadata={"media_kind": "image"},
        ),
        decision=_mention_only_decision(should_respond=True, reason="when_to_reply:mentioned_bot"),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "explicit_mention"
    assert state["answer_shape"] == "short_take"


@pytest.mark.asyncio
async def test_question_without_question_mark_after_recent_assistant_reply_wakes() -> None:
    event_time = datetime(2026, 5, 2, 18, 0, 8, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Nokia ist kurzfristig eher News-getrieben.",
                "timestamp": (event_time - timedelta(seconds=8)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(content="was meinst du bei Intel", timestamp=event_time),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.reply_to_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "recent_assistant_followup"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_recent_assistant_followup"


@pytest.mark.asyncio
async def test_question_after_recent_assistant_thread_wakes_for_real_group_followup() -> None:
    event_time = datetime(2026, 5, 25, 8, 52, 34, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Das ist eine Generationenbilanz über 30+ Jahre.",
                "timestamp": (event_time - timedelta(seconds=52)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content=(
                "Bei derzeitiger Lage 30 Jahre im voraus rechnen ist anders "
                "ambitioniert. Welche Technologien gab es vor 30 Jahren so noch nicht? 😅"
            ),
            timestamp=event_time,
            sender_id="frank@s.whatsapp.net",
            participant="frank@s.whatsapp.net",
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.reply_to_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "recent_assistant_followup"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_recent_assistant_followup"


@pytest.mark.asyncio
async def test_question_after_ten_minutes_wakes_when_bot_thread_is_uninterrupted() -> None:
    event_time = datetime(2026, 5, 25, 9, 10, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Das ist eine Generationenbilanz über 30+ Jahre.",
                "timestamp": (event_time - timedelta(minutes=10)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content="Welche Technologien gab es vor 30 Jahren noch nicht?",
            timestamp=event_time,
            raw_metadata={
                "ambient_context_rows": [
                    {
                        "sender_id": None,
                        "participant": "203075365150770@lid",
                        "text": "Das ist eine Generationenbilanz über 30+ Jahre.",
                    }
                ]
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.reply_to_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "recent_assistant_followup"


@pytest.mark.asyncio
async def test_followup_ignores_humans_before_last_assistant_reply() -> None:
    event_time = datetime(2026, 7, 28, 0, 3, 47, tzinfo=UTC)
    assistant_text = "Ich kann das gerade nicht verlässlich beantworten."
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": assistant_text,
                "timestamp": (event_time - timedelta(minutes=13)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content="Und jetzt?",
            timestamp=event_time,
            raw_metadata={
                "ambient_context_rows": [
                    {
                        "sender_id": "owner@s.whatsapp.net",
                        "participant": "owner@s.whatsapp.net",
                        "text": "Ok bitte mehr Details zum sell-off",
                    },
                    {
                        "sender_id": "assistant",
                        "sender_name": "Yeoman",
                        "participant": None,
                        "text": assistant_text,
                    },
                ]
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.reply_to_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "recent_assistant_followup"


@pytest.mark.asyncio
async def test_human_copy_of_last_assistant_text_does_not_reset_anchor() -> None:
    event_time = datetime(2026, 7, 28, 0, 3, 47, tzinfo=UTC)
    assistant_text = "Ich kann das gerade nicht verlässlich beantworten."
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": assistant_text,
                "timestamp": (event_time - timedelta(minutes=13)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content="Und jetzt?",
            timestamp=event_time,
            raw_metadata={
                "ambient_context_rows": [
                    {
                        "sender_id": "assistant",
                        "sender_name": "Yeoman",
                        "participant": None,
                        "text": assistant_text,
                    },
                    {
                        "sender_id": "other@s.whatsapp.net",
                        "participant": "other@s.whatsapp.net",
                        "text": "Ganz anderes Thema",
                    },
                    {
                        "sender_id": "owner@s.whatsapp.net",
                        "participant": "owner@s.whatsapp.net",
                        "text": assistant_text,
                    },
                ]
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.event.reply_to_bot is False
    assert ctx.decision is not None
    assert ctx.decision.should_respond is False


@pytest.mark.asyncio
async def test_question_after_intervening_human_message_stays_silent() -> None:
    event_time = datetime(2026, 5, 25, 8, 53, 30, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Das ist eine Generationenbilanz über 30+ Jahre.",
                "timestamp": (event_time - timedelta(seconds=60)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content="Was ist mit dem Depot?",
            timestamp=event_time,
            raw_metadata={
                "ambient_context_rows": [
                    {
                        "sender_id": None,
                        "participant": "203075365150770@lid",
                        "text": "Das ist eine Generationenbilanz über 30+ Jahre.",
                    },
                    {
                        "sender_id": "4915774497527",
                        "participant": "4915774497527@s.whatsapp.net",
                        "text": "Onlyfans",
                    },
                ]
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _noop_next)

    assert ctx.reply is None
    assert ctx.event.reply_to_bot is False
    assert ctx.decision is not None
    assert ctx.decision.should_respond is False


@pytest.mark.asyncio
async def test_same_topic_question_after_intervening_humans_still_wakes() -> None:
    event_time = datetime(2026, 6, 23, 9, 42, 40, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": (
                    "Vorteile: Miete weg, drei Mahlzeiten am Tag, Struktur, "
                    "kein Altersarmuts-Risiko. Nachteile: Knast, keine Freiheit, "
                    "Steak ist eher schwierig und Rente wird dadurch nicht besser."
                ),
                "timestamp": (event_time - timedelta(seconds=93)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(
            content=(
                "Mit Altersarmut gibt's auch kein Steak, musst auch um 6 aufstehen "
                "um die Pfandflaschen der Partygänger einzusammeln. Bekommt man "
                "keine Rente wenn man im Knast sitzt?"
            ),
            timestamp=event_time,
            sender_id="genti@s.whatsapp.net",
            participant="genti@s.whatsapp.net",
            raw_metadata={
                "ambient_context_rows": [
                    {
                        "sender_id": None,
                        "participant": "203075365150770@lid",
                        "text": (
                            "Vorteile: Miete weg, drei Mahlzeiten am Tag, Struktur, "
                            "kein Altersarmuts-Risiko. Nachteile: Knast, keine Freiheit, "
                            "Steak ist eher schwierig und Rente wird dadurch nicht besser."
                        ),
                    },
                    {
                        "sender_id": "genti@s.whatsapp.net",
                        "participant": "genti@s.whatsapp.net",
                        "text": "Kirche auf der sahne",
                    },
                    {
                        "sender_id": "alex@s.whatsapp.net",
                        "participant": "alex@s.whatsapp.net",
                        "text": (
                            "Wenn man Bock hat und möchte, wieso nicht. Wer muss, "
                            "weil es sonst nicht hinhaut, hat gelitten"
                        ),
                    },
                ]
            },
        ),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.reply_to_bot is True
    assert ctx.event.raw_metadata["implicit_bot_address"] == "recent_assistant_followup"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True


@pytest.mark.asyncio
async def test_non_question_after_recent_assistant_thread_stays_silent() -> None:
    event_time = datetime(2026, 5, 25, 8, 52, 48, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Das ist eine Generationenbilanz über 30+ Jahre.",
                "timestamp": (event_time - timedelta(seconds=66)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(content="Onlyfans", timestamp=event_time),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _noop_next)

    assert ctx.reply is None
    assert ctx.event.reply_to_bot is False
    assert ctx.decision is not None
    assert ctx.decision.should_respond is False


@pytest.mark.asyncio
async def test_side_effect_request_after_recent_assistant_thread_stays_silent() -> None:
    event_time = datetime(2026, 5, 25, 8, 52, 48, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "Die Zusammenfassung steht.",
                "timestamp": (event_time - timedelta(seconds=20)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(content="Kannst du das an Ente schicken?", timestamp=event_time),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _noop_next)

    assert ctx.reply is None
    assert ctx.event.reply_to_bot is False
    assert ctx.decision is not None
    assert ctx.decision.should_respond is False


@pytest.mark.asyncio
async def test_recent_negative_feedback_to_arvid_wakes_repair_turn() -> None:
    event_time = datetime(2026, 5, 2, 18, 0, 16, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "```deep_research(query='eBay')```",
                "timestamp": (event_time - timedelta(seconds=16)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(content="keine gute antwort Arvid", timestamp=event_time),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _tracking_next)

    assert ctx.reply == "downstream reached"
    assert ctx.event.raw_metadata["implicit_bot_address"] == "repair_feedback"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["addressed_to_bot"] is True
    assert state["address_mode"] == "repair_feedback"
    assert state["preferred_action"] == "answer"
    assert state["answer_shape"] == "repair"
    assert ctx.decision is not None
    assert ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_repair_feedback"


@pytest.mark.asyncio
async def test_plain_arvid_non_request_gets_reaction_only() -> None:
    ctx = PipelineContext(
        event=_event(content="Arvid Moment"),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    reactions = [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert ctx.halted is True
    assert ctx.reply is None
    assert len(reactions) == 1
    assert reactions[0].message_id == "msg-1"
    assert reactions[0].emoji in {"🤔", "🙄", "👀"}


@pytest.mark.asyncio
async def test_short_ack_reply_to_bot_gets_reaction_only() -> None:
    ctx = PipelineContext(
        event=_event(
            content="Ok",
            reply_to_bot=True,
            reply_to_message_id="bot-msg-1",
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    reactions = [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert ctx.halted is True
    assert ctx.reply is None
    assert len(reactions) == 1
    assert reactions[0].message_id == "msg-1"
    assert reactions[0].emoji == "👍"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "reply_ack"
    assert state["preferred_action"] == "react"


@pytest.mark.asyncio
async def test_hesitation_reply_to_bot_gets_reaction_only() -> None:
    ctx = PipelineContext(
        event=_event(
            content="Ähm",
            reply_to_bot=True,
            reply_to_message_id="bot-msg-1",
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    reactions = [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert ctx.halted is True
    assert ctx.reply is None
    assert len(reactions) == 1
    assert reactions[0].emoji == "🤔"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "reply_ack"


@pytest.mark.asyncio
async def test_ente_replay_meme_reply_to_bot_gets_reaction_only() -> None:
    ctx = PipelineContext(
        event=_event(
            chat_id="1234567890-1234567890@g.us",
            content="Death by Snu Snu",
            reply_to_bot=True,
            reply_to_message_id="bot-msg-ente-1",
            raw_metadata={"reply_to_text": "Wer hat das Thema eigentlich angefangen?"},
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    reactions = [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert ctx.halted is True
    assert ctx.reply is None
    assert len(reactions) == 1
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "low_content_reply"
    assert state["preferred_action"] == "react"


@pytest.mark.asyncio
async def test_reply_to_bot_with_clear_question_still_answers() -> None:
    ctx = PipelineContext(
        event=_event(
            content="Was meinst du konkret mit Proteinshake?",
            reply_to_bot=True,
            reply_to_message_id="bot-msg-1",
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    assert ctx.halted is False
    assert ctx.reply == "downstream reached"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "reply_to_bot"
    assert state["preferred_action"] == "answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "event_overrides"),
    [
        (
            "Ok",
            {"reply_to_bot": True, "reply_to_message_id": "bot-msg-1"},
        ),
        (
            "@123456789012345 wer hier in der gruppe geht dir am meisten auf den sack",
            {"mentioned_bot": True},
        ),
        (
            "Death by Snu Snu",
            {
                "reply_to_bot": True,
                "reply_to_message_id": "bot-msg-1",
                "raw_metadata": {"reply_to_text": "Wer hat das Thema angefangen?"},
            },
        ),
    ],
)
async def test_denied_early_bait_reaches_access_control(
    content: str,
    event_overrides: dict[str, object],
) -> None:
    ctx = PipelineContext(
        event=_event(content=content, **event_overrides),
        decision=_mention_only_decision(
            accept_message=False,
            should_respond=False,
            notes_enabled=True,
            notes_allow_blocked_senders=True,
        ),
    )

    async def access_control_next(current: PipelineContext) -> None:
        await AccessControlMiddleware()(current, _tracking_next)

    await ImplicitBotAddressMiddleware()(ctx, access_control_next)

    assert ctx.halted is True
    assert ctx.reply is None
    assert not [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert [
        intent for intent in ctx.intents if isinstance(intent, QueueMemoryNotesCaptureIntent)
    ]


@pytest.mark.asyncio
async def test_repeated_bait_reactions_enter_short_group_cooldown() -> None:
    middleware = ImplicitBotAddressMiddleware(
        bait_reaction_streak_threshold=2,
        bait_reaction_cooldown_seconds=300,
    )
    base = datetime(2026, 7, 20, 19, 24, tzinfo=UTC)

    for index, content in enumerate(["Ok", "Ähm"]):
        ctx = PipelineContext(
            event=_event(
                content=content,
                message_id=f"msg-{index + 1}",
                timestamp=base + timedelta(seconds=index * 5),
                reply_to_bot=True,
                reply_to_message_id="bot-msg-1",
            ),
            decision=_mention_only_decision(should_respond=True),
        )

        await middleware(ctx, _tracking_next)

        assert len([i for i in ctx.intents if isinstance(i, SendReactionIntent)]) == 1

    cooled_ctx = PipelineContext(
        event=_event(
            content="Death by Snu Snu",
            message_id="msg-3",
            timestamp=base + timedelta(seconds=10),
            reply_to_bot=True,
            reply_to_message_id="bot-msg-1",
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await middleware(cooled_ctx, _tracking_next)

    assert cooled_ctx.halted is True
    assert cooled_ctx.reply is None
    assert [i for i in cooled_ctx.intents if isinstance(i, SendReactionIntent)] == []
    state = cooled_ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "bait_cooldown"
    assert state["preferred_action"] == "silence"


@pytest.mark.asyncio
async def test_group_member_ranking_bait_gets_reaction_only() -> None:
    ctx = PipelineContext(
        event=_event(
            content="@123456789012345 wer hier in der gruppe geht dir am meisten auf den sack",
            mentioned_bot=True,
        ),
        decision=_mention_only_decision(should_respond=True),
    )

    await ImplicitBotAddressMiddleware()(ctx, _tracking_next)

    reactions = [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]
    assert ctx.halted is True
    assert ctx.reply is None
    assert len(reactions) == 1
    assert reactions[0].emoji == "🙄"
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["address_mode"] == "group_member_bait"
    assert state["preferred_action"] == "react"


@pytest.mark.asyncio
async def test_old_assistant_reply_does_not_wake_followup() -> None:
    event_time = datetime(2026, 5, 2, 18, 20, tzinfo=UTC)
    sessions = _Sessions(
        [
            {
                "role": "assistant",
                "content": "GME ist deutlich kleiner als eBay.",
                "timestamp": (event_time - timedelta(minutes=20)).isoformat(),
            }
        ]
    )
    ctx = PipelineContext(
        event=_event(content="was meinst du bei Intel", timestamp=event_time),
        decision=_mention_only_decision(),
    )

    await ImplicitBotAddressMiddleware(session_manager=sessions)(ctx, _noop_next)

    assert ctx.reply is None
    assert ctx.event.reply_to_bot is False
    assert ctx.decision is not None
    assert ctx.decision.should_respond is False
