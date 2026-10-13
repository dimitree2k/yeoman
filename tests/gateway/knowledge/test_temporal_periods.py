"""Validity periods: ended values get a proven end, newer residences end older ones."""

from __future__ import annotations

from yeoman_gateway.knowledge.models import (
    AttributeCandidate,
    AttributeValue,
    PersonLinkCandidate,
    RecallQuery,
    SourceRef,
    StatementCandidate,
)

_HOUR_MS = 60 * 60 * 1000


def _residence(h, person: str, city: str, source: SourceRef) -> str:
    candidate = StatementCandidate(
        content=f"wohnt in {city}.",
        sources=(source,),
        people=(
            PersonLinkCandidate(
                person_id=person, role="subject", source=source, attribution="extracted"
            ),
        ),
        attributes=(
            AttributeCandidate(
                person_id=person, attribute_key="residence", value=AttributeValue(city)
            ),
        ),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    return h.service.capture(candidate, context=h.capture_context(source)).statement_ids[0]


def _row(h, statement_id: str):
    return h.service._store.query_one(  # noqa: SLF001 - asserting the stored contract
        "SELECT status, superseded_by, supersession_reason, valid_from_ms, valid_until_ms"
        " FROM knowledge_statements WHERE statement_id = ?",
        (statement_id,),
    )


def _historic(h, person: str, at_ms: int) -> tuple[str, ...]:
    return h.service.recall(
        RecallQuery(person_ids=(person,), limit=10),
        context=h.read_context(person, now_ms=at_ms),
        view="historic",
    ).statement_ids


def test_a_newer_residence_ends_the_older_one_at_its_source_time(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom = h.person("Tom")
    start = h.clock.now_ms()
    cologne = _residence(h, tom, "Köln", h.source(tom, when_ms=start))
    bonn_start = start + 10 * _HOUR_MS
    bonn = _residence(h, tom, "Bonn", h.source(tom, when_ms=bonn_start))

    old = _row(h, cologne)
    assert old["status"] == "superseded"
    assert old["supersession_reason"] == "state_change"
    assert old["superseded_by"] is None
    assert old["valid_until_ms"] == bonn_start
    assert _row(h, bonn)["status"] == "assertion"

    current = h.recall_person(tom, h.read_context(tom, now_ms=bonn_start + _HOUR_MS))
    assert cologne not in current.statement_ids
    assert bonn in current.statement_ids
    # The Köln period is [start, bonn_start): visible inside, gone after.
    assert _historic(h, tom, start + _HOUR_MS) == (cologne,)
    assert _historic(h, tom, bonn_start + _HOUR_MS) == ()


def test_an_out_of_order_capture_closes_itself(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom = h.person("Tom")
    start = h.clock.now_ms()
    bonn_start = start + 10 * _HOUR_MS
    bonn = _residence(h, tom, "Bonn", h.source(tom, when_ms=bonn_start))
    cologne = _residence(h, tom, "Köln", h.source(tom, when_ms=start))

    assert _row(h, bonn)["status"] == "assertion"
    late = _row(h, cologne)
    assert late["status"] == "superseded"
    assert late["supersession_reason"] == "state_change"
    assert late["valid_until_ms"] == bonn_start


def test_repeating_the_same_residence_is_not_a_change(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom = h.person("Tom")
    start = h.clock.now_ms()
    first = _residence(h, tom, "Köln", h.source(tom, when_ms=start))
    second = _residence(h, tom, "Köln", h.source(tom, when_ms=start + _HOUR_MS))
    assert _row(h, first)["status"] == "assertion"
    assert _row(h, second)["status"] == "assertion"


def test_another_chat_never_ends_a_value(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom = h.person("Tom")
    start = h.clock.now_ms()
    cologne = _residence(h, tom, "Köln", h.source(tom, chat="group-a", when_ms=start))
    _residence(h, tom, "Bonn", h.source(tom, chat="group-b", when_ms=start + _HOUR_MS))
    assert _row(h, cologne)["status"] == "assertion"
    assert _row(h, cologne)["valid_until_ms"] is None


def test_ending_an_attribute_records_the_end_of_its_period(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom = h.person("Tom")
    start = h.clock.now_ms()
    cologne = _residence(h, tom, "Köln", h.source(tom, when_ms=start))
    h.service.end_attribute(
        statement_id=cologne,
        person_id=tom,
        attribute_key="residence",
        context=h.admin_context(),
    )
    row = _row(h, cologne)
    assert row["supersession_reason"] == "state_change"
    assert row["valid_until_ms"] is not None and row["valid_until_ms"] > start
    # Before the fix the open period made the old value "historically true" forever.
    assert _historic(h, tom, row["valid_until_ms"] + _HOUR_MS) == ()


def test_a_correction_closes_the_corrected_period(projected_knowledge_harness):
    h = projected_knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom)
    original = h.capture_text("Alex reist nach Berlin.", source, subjects=(alex,)).statement_ids[0]
    h.service.correct_statement(
        original,
        h.candidate("Alex reist nach Hamburg.", source, subjects=(alex,)),
        expected_source=source,
        context=h.capture_context(source),
    )
    assert _row(h, original)["valid_until_ms"] is not None
