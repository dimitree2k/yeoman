"""Statement screening: what may become durable knowledge, and what may not.

The corpus that exposed the gap: a model that is asked for durable statements sometimes
returns a report *about the conversation* instead - "es wird gefragt", "es wurde gesagt",
"wurde erwähnt".  The earlier rules only caught first-person and named-author forms, so
those became readable statements.  These tests pin the tightened rules and, just as
importantly, pin the statements that must survive them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge._capture_worker import StatementDraft
from yeoman_gateway.knowledge._memory.extraction_jobs import (
    is_hedged,
    screen_content,
    screen_statement_content,
)
from yeoman_gateway.knowledge._statements import rescreen_statements

from tests.gateway.capture_harness import AUTHOR, GROUP, CaptureHarness

IDLE_MS = 60_000


@pytest.fixture
def harness(tmp_path: Path) -> CaptureHarness:
    runtime = CaptureHarness(tmp_path, idle_ms=IDLE_MS)
    try:
        yield runtime
    finally:
        runtime.close()


#: Reports about the conversation itself: never durable knowledge about the world.
CONVERSATION_REPORTS = (
    "Es wird gefragt, wie alt die Person war.",
    "Es wurde gesagt, dass es nicht echt ist.",
    "Es werden 8 Bilder von Tom erwähnt.",
    "Ein Betrag von 160 Euro wurde genannt.",
    "Nachgefragt wird, wofür ein Vertrag ist.",
    "Es wird eine Bitte um etwas geäußert.",
    "Die Frage wurde beantwortet.",
    "Er erwähnte, dass er umzieht.",
    "It was said that the meeting moved.",
    "The price was mentioned in the thread.",
)

#: Statements that merely look similar and must keep being promoted.
REAL_STATEMENTS = (
    "Es gibt 35.000 Euro Fixkosten pro Monat.",
    "Die FDP liegt nur noch 3,9% hinter der CDU.",
    "Wombat-Kot ist würfelförmig.",
    "Der Verkaufspreis bei Amazon beträgt 99,99 Euro.",
    "Das Buch wurde 2020 geschrieben.",
    "Der Film wurde gestern gezeigt.",
    "Die Firma wird 2026 gegründet und der Chef hat gesagt, dass es klappt.",
    "Timo könnte heiraten.",
    "Ich wohne seit Juni in Köln.",
)


@pytest.mark.parametrize("text", CONVERSATION_REPORTS)
def test_a_report_about_the_conversation_is_refused(text: str) -> None:
    verdict = screen_statement_content(text)
    assert verdict.rejected
    assert verdict.reason == "conversation_report"


@pytest.mark.parametrize("text", REAL_STATEMENTS)
def test_a_real_statement_survives_the_tightened_screen(text: str) -> None:
    assert screen_statement_content(text).accepted, text


@pytest.mark.parametrize("text", CONVERSATION_REPORTS)
def test_the_shared_fact_screen_applies_the_same_rule(text: str) -> None:
    """One rule set for both paths - the fact screen is not a fork of the statement one."""
    assert screen_content(text).reason == "conversation_report"


def test_uncertainty_is_still_kept_not_refused() -> None:
    """The tightened screen must not become a second hedge filter."""
    assert is_hedged("Timo könnte heiraten.")
    assert screen_statement_content("Timo könnte heiraten.").accepted


def test_a_refused_report_never_removes_the_observation(tmp_path: Path) -> None:
    harness = CaptureHarness(tmp_path, idle_ms=60_000)
    try:
        harness.activate()
        event_id = harness.observe("Es wird gefragt, wie alt die Person war.")
        harness.advance(60_001)
        harness.drafts = [
            StatementDraft(
                content="Es wird gefragt, wie alt die Person war.",
                source_index=0,
                basis="explicit_statement",
            )
        ]

        report = harness.run_capture()

        assert report.published == 0
        assert report.refused.get("conversation_report") == 1
        assert harness.statements() == []
        # The observation and its proof stay exactly where they were.
        assert harness.store.get_event(event_id) is not None
        assert harness.source_row(event_id)["audience_status"] == "known"
    finally:
        harness.close()


def _publish_legacy_and_real(harness: CaptureHarness) -> tuple[str, str]:
    """Reproduce one legacy junk statement and one legitimate statement.

    The content screens run in the promotion worker, not in ``capture()``, so a row that
    was published before the rule existed is reproduced here exactly as history left it.
    """
    from yeoman_gateway.knowledge.models import SourceRef, StatementCandidate, TrustedCaptureContext

    harness.activate()
    harness.observe("Es wird gefragt, wie alt die Person war.", message_id="3EB0A00")
    harness.advance(1_000)
    harness.observe("Die FDP liegt nur noch 3,9% hinter der CDU.", message_id="3EB0A01")
    harness.advance(60_001)

    sources: list[SourceRef] = []
    for provider_id in ("3EB0A00", "3EB0A01"):
        event = harness.store.events_by_provider_identity(
            channel="whatsapp", chat_id=GROUP, provider_message_id=provider_id
        )[0]
        sources.append(
            SourceRef(
                event_id=event.event_id,
                revision=1,
                channel="whatsapp",
                chat_id=GROUP,
                author_principal=AUTHOR,
                occurred_at_ms=int(event.occurred_ms or harness.now),
            )
        )
    context = TrustedCaptureContext(
        request_id="legacy-rows",
        policy_revision=1,
        capture_basis="historic_row",
        authorized_sources=tuple(sources),
    )
    junk = harness.knowledge.capture(
        StatementCandidate(
            content="Es wird gefragt, wie alt die Person war.",
            sources=(sources[0],),
            extractor_version="statement-capture-v0",
        ),
        context=context,
    )
    kept = harness.knowledge.capture(
        StatementCandidate(
            content="Die FDP liegt nur noch 3,9% hinter der CDU.",
            sources=(sources[1],),
            extractor_version="statement-capture-v0",
        ),
        context=context,
    )
    return junk.statement_ids[0], kept.statement_ids[0]


def test_rescreen_dry_run_changes_nothing(harness: CaptureHarness) -> None:
    junk, kept = _publish_legacy_and_real(harness)

    report = rescreen_statements(harness.knowledge._store, apply=False)  # noqa: SLF001

    assert report.dry_run and junk in report.superseded and kept not in report.superseded
    assert report.reasons.get("conversation_report") == 1
    row = harness.knowledge._store.query_one(  # noqa: SLF001
        "SELECT status FROM knowledge_statements WHERE statement_id = ?", (junk,)
    )
    assert row["status"] == "assertion"


def test_rescreen_apply_hides_only_the_refused_statement(harness: CaptureHarness) -> None:
    junk, kept = _publish_legacy_and_real(harness)
    source = harness.knowledge._store.query_one(  # noqa: SLF001
        "SELECT event_id, revision FROM knowledge_statement_sources WHERE statement_id = ?",
        (junk,),
    )

    report = rescreen_statements(harness.knowledge._store, apply=True)  # noqa: SLF001

    assert junk in report.superseded and kept not in report.superseded
    statuses = {
        str(row["statement_id"]): str(row["status"])
        for row in harness.knowledge._store.query(  # noqa: SLF001
            "SELECT statement_id, status FROM knowledge_statements"
        )
    }
    assert statuses[junk] == "superseded"
    assert statuses[kept] == "assertion"
    # Text stays inspectable for an admin, the source revision keeps its proof.
    content = harness.knowledge._store.scalar(  # noqa: SLF001
        "SELECT content FROM memory2_nodes WHERE id = ?", (junk,)
    )
    assert content
    authority = harness.store.get_event_source_authority(
        str(source["event_id"]), int(source["revision"])
    )
    assert authority is not None and authority["revoked_at_ms"] is None
    audit = harness.knowledge._store.query_one(  # noqa: SLF001
        "SELECT reason FROM knowledge_statement_audit"
        " WHERE statement_id = ? AND operation = 'rescreen'",
        (junk,),
    )
    assert audit is not None and str(audit["reason"]) == "screen:conversation_report"
    # A superseded statement is no longer part of the ordinary read result.
    assert junk not in harness.knowledge_recall("Frage").statement_ids


# ── two separate screens (T11-T13, T38) ──────────────────────────────────────
#
# The screens answer two different questions and must not be collapsed into one lexical
# rule.  The input screen asks "may this section reach a model?"; the candidate screen
# asks "may this proposition become durable knowledge?".  A section that *reports* what
# somebody said is perfectly good input - the reporting vocabulary is only a reason to
# refuse a *candidate* that turned out to be nothing but that report.

from yeoman_gateway.knowledge._memory.extraction_jobs import (  # noqa: E402
    screen_capture_input,
    screen_statement_candidate,
)

#: Sections that contain reported speech but also a reportable proposition.
INPUT_SECTIONS_THAT_MUST_PASS = (
    "Tom erwähnte, dass Alex nach Köln gezogen ist",
    "Er sagte, dass die Firma 2026 gegründet wird",
    "Es wurde gesagt, dass das Buch 2020 geschrieben wurde",
    "Es gibt 35.000 Euro Fixkosten pro Monat",
)

#: Sections that carry nothing at all.
INPUT_SECTIONS_THAT_MUST_FAIL = (
    ("   ", "empty"),
    ("\x07", "control_characters"),
    ("..", "too_short"),
    ("<media>", "placeholder"),
    ("[image]", "placeholder"),
)


@pytest.mark.parametrize("text", INPUT_SECTIONS_THAT_MUST_PASS)
def test_the_input_screen_lets_reported_speech_through(text: str) -> None:
    """T38: "er erwähnte, dass ..." must not be destroyed before the model sees it."""
    assert screen_capture_input(text).accepted, text


@pytest.mark.parametrize("text,reason", INPUT_SECTIONS_THAT_MUST_FAIL)
def test_the_input_screen_refuses_only_unusable_input(text: str, reason: str) -> None:
    verdict = screen_capture_input(text)
    assert verdict.rejected
    assert verdict.reason == reason


def test_the_input_screen_never_applies_the_reporting_rule() -> None:
    """The two screens share validation helpers, not a blanket rejection rule."""
    report = "Es wurde gesagt, dass es nicht echt ist."
    # The same sentence is a valid input section and an invalid candidate.
    assert screen_capture_input(report).accepted
    assert screen_statement_candidate(
        StatementDraft(content=report, source_index=0)
    ).rejected


def test_a_revoked_source_never_reaches_a_provider() -> None:
    verdict = screen_capture_input(
        "Alex wohnt in Köln.", {"source_status": "revoked"}
    )
    assert verdict.rejected
    assert verdict.reason == "source_revoked"


def test_a_meta_only_candidate_still_fails_the_candidate_screen() -> None:
    for text in ("Es wurde etwas erwähnt.", "Die Frage wurde gestellt."):
        verdict = screen_statement_candidate(StatementDraft(content=text, source_index=0))
        assert verdict.rejected, text
        assert verdict.reason == "conversation_report"


def test_a_real_third_party_report_passes_the_candidate_screen() -> None:
    verdict = screen_statement_candidate(
        StatementDraft(
            content="Alex wohnt in Köln.",
            source_index=0,
            basis="reported_statement",
        )
    )
    assert verdict.accepted


def test_a_candidate_may_not_reference_a_person_it_was_not_offered() -> None:
    """T13: a foreign identifier or a prompt instruction changes nothing."""
    verdict = screen_statement_candidate(
        StatementDraft(
            content="Alex wohnt in Köln.",
            source_index=0,
            people=(("11111111-1111-4111-8111-111111111111", "subject"),),
        ),
        allowed_people=("22222222-2222-4222-8222-222222222222",),
    )
    assert verdict.rejected
    assert verdict.reason == "unoffered_person_reference"


def test_a_candidate_with_an_unsupported_basis_is_refused() -> None:
    verdict = screen_statement_candidate(
        StatementDraft(content="Alex wohnt in Köln.", source_index=0, basis="guess")
    )
    assert verdict.rejected
    assert verdict.reason == "unsupported_basis"


def test_facts_about_costs_and_dates_survive_both_screens() -> None:
    """T38: a financial or historical fact is a fact, not a conversation report."""
    for text in ("Es gibt 35.000 Euro Fixkosten.", "Das Buch wurde 2020 geschrieben."):
        assert screen_capture_input(text).accepted, text
        assert screen_statement_candidate(
            StatementDraft(content=text, source_index=0)
        ).accepted, text


def test_a_long_source_is_split_into_sections_and_each_section_is_usable() -> None:
    """A long text reaches the model in bounded sections instead of one crop."""
    from yeoman_gateway.knowledge._capture_worker import split_source_sections

    long_text = ". ".join(f"Sachverhalt Nummer {index}" for index in range(400)) + "."
    sections = split_source_sections(long_text, max_chars=400)
    assert len(sections) > 1
    # No character is lost and no section is empty or over the bound.
    assert "".join(sections) == long_text
    assert all(0 < len(section) <= 400 for section in sections)
    assert all(screen_capture_input(section).accepted for section in sections)


def test_splitting_never_truncates_a_single_oversized_sentence() -> None:
    from yeoman_gateway.knowledge._capture_worker import split_source_sections

    text = "x" * 1200
    sections = split_source_sections(text, max_chars=200)
    assert "".join(sections) == text
    assert all(section for section in sections)
