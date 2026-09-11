"""Plan 07 / Aufgabe 2: continuity is decided by proven sources, never by keywords."""

from __future__ import annotations

from yeoman_gateway.processing.routing import (
    SIGNAL_ANSWER,
    SIGNAL_CALLBACK,
    VERDICT_NONE,
    VERDICT_POSITIVE,
    BotMessageView,
    SourceView,
    continuity_signal,
    explicit_callback,
    question_options,
)

T0 = 1_700_000_000_000

TRIGGER = SourceView(
    event_id="ev-contract",
    text="Fasse den Mietvertrag zusammen.",
    role="trigger",
    principal="orderer",
    occurred_ms=T0,
)


def _signal(text: str, *, sources=(TRIGGER,), bot=()):
    return continuity_signal(current_text=text, thread_sources=list(sources), bot_messages=list(bot))


# -- signal 1: answer to a concrete question ------------------------------------------------


def test_positive_example_from_the_spec_answers_the_question() -> None:
    """"Soll die Zusammenfassung kurz oder ausführlich sein?" -> "kurz"."""
    question = BotMessageView(
        message_id="fx-question",
        text="Soll die Zusammenfassung kurz oder ausführlich sein?",
        occurred_ms=T0 + 1000,
    )

    result = _signal("kurz", bot=[question])

    assert result.kind == SIGNAL_ANSWER
    assert result.verdict == VERDICT_POSITIVE
    assert result.evidence_ids == ("fx-question",)


def test_question_options_are_parsed_from_both_shapes() -> None:
    assert question_options("Soll ich kurz oder ausführlich antworten?") == ("kurz", "ausführlich")
    assert question_options("Welche Sprache? (deutsch/englisch)") == ("deutsch", "englisch")
    assert question_options("Wie geht es weiter?") is None


def test_a_question_without_named_options_is_not_an_expectation() -> None:
    question = BotMessageView(message_id="fx", text="Wie möchtest du weitermachen?", occurred_ms=T0 + 1)

    assert _signal("mit dem Vertrag", bot=[question]).verdict == VERDICT_NONE


def test_an_already_answered_question_carries_no_signal() -> None:
    """Spec: a question that a later source already answered is lifted."""
    question = BotMessageView(
        message_id="fx-question", text="kurz oder ausführlich?", occurred_ms=T0 + 1000
    )
    later = SourceView(
        event_id="ev-answered", text="kurz bitte", role="context", occurred_ms=T0 + 2000
    )

    result = _signal("kurz", sources=(TRIGGER, later), bot=[question])

    assert result.verdict == VERDICT_NONE
    assert result.detail == "question_already_answered"


def test_a_message_that_does_not_fulfil_the_expectation_is_not_a_signal() -> None:
    question = BotMessageView(
        message_id="fx", text="Soll die Zusammenfassung kurz oder ausführlich sein?", occurred_ms=T0 + 1
    )

    assert _signal("Wie wird morgen das Wetter?", bot=[question]).verdict == VERDICT_NONE


# -- signal 2: explicit call-back -----------------------------------------------------------


def test_positive_example_from_the_spec_names_subject_and_change() -> None:
    """"Zur Zusammenfassung des Mietvertrags: ergänze bitte die Kündigungsfrist."."""
    result = _signal("Zur Zusammenfassung des Mietvertrags: ergänze bitte die Kündigungsfrist.")

    assert result.kind == SIGNAL_CALLBACK
    assert result.verdict == VERDICT_POSITIVE
    assert result.evidence_ids == ("ev-contract",)


def test_a_new_subject_is_not_a_continuation() -> None:
    """Spec negative: a topic change after the order, even seconds later."""
    assert _signal("Wie wird morgen das Wetter?").verdict == VERDICT_NONE


def test_bare_continuations_carry_no_signal() -> None:
    """Spec negative: "ja", "kurz", "mach weiter" without a proven question."""
    for text in ("ja", "kurz", "mach weiter", "weiter", "ok"):
        result = _signal(text)
        assert result.verdict == VERDICT_NONE, text
        assert result.detail in {"bare_continuation", "no_change_marker"}


def test_a_shared_keyword_alone_is_not_a_call_back() -> None:
    """Spec: a common keyword is explicitly not enough."""
    assert _signal("Der Vertrag ist mir zu lang.").verdict == VERDICT_NONE


def test_subject_with_change_marker_but_no_thread_source_is_none() -> None:
    assert explicit_callback(current_text="ergänze bitte den Vertrag", thread_sources=[]).verdict == VERDICT_NONE


def test_the_answered_question_wins_over_the_callback() -> None:
    """Both signals can match; the concrete question is the stronger evidence."""
    question = BotMessageView(
        message_id="fx-q", text="kurz oder ausführlich?", occurred_ms=T0 + 500
    )

    result = _signal("kurz, und ergänze bitte den Mietvertrag", bot=[question])

    assert result.kind == SIGNAL_ANSWER
    assert result.evidence_ids == ("fx-q",)


def test_unknown_stays_unknown_and_never_positive() -> None:
    """Anything unclear must not attach a message to an old thread."""
    result = _signal("Hmm.")

    assert result.verdict == VERDICT_NONE
    assert result.positive is False
