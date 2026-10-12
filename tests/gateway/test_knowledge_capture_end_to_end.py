"""One complete path, end to end, without a response turn anywhere.

passive observation -> durable storage -> native source and audience proof -> sourced
statement -> scoped Knowledge recall -> revocation blocks recall.

Everything runs on production components: the Bridge signal sink writes the canonical
journal event, the observation registrar proves the audience from projected History, the
native producer discovers and queues the source, the capture worker extracts through an
injected extractor and publishes through ``KnowledgeService.capture``, and recall uses a
scope derived from the same native snapshot. Only extraction is injected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge._capture_worker import StatementDraft

from tests.gateway.capture_harness import AUTHOR, GROUP, CaptureHarness

IDLE = 60_000
TEXT = "Wir treffen uns am Freitag um acht."
STATEMENT = "Treffen am Freitag um acht."


@pytest.fixture
def harness(tmp_path: Path) -> CaptureHarness:
    runtime = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        yield runtime
    finally:
        runtime.close()


def test_passive_observation_reaches_recall_and_revocation_ends_it(
    harness: CaptureHarness,
) -> None:
    harness.activate()
    provider_id = "3EB0400"

    # 1. Passive observation: no turn, no reply decision, no responder involvement.
    event_id = harness.observe(TEXT, message_id=provider_id)
    assert harness.store.get_event(event_id) is not None
    assert harness.knowledge.capture_status(now_ms=harness.now)["states"] == {}

    # 2. Durable proof: the audience is registered at the observation boundary.
    row = harness.source_row(event_id)
    assert row["audience_status"] == "known"
    assert row["occurred_at_ms"] == harness.now
    with harness._history_scope():
        issued = harness.knowledge.knowledge_sources.verify_source_ref(row["event_id"], 1)
    assert issued is not None and issued.author_principal == f"whatsapp:{AUTHOR}"

    # 3. Promotion: one idempotent job, one sourced statement.
    # Native capture starts its quiescence clock when the committed row is first seen.
    harness.promote()
    harness.advance(IDLE + 1)
    harness.drafts = [StatementDraft(content=STATEMENT, source_index=0)]
    report = harness.run_capture()
    assert report.published == 1
    statements = harness.statements()
    assert [item["content"] for item in statements] == [STATEMENT]
    assert statements[0]["status"] == "assertion"
    source_rows = harness.knowledge._store.query(  # noqa: SLF001 - provenance assertion
        "SELECT event_id, revision, status FROM knowledge_statement_sources"
    )
    assert [
        (str(item["event_id"]), int(item["revision"]), str(item["status"])) for item in source_rows
    ] == [(row["event_id"], 1, "active")]

    # 4. Recall with reader and audience proven by projected History.
    recalled = harness.recall("Freitag")
    assert STATEMENT in recalled

    # 5. Revocation wins: the provider delete arrives, recall goes quiet.
    harness.delete(provider_id)
    harness.advance(1_000)
    assert STATEMENT not in harness.recall("Freitag")
    assert harness.knowledge_recall("Freitag").statement_ids == ()


def test_observation_continues_without_a_capture_pass(tmp_path: Path) -> None:
    """The journal remains durable when no native capture pass is requested."""
    harness = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        first = harness.observe("Erster beobachteter Satz.", message_id="3EB0500")
        harness.advance(1_000)
        second = harness.observe("Zweiter beobachteter Satz.", message_id="3EB0501")
        harness.advance(IDLE + 1)

        assert harness.store.get_event(first) is not None
        assert harness.store.get_event(second) is not None
        assert harness.jobs() == []
        assert harness.statements() == []
    finally:
        harness.close()


def test_observation_survives_a_refused_and_an_overflowing_promotion(tmp_path: Path) -> None:
    blind = CaptureHarness(tmp_path, history_members=(), idle_ms=IDLE, max_waiting=1)
    try:
        blind.activate()
        refused = blind.observe("Niemand ist als Publikum bewiesen.", message_id="3EB0600")
        blind.advance(IDLE + 1)
        report = blind.promote()

        assert report.refusals.get("unknown_evidence") == 1
        assert blind.jobs() == []
        # The refused observation is still durable, complete with its identity.
        event = blind.store.get_event(refused)
        assert event is not None and event.payload["text"] == "Niemand ist als Publikum bewiesen."
    finally:
        blind.close()


def test_promotion_is_idempotent_across_a_replayed_window(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe(TEXT, message_id="3EB0700")
    harness.promote()
    harness.advance(IDLE + 1)
    harness.drafts = [StatementDraft(content=STATEMENT, source_index=0)]

    first = harness.run_capture()
    second = harness.run_capture()

    assert first.published == 1
    assert second.published == 0
    assert len(harness.statements()) == 1
    assert len(harness.jobs()) == 1
    assert (
        len(
            harness.store.events_by_provider_identity(
                channel="whatsapp", chat_id=GROUP, provider_message_id="3EB0700"
            )
        )
        == 1
    )
