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
from yeoman_gateway.knowledge._screening import (
    is_hedged,
    screen_content,
    screen_statement_content,
)
from yeoman_gateway.knowledge._statements import rescreen_statements

from tests.gateway.capture_harness import GROUP, CaptureHarness

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


def _publish_legacy_and_real(harness: CaptureHarness) -> tuple[str, str]:
    """Reproduce one legacy junk statement and one legitimate statement.

    The content screens run in the promotion worker, not in ``capture()``, so a row that
    was published before the rule existed is reproduced here exactly as history left it.
    """
    from yeoman_gateway.knowledge._history_identity import current_history_scope
    from yeoman_gateway.knowledge.models import SourceRef, StatementCandidate, TrustedCaptureContext

    harness.activate()
    harness.observe("Es wird gefragt, wie alt die Person war.", message_id="3EB0A00")
    harness.advance(1_000)
    harness.observe("Die FDP liegt nur noch 3,9% hinter der CDU.", message_id="3EB0A01")
    harness.advance(60_001)

    with harness._history_scope():
        scope = current_history_scope(harness.knowledge._store)  # noqa: SLF001
        assert scope is not None
        sources: list[SourceRef] = []
        for provider_id in ("3EB0A00", "3EB0A01"):
            native = scope.queries.native_message(chat_id=GROUP, native_id=provider_id)
            assert native is not None
            source = scope.sources.issue(str(native["message_id"]))
            assert source.event_id == str(native["message_id"])
            sources.append(source)
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
    authority = harness.knowledge.history_source_ledger.lookup(
        str(source["event_id"]), int(source["revision"])
    )
    assert authority is not None and not authority.revoked
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

from yeoman_gateway.knowledge._screening import (  # noqa: E402
    screen_capture_input,
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




def test_a_revoked_source_never_reaches_a_provider() -> None:
    verdict = screen_capture_input(
        "Alex wohnt in Köln.", {"source_status": "revoked"}
    )
    assert verdict.rejected
    assert verdict.reason == "source_revoked"












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
