"""The ``state: off`` view: only the long-term blocks, as the evaluation arms saw them."""

from __future__ import annotations

from loguru import logger
from yeoman_gateway.agent.untrusted import EXTERNAL_START
from yeoman_gateway.group_state.views import (
    MAX_PAST_CHARS,
    ViewTarget,
    _choose_conversation,
    _conversation_rows,
    _int,
    _linked_conversation_ids,
    render_past_block,
)
from yeoman_shared.config.schema import Config, GroupStateConfig

from tests.gateway.group_state.test_views import CHAT, LULL, M1, NOW
from tests.gateway.test_long_term_views import (
    TOM,
    _Knowledge,
    _note,
    _past_state,
    _Queries,
    _render,
)


def _settings(*, state: str = "off", people: str = "off", past: str = "on") -> GroupStateConfig:
    return GroupStateConfig.model_validate(
        {
            "mode": "shadow",
            "chats": [f"whatsapp:{CHAT}"],
            "views": {
                "reply": {
                    "mode": "on",
                    "chats": [f"whatsapp:{CHAT}"],
                    "maxChars": 4_000,
                    "people": people,
                    "past": past,
                    "state": state,
                }
            },
        }
    )


def test_state_defaults_on_and_round_trips() -> None:
    views = Config().processing.group_state.views
    assert views.reply.state == "on" and views.writer.state == "on"
    settings = GroupStateConfig.model_validate({"views": {"writer": {"state": "off"}}})
    assert settings.views.writer.state == "off"
    assert Config().processing.group_state.summaries.chats == []


def test_state_off_renders_exactly_the_evaluated_past_block() -> None:
    state = _past_state()
    target = ViewTarget(message_id=M1, sender_principal=TOM)

    text, rendered = _render(settings=_settings(), target=target, state=state)

    conversation = _choose_conversation(_conversation_rows(state), target, now_ms=NOW, lull_ms=LULL)
    assert conversation is not None
    current = str(conversation.get("conversation_id") or "")
    expected = render_past_block(
        state.sections["past"].items,
        linked_conversation_ids=_linked_conversation_ids(state, target),
        older_than_ms=_int(conversation.get("start_ms")),
        exclude_conversation_ids=(current,) if current else (),
        now_ms=NOW,
        max_chars=MAX_PAST_CHARS,
    )
    assert text == expected
    assert "they planned a trip" in text
    assert "[Group State]" not in text and EXTERNAL_START not in text
    assert rendered.long_term == ("past",)
    assert rendered.revision == state.revision


def test_state_off_with_people_puts_people_first() -> None:
    knowledge = _Knowledge((_note("p-tom", "Tom: likes charts"),))

    text, rendered = _render(
        settings=_settings(people="on"), knowledge=knowledge, queries=_Queries()
    )

    assert text is not None
    assert text.index("[People here]") < text.index("[Earlier in this group]")
    assert "[Group State]" not in text
    assert rendered.long_term == ("people", "past")


def test_state_off_without_long_term_layers_sends_nothing() -> None:
    text, rendered = _render(settings=_settings(past="off"))

    assert text is None
    assert rendered.fallback == "empty" and rendered.long_term == ()


def test_state_off_without_summaries_sends_nothing() -> None:
    state = _past_state()
    sections = {key: value for key, value in state.sections.items() if key != "past"}
    from tests.gateway.group_state.test_views import _state

    text, rendered = _render(settings=_settings(), state=_state(sections))

    assert text is None
    assert rendered.long_term == ()


def test_state_on_still_reports_long_term_presence() -> None:
    text, rendered = _render(settings=_settings(state="on"))

    assert text is not None and "[Group State]" in text
    assert rendered.long_term == ("past",)


def test_log_line_carries_revision_and_long_term() -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        _, rendered = _render(settings=_settings())
    finally:
        logger.remove(sink)

    view_lines = [line for line in lines if "group_state_view " in line]
    assert view_lines
    assert f"revision={rendered.revision}" in view_lines[-1]
    assert "long_term=past" in view_lines[-1]
