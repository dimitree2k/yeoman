"""The owner's reaction vocabulary: one list, one decision, no guessing.

A reaction is the cheapest thing Arvid can send and the loudest per character, so the model
picks an emoji but does not invent the vocabulary. These tests pin the contract at both
boundaries where a model-chosen emoji could become a real reaction - the outbound pipeline
(the ``::reaction::`` marker) and the managed effect path - and they pin the shipped default
list, because shrinking it silently would change live behaviour on the next restart.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from yeoman_gateway.core.intents import (
    PersistSessionIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SendReactionIntent,
)
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_shared.config.schema import Config
from yeoman_shared.reactions import (
    DEFAULT_REACTION_EMOJIS,
    allowed_reaction,
    looks_like_emoji,
)

#: The reactions the runtime personas prescribe by name (boys-club, omega, TEMPLATE,
#: natalias-boyfriend). The shipped vocabulary must cover them, otherwise deploying this
#: silently removes reactions that real chats already receive.
PERSONA_EMOJIS = ("🤙", "😏", "😎", "🥱", "🤔", "👍", "💀", "🤷", "😌", "😄")


def _event(*, content: str = "incoming", message_id: str = "msg-1") -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="456",
        content=content,
        message_id=message_id,
        is_group=True,
    )


def _decision() -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
    )


async def _run_outbound(reply: str, *, allowed: list[str] | None = None) -> PipelineContext:
    ctx = PipelineContext(event=_event())
    ctx.decision = _decision()
    ctx.reply = reply
    middleware = OutboundMiddleware(allowed_reaction_emojis=allowed)

    async def _noop(_ctx: PipelineContext) -> None:
        return None

    await middleware(ctx, _noop)
    return ctx


def _metrics(ctx: PipelineContext) -> set[str]:
    return {intent.name for intent in ctx.intents if isinstance(intent, RecordMetricIntent)}


def test_the_shipped_vocabulary_covers_every_reaction_the_personas_use() -> None:
    """The default list is not decoration: it is what the live personas already send."""
    missing = [emoji for emoji in PERSONA_EMOJIS if emoji not in DEFAULT_REACTION_EMOJIS]
    assert not missing, f"the shipped vocabulary lost persona reactions: {missing}"


def test_the_configuration_defaults_to_the_shipped_vocabulary() -> None:
    config = Config.model_validate({"processing": {"enabled": True}})
    assert config.processing.reaction_emojis == list(DEFAULT_REACTION_EMOJIS)


def test_the_configuration_refuses_a_value_that_is_not_an_emoji() -> None:
    """A typo would silently shrink the vocabulary; it fails loudly instead."""
    with pytest.raises(ValidationError) as error:
        Config.model_validate({"processing": {"reaction_emojis": ["thumbsup"]}})
    assert "processing.reactionEmojis" in str(error.value)


def test_the_configuration_accepts_an_empty_list_as_no_reactions() -> None:
    config = Config.model_validate({"processing": {"reaction_emojis": []}})
    assert config.processing.reaction_emojis == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("👍", "👍"),
        ("  🤙  ", "🤙"),
        ("👍🏽", "👍🏽"),  # a skin-tone modifier is still one emoji
        ("❤️", "❤️"),  # a variation selector is still one emoji
        ("🤖", None),  # a real emoji the owner did not approve
        ("thumbsup", None),  # a word that names a face
        ("👍 thanks", None),  # a phrase
        ("", None),
        ("   ", None),
    ],
)
def test_only_an_approved_emoji_survives(value: str, expected: str | None) -> None:
    approved = ["👍", "🤙", "👍🏽", "❤️"]
    assert allowed_reaction(value, approved) == expected


def test_an_empty_vocabulary_approves_nothing() -> None:
    assert allowed_reaction("👍", []) is None


def test_a_word_is_never_mistaken_for_an_emoji() -> None:
    assert looks_like_emoji("🥱") is True
    assert looks_like_emoji("yawning_face") is False
    assert looks_like_emoji("") is False


@pytest.mark.asyncio
async def test_the_marker_sends_an_approved_emoji() -> None:
    ctx = await _run_outbound("`::reaction::🤙`", allowed=["🤙"])

    reactions = [i for i in ctx.intents if isinstance(i, SendReactionIntent)]
    assert [reaction.emoji for reaction in reactions] == ["🤙"]
    assert "reaction_sent" in _metrics(ctx)


@pytest.mark.asyncio
async def test_the_marker_drops_an_unapproved_emoji_and_logs_it() -> None:
    from loguru import logger

    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    try:
        ctx = await _run_outbound("`::reaction::🤖`", allowed=["🤙"])
    finally:
        logger.remove(sink)

    assert [i for i in ctx.intents if isinstance(i, SendReactionIntent)] == []
    assert "reaction_dropped" in _metrics(ctx)
    dropped = [line for line in records if "reaction_dropped" in line]
    assert dropped, f"a dropped reaction must be observable: {records}"


@pytest.mark.asyncio
async def test_a_dropped_reaction_only_reply_sends_nothing_at_all() -> None:
    """No guessed face, and no marker leaked as text either - the reply is silence."""
    ctx = await _run_outbound("`::reaction::🤖`", allowed=["🤙"])

    assert [i for i in ctx.intents if isinstance(i, SendOutboundIntent)] == []
    persist = [i for i in ctx.intents if isinstance(i, PersistSessionIntent)]
    assert [entry.assistant_content for entry in persist] == ["[silence]"]


@pytest.mark.asyncio
async def test_text_beside_a_dropped_reaction_is_still_sent() -> None:
    ctx = await _run_outbound("::reaction::🤖\n\nKurz und sachlich.", allowed=["🤙"])

    assert [i for i in ctx.intents if isinstance(i, SendReactionIntent)] == []
    sends = [i for i in ctx.intents if isinstance(i, SendOutboundIntent)]
    assert [send.event.content for send in sends] == ["Kurz und sachlich."]


@pytest.mark.asyncio
async def test_the_default_vocabulary_keeps_the_persona_reactions_working() -> None:
    """Without configuration the middleware behaves exactly as the personas expect."""
    ctx = await _run_outbound("`::reaction::💀`")

    reactions = [i for i in ctx.intents if isinstance(i, SendReactionIntent)]
    assert [reaction.emoji for reaction in reactions] == ["💀"]


# the react reply action -----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeProvider:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self.answer)


def _chooser(answer: str):
    """A chooser whose provider answers with a fixed string."""
    from yeoman_gateway.processing.reaction_action import ReactionChooser

    chooser = ReactionChooser.__new__(ReactionChooser)
    chooser._config = None  # type: ignore[attr-defined]
    chooser._route_key = "test.route"  # type: ignore[attr-defined]
    chooser._timeout_seconds = 5.0  # type: ignore[attr-defined]
    chooser._timeout_ms = 0  # type: ignore[attr-defined]
    chooser._model = "test-model"  # type: ignore[attr-defined]
    chooser._provider = _FakeProvider(answer)  # type: ignore[attr-defined]
    return chooser


@pytest.mark.asyncio
async def test_the_chooser_asks_for_exactly_one_approved_emoji() -> None:
    chooser = _chooser("🤙")
    assert await chooser.choose("danke dir", allowed=["🤙", "🥱"]) == "🤙"

    call = chooser._provider.calls[0]  # type: ignore[attr-defined]
    assert call["max_tokens"] == 8, "a reaction choice must not be able to write prose"
    assert call["temperature"] == 0.0
    system = str(call["messages"][0]["content"])  # type: ignore[index]
    assert "🤙" in system and "🥱" in system, "the model must see the approved vocabulary"


@pytest.mark.asyncio
async def test_the_chooser_refuses_an_emoji_the_owner_did_not_approve() -> None:
    assert await _chooser("🤖").choose("irgendwas", allowed=["🤙"]) is None
    assert await _chooser("none").choose("irgendwas", allowed=["🤙"]) is None
    assert await _chooser("Der Text ist zu lang").choose("x", allowed=["🤙"]) is None


@pytest.mark.asyncio
async def test_the_chooser_never_calls_the_model_without_a_vocabulary() -> None:
    chooser = _chooser("🤙")
    assert await chooser.choose("hallo", allowed=[]) is None
    assert chooser._provider.calls == []  # type: ignore[attr-defined]


# the ambient judge ----------------------------------------------------------------------


def _judge(answer: str, *, min_confidence: float = 0.75, allowed: tuple[str, ...] = ("🤙", "👍")):
    from yeoman_gateway.processing.ambient_judge import AmbientJudge
    from yeoman_gateway.processing.model_route import RouteClient

    client = RouteClient.__new__(RouteClient)
    client.route_key = "test.route"  # type: ignore[attr-defined]
    client.model = "test-model"  # type: ignore[attr-defined]
    client.timeout_ms = 0  # type: ignore[attr-defined]
    client._provider = _FakeProvider(answer)  # type: ignore[attr-defined]
    return AmbientJudge(
        client=client,
        allowed_emojis=allowed,
        min_confidence=min_confidence,
        timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_the_ambient_judge_can_answer_react_or_stay_silent() -> None:
    """One call, three outcomes - a reaction must not cost an answer turn."""
    answered = await _judge('{"action": "answer", "confidence": 0.9}').decide("Frage?")
    assert answered.action == "answer" and answered.needs_turn is True

    reacted = await _judge('{"action": "react", "emoji": "🤙", "confidence": 0.9}').decide("danke")
    assert reacted.action == "react" and reacted.emoji == "🤙"
    assert reacted.needs_turn is False and reacted.speaks is True

    silent = await _judge('{"action": "none", "confidence": 0.9}').decide("neues Thema")
    assert silent.action == "silence" and silent.speaks is False


@pytest.mark.asyncio
async def test_the_ambient_judge_needs_confidence_for_every_action() -> None:
    assert (await _judge('{"action": "answer", "confidence": 0.4}').decide("Frage?")).action == (
        "silence"
    )
    assert (
        await _judge('{"action": "react", "emoji": "👍", "confidence": 0.4}').decide("danke")
    ).action == "silence"


@pytest.mark.asyncio
async def test_the_ambient_judge_only_reacts_with_approved_emojis() -> None:
    """An unapproved face is not a reaction - it is silence, never a guess."""
    unapproved = await _judge('{"action": "react", "emoji": "🤖", "confidence": 0.99}').decide("x")
    assert unapproved.action == "silence"

    no_emoji = await _judge('{"action": "react", "confidence": 0.99}').decide("x")
    assert no_emoji.action == "silence"


@pytest.mark.asyncio
async def test_the_ambient_judge_fails_closed() -> None:
    assert (await _judge("klar, antworte!").decide("Frage?")).action == "silence"
    assert (await _judge("").decide("Frage?")).action == "silence"
    assert (await _judge('{"action": "answer", "confidence": 0.9}').decide("")).action == "silence"
    assert (await _judge('{"action": "vielleicht", "confidence": 0.9}').decide("x")).action == (
        "silence"
    )


def test_the_ambient_judge_prompt_forbids_answering_just_because_it_is_new() -> None:
    from yeoman_gateway.processing.ambient_judge import AMBIENT_JUDGE_PROMPT

    assert "Ein neues Thema ist kein Grund zu antworten." in AMBIENT_JUDGE_PROMPT
    assert "action=none" in AMBIENT_JUDGE_PROMPT
    assert "Eine Reaktion ist kein Ersatz für eine" in AMBIENT_JUDGE_PROMPT


@pytest.mark.asyncio
async def test_the_ambient_judge_sees_the_approved_emojis() -> None:
    judge = _judge('{"action": "none", "confidence": 0.9}', allowed=("👍", "💀"))
    await judge.decide("hallo")

    system = str(judge._client._provider.calls[0]["messages"][0]["content"])  # type: ignore[attr-defined]
    assert "👍" in system and "💀" in system, "the judge must know what it may send"
