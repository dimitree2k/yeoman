"""Forward statement promotion: sources, jobs, the worker and revocation.

The behaviour under test is the owner requirement: everything observed reaches durable
observational memory even when nobody answers, and promotion into long-term memory is a
separate, later decision that may be switched off, refused, full or fail without losing
the observation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.knowledge._capture import (
    ObservedEvent,
    collapse_provider_duplicates,
    promoter_reason,
)
from yeoman_gateway.knowledge._capture_worker import StatementDraft

from tests.gateway.capture_harness import AUTHOR, GROUP, CaptureHarness, Registry

IDLE = 60_000


@pytest.fixture
def harness(tmp_path: Path) -> CaptureHarness:
    runtime = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        yield runtime
    finally:
        runtime.close()


# ── sources ──────────────────────────────────────────────────────────────────


def test_observed_message_becomes_a_proven_source_without_a_turn(harness: CaptureHarness) -> None:
    harness.activate()
    event_id = harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)

    report = harness.promote()

    assert report.promoted_sources == 1
    assert harness.source_row(event_id)["audience_status"] == "known"
    # Promotion ran without a turn, a reply decision or a responder call.
    assert harness.knowledge.capture_status(now_ms=harness.now)["states"].get("queued") == 1


def test_assistant_text_and_empty_text_are_never_sources(harness: CaptureHarness) -> None:
    harness.activate()
    # Assistant output reaches the journal as an enriched event with an explicit role.
    harness.append_raw(
        text="Ich habe das schon beantwortet.", message_id="bot-1", extra={"role": "assistant"}
    )
    harness.observe("")
    harness.advance(IDLE + 1)

    report = harness.promote()

    assert report.promoted_sources == 0
    assert report.refusals.get("not_human_source") == 1
    assert report.refusals.get("empty_text") == 1
    assert harness.jobs() == []
    # The observations are durable all the same.
    assert harness.store.count_events() == 2


def test_unknown_audience_is_refused_not_downgraded(harness: CaptureHarness) -> None:
    blind = CaptureHarness(harness.tmp_path / "blind", registry=Registry({}), idle_ms=IDLE)
    try:
        blind.activate()
        blind.observe("Wir treffen uns am Freitag um acht.")
        blind.advance(IDLE + 1)
        report = blind.promote()
        assert report.promoted_sources == 0
        assert report.refusals.get("unknown_audience") == 1
        assert blind.jobs() == []
        assert blind.store.count_events() == 1
    finally:
        blind.close()


def test_provider_duplicates_collapse_to_one_source() -> None:
    def item(event_id: str, origin: str, created: int) -> ObservedEvent:
        return ObservedEvent(
            event_id=event_id,
            revision=1,
            channel="whatsapp",
            chat_id=GROUP,
            principal=AUTHOR,
            occurred_ms=created,
            created_ms=created,
            text="hallo",
            origin=origin,
            provider_message_id="3EB0DUP",
            audience_status="known",
            audience_members=(AUTHOR,),
        )

    collapsed = collapse_provider_duplicates(
        [
            item("gate-row", "whatsapp_bridge", 20),
            item("bridge-row", "whatsapp_canonical", 10),
        ]
    )
    assert [entry.event_id for entry in collapsed] == ["bridge-row"]
    assert promoter_reason(collapsed[0]) == ""


# ── jobs ─────────────────────────────────────────────────────────────────────


def test_batch_waits_for_quiescence_and_replay_creates_no_second_job(
    harness: CaptureHarness,
) -> None:
    harness.activate()
    harness.observe("Erster Satz.")
    harness.advance(1_000)
    harness.observe("Zweiter Satz.")

    assert harness.promote().jobs == 0
    assert harness.jobs() == []

    harness.advance(IDLE + 1)
    assert harness.promote().jobs == 1
    jobs = harness.jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "queued"

    # A replay of the same window is idempotent: no second job, no second source set.
    harness.promote()
    assert len(harness.jobs()) == 1


def test_the_forward_boundary_survives_a_restart_without_promoting_history(
    tmp_path: Path,
) -> None:
    first = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        first.activate()
        first.observe("Alter Satz.")
        first.advance(IDLE + 1)
        assert first.promote().jobs == 1
    finally:
        first.close()

    second = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        # Same database, fresh process state: the boundary is durable, so the historic
        # observation is not promoted a second time and nothing new is queued.
        assert second.producer.boundary()[0] > 0
        second.advance(IDLE + 1)
        assert second.promote().jobs == 0
        assert len(second.jobs()) == 1
    finally:
        second.close()


def test_activation_never_promotes_history(harness: CaptureHarness) -> None:
    """Forward capture only: a backlog is a separate, explicitly authorized decision."""
    harness.observe("Ein Satz aus der Zeit vor der Aktivierung.")
    harness.advance(IDLE + 1)

    harness.activate()
    harness.advance(IDLE + 1)
    report = harness.promote()

    assert report.jobs == 0
    assert harness.jobs() == []
    assert harness.statements() == []
    # The historic observation is still durable and still observable.
    assert harness.store.count_events() == 1


def test_a_new_source_revision_creates_a_new_job(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Erster Satz.", message_id="3EB0001")
    harness.advance(IDLE + 1)
    assert harness.promote().jobs == 1

    harness.observe("Ein anderer Satz.", message_id="3EB0002")
    harness.advance(IDLE + 1)
    assert harness.promote().jobs == 1
    assert len(harness.jobs()) == 2


def test_queue_overflow_is_visible_and_recoverable(harness: CaptureHarness) -> None:
    limited = CaptureHarness(harness.tmp_path / "full", idle_ms=IDLE, max_waiting=1)
    try:
        limited.activate()
        limited.observe("Erster Satz.", message_id="3EB0100")
        limited.advance(IDLE + 1)
        assert limited.promote().jobs == 1

        limited.observe("Zweiter Satz.", message_id="3EB0101")
        limited.advance(IDLE + 1)
        report = limited.promote()

        assert report.jobs == 0
        assert report.refusals.get("queue_full") == 1
        states = [job["state"] for job in limited.jobs()]
        assert states.count("skipped") == 1
        # Nothing was lost: both observations are still durable.
        assert limited.store.count_events() == 2
    finally:
        limited.close()


# ── worker ───────────────────────────────────────────────────────────────────


def test_worker_publishes_a_sourced_statement_without_a_response_turn(
    harness: CaptureHarness,
) -> None:
    harness.activate()
    event_id = harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.drafts = [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]

    report = harness.run_capture()

    assert report.published == 1
    statements = harness.statements()
    assert len(statements) == 1
    row = statements[0]
    assert row["status"] == "assertion"
    assert row["content"] == "Treffen am Freitag um acht."
    sources = harness.knowledge._store.query(  # noqa: SLF001 - provenance assertion
        "SELECT event_id, status FROM knowledge_statement_sources WHERE statement_id = ?",
        (row["statement_id"],),
    )
    assert [(str(item["event_id"]), str(item["status"])) for item in sources] == [
        (event_id, "active")
    ]
    assert harness.jobs()[0]["state"] == "done"
    assert harness.jobs()[0]["reason"] == "published=1"


def test_worker_is_a_no_op_when_no_worker_is_built(harness: CaptureHarness) -> None:
    """``capture_enabled = false``: no worker, no job, and observation continues."""
    from yeoman_gateway.app.bootstrap import build_statement_capture
    from yeoman_shared.config.schema import Config

    config = Config.model_validate({"knowledge": {"enabled": True, "captureEnabled": False}})
    assert (
        build_statement_capture(config, knowledge=harness.knowledge, processing=harness.store)
        is None
    )

    harness.activate()
    harness.observe("Beobachtet, nicht promoviert.")
    harness.advance(IDLE + 1)
    assert harness.jobs() == []
    assert harness.store.count_events() == 1


def test_worker_recovers_a_job_a_crash_left_running(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.promote()
    job_id = harness.jobs()[0]["job_id"]
    harness.knowledge._store.execute(  # noqa: SLF001 - simulated crash state
        "UPDATE knowledge_jobs SET state = 'running', updated_ms = ? WHERE job_id = ?",
        (harness.now, job_id),
    )
    harness.drafts = [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]

    harness.advance(700_000)
    report = harness.run_capture()

    assert report.published == 1
    assert harness.jobs()[0]["state"] == "done"


def test_one_failing_job_does_not_block_the_queue(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Erster Satz.", message_id="3EB0200")
    harness.advance(IDLE + 1)
    harness.promote()

    def broken(items: object) -> list[StatementDraft]:
        raise RuntimeError("provider down")

    worker = harness.build_worker(extractor=broken, max_attempts=1)
    harness.advance(IDLE + 1)
    worker.run_due(now_ms=harness.now)
    assert harness.jobs()[0]["state"] == "failed"

    harness.drafts = [StatementDraft(content="Zweiter Satz ist da.", source_index=0)]
    harness.build_worker(max_attempts=1)
    harness.observe("Zweiter Satz ist da.", message_id="3EB0201")
    harness.advance(IDLE + 1)
    report = harness.run_capture()

    assert report.published == 1
    states = sorted(job["state"] for job in harness.jobs())
    assert states == ["done", "failed"]


def test_no_provider_call_happens_inside_a_database_transaction(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.promote()
    store = harness.knowledge._store  # noqa: SLF001 - transaction observation
    seen: list[int] = []

    def extractor(items: object) -> list[StatementDraft]:
        seen.append(store._depth)  # noqa: SLF001 - white-box transaction assertion
        return [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]

    harness.build_worker(extractor=extractor)
    harness.run_capture()

    assert seen == [0]


def test_uncertainty_is_kept_instead_of_being_confirmed(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Timo könnte heiraten.")
    harness.advance(IDLE + 1)
    harness.drafts = [
        StatementDraft(
            content="Timo könnte heiraten.",
            source_index=0,
            basis="explicit_statement",
            certainty="uncertain",
        )
    ]

    report = harness.run_capture()

    assert report.published == 1
    row = harness.statements()[0]
    assert row["status"] == "assertion"
    assert row["kind"] == "uncertain"
    assert row["content"] == "Timo könnte heiraten."


def test_unsupported_bases_and_conversation_talk_are_refused(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Ich glaube, das wird nichts.")
    harness.observe("Wie besprochen machen wir weiter.")
    harness.advance(IDLE + 1)
    harness.drafts = [
        StatementDraft(content="Das wird nichts.", source_index=0, basis="speculation"),
        StatementDraft(
            content="Wie besprochen machen wir weiter.", source_index=1, basis="explicit_statement"
        ),
    ]

    report = harness.run_capture()

    assert report.published == 0
    assert "speculation" in report.refused
    assert "conversation_reference" in report.refused
    assert harness.statements() == []
    assert harness.jobs()[0]["reason"].startswith("refused=")


# ── revocation ───────────────────────────────────────────────────────────────


def test_delete_during_extraction_prevents_publication(harness: CaptureHarness) -> None:
    harness.activate()
    provider_id = "3EB0300"
    harness.observe("Wir treffen uns am Freitag um acht.", message_id=provider_id)
    harness.advance(IDLE + 1)
    harness.promote()

    def extractor(items: object) -> list[StatementDraft]:
        # The provider delete lands while the model is answering.
        harness.delete(provider_id)
        return [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]

    harness.build_worker(extractor=extractor)
    report = harness.run_capture()

    assert report.published == 0
    assert harness.statements() == []
    assert harness.jobs()[0]["state"] == "cancelled"
    assert harness.jobs()[0]["reason"] == "source_revoked"


def test_revoked_source_never_yields_a_statement(harness: CaptureHarness) -> None:
    harness.activate()
    provider_id = "3EB0301"
    harness.observe("Wir treffen uns am Freitag um acht.", message_id=provider_id)
    harness.advance(IDLE + 1)
    harness.promote()
    harness.delete(provider_id)
    harness.drafts = [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]

    report = harness.run_capture()

    assert report.published == 0
    assert harness.statements() == []
    assert harness.jobs()[0]["state"] == "cancelled"


def test_published_statement_is_invalidated_when_its_source_is_revoked(
    harness: CaptureHarness,
) -> None:
    harness.activate()
    provider_id = "3EB0302"
    harness.observe("Wir treffen uns am Freitag um acht.", message_id=provider_id)
    harness.advance(IDLE + 1)
    harness.drafts = [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]
    assert harness.run_capture().published == 1
    assert harness.statement_texts() == ["Treffen am Freitag um acht."]

    harness.delete(provider_id)
    harness.advance(1_000)

    assert harness.statements()[0]["status"] == "revoked"
    assert harness.knowledge_recall("Treffen").statement_ids == ()


def test_a_cancelled_job_is_not_overwritten_by_a_late_done(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.promote()
    job_id = harness.jobs()[0]["job_id"]
    harness.knowledge.mark_capture_job(job_id, "cancelled", reason="source_revoked")

    harness.drafts = [StatementDraft(content="Treffen am Freitag um acht.", source_index=0)]
    harness.run_capture()

    assert harness.jobs()[0]["state"] == "cancelled"
    assert harness.statements() == []


# ── the gate, the switch and the status surface ──────────────────────────────


def _config_from_file(directory: Path, payload: dict[str, object]) -> object:
    """Load a config the way the runtime does, so the file spelling is honoured."""
    import json

    from yeoman_shared.config.loader import load_config

    path = directory / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_config(path)


def test_capture_gate_builds_a_worker_only_when_the_switch_is_on(
    harness: CaptureHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from yeoman_gateway.app import bootstrap as bootstrap_module
    from yeoman_gateway.knowledge import _capture_worker
    from yeoman_shared.config.schema import Config

    built: list[dict[str, object]] = []

    class _FakeExtractor:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)

        def __call__(self, items: object) -> list[StatementDraft]:
            return []

    monkeypatch.setattr(_capture_worker, "StatementExtractor", _FakeExtractor)
    off = Config.model_validate({"knowledge": {"enabled": True, "captureEnabled": False}})
    on = _config_from_file(tmp_path, {"configVersion": 2, "knowledge": {"captureEnabled": True}})

    assert off.knowledge.capture_enabled is False
    assert on.knowledge.capture_enabled is True, "the config file spelling must reach the flag"
    assert (
        bootstrap_module.build_statement_capture(
            off, knowledge=harness.knowledge, processing=harness.store
        )
        is None
    )
    worker = bootstrap_module.build_statement_capture(
        on, knowledge=harness.knowledge, processing=harness.store
    )
    assert worker is not None
    assert built, "the enabled switch must reach the extractor construction"
    worker.start()
    worker.stop()


def test_capture_enabled_is_read_only_in_the_composition_root() -> None:
    """The flag may not silently go back to meaning nothing anywhere else."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    readers = sorted(
        str(path.relative_to(root))
        for path in (root / "packages").rglob("*.py")
        if "capture_enabled" in path.read_text(encoding="utf-8")
    )
    assert readers == [
        "packages/gateway/yeoman_gateway/app/bootstrap.py",
        "packages/shared/yeoman_shared/config/schema.py",
    ]


def test_status_reports_states_reasons_and_the_oldest_wait(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.promote()
    harness.build_worker()
    harness.advance(5_000)

    counters = harness.knowledge.capture_status(now_ms=harness.now)

    assert counters["states"].get("queued") == 1
    assert counters["oldest_queued_age_ms"] == 5_000
    assert counters["reasons"] == {}


def test_status_counts_refusals_without_any_statement_content(harness: CaptureHarness) -> None:
    harness.activate()
    harness.observe("Ich glaube, das wird nichts.")
    harness.advance(IDLE + 1)
    harness.drafts = [
        StatementDraft(content="Das wird nichts.", source_index=0, basis="speculation")
    ]
    harness.run_capture()

    counters = harness.knowledge.capture_status(now_ms=harness.now)

    assert counters["states"].get("done") == 1
    assert counters["reasons"] == {"refused=speculation": 1}
    assert "Das wird nichts." not in str(counters)


def test_cli_capture_status_is_read_only(tmp_path: Path, harness: CaptureHarness) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app

    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.advance(IDLE + 1)
    harness.promote()

    result = CliRunner().invoke(
        app, ["knowledge", "capture", "status", "--target", str(harness.tmp_path / "knowledge.db")]
    )

    assert result.exit_code == 0, result.output
    assert "queued" in result.output
    assert "Treffen" not in result.output


# ── historic repair and bounded backfill (owner-authorized operations) ───────


def test_historic_audience_repair_registers_author_only_and_is_idempotent(
    harness: CaptureHarness,
) -> None:
    from yeoman_gateway.knowledge._capture import HistoricAudienceRepair

    # An observation whose audience was never proven (as every historic row is).
    event_id = harness.append_raw(text="Alter Satz.", message_id="hist-1")
    assert harness.source_row(event_id)["audience_status"] == "unknown"

    repair = HistoricAudienceRepair(knowledge=harness.knowledge, processing=harness.store)
    dry = repair.run(limit=10, apply=False)
    assert dry.dry_run and dry.registered == 1
    assert harness.source_row(event_id)["audience_status"] == "unknown", "dry run writes nothing"

    applied = repair.run(limit=10, apply=True)
    assert applied.registered == 1
    row = harness.source_row(event_id)
    assert row["audience_status"] == "author_only"
    # The current group members were deliberately *not* granted: a later member must not
    # inherit a right that was never proven for this revision.
    assert not row["audience_members"]

    again = repair.run(limit=10, apply=True)
    assert again.registered == 0 and again.examined == 0


def test_historic_audience_repair_skips_revoked_sources(harness: CaptureHarness) -> None:
    from yeoman_gateway.knowledge._capture import HistoricAudienceRepair

    event_id = harness.append_raw(text="Ein widerrufener Satz.", message_id="3EB0800")
    harness.delete("raw-provider-3EB0800")

    repair = HistoricAudienceRepair(knowledge=harness.knowledge, processing=harness.store)
    report = repair.run(limit=10, apply=True)

    assert report.registered == 0
    assert report.refused.get("source_revoked") == 1
    assert harness.source_row(event_id)["audience_status"] == "unknown"


def test_historic_backfill_is_dry_run_first_and_never_moves_the_boundary(
    harness: CaptureHarness,
) -> None:
    # Two historic observations, then activation: forward capture starts after them.
    harness.observe("Erster alter Satz.", message_id="3EB0900")
    harness.advance(1_000)
    harness.observe("Zweiter alter Satz.", message_id="3EB0901")
    harness.advance(IDLE + 1)
    harness.activate()
    boundary = harness.producer.boundary()
    assert boundary[0] > 0

    dry = harness.producer.run_historical(before_ms=boundary[0], apply=False)
    assert dry.jobs == 1 and dry.promoted_sources == 2
    assert harness.jobs() == []
    assert harness.producer.boundary() == boundary

    applied = harness.producer.run_historical(before_ms=boundary[0], apply=True)
    assert applied.jobs == 1 and applied.promoted_sources == 2
    assert harness.producer.boundary() == boundary, "a backfill may not move the cursor"
    assert len(harness.jobs()) == 1

    replay = harness.producer.run_historical(before_ms=boundary[0], apply=True)
    assert replay.jobs == 0 and replay.already_queued == 1


def test_historic_backfill_publishes_author_only_statements(harness: CaptureHarness) -> None:
    harness.advance(1_000)
    # A historic row: journaled without an audience proof, as the backlog is.
    harness.append_raw(text="Der alte Satz.", message_id="3EB0910")
    harness.advance(IDLE + 1)
    harness.activate()
    from yeoman_gateway.knowledge._capture import HistoricAudienceRepair

    HistoricAudienceRepair(knowledge=harness.knowledge, processing=harness.store).run(
        limit=10, apply=True
    )
    harness.producer.run_historical(before_ms=harness.producer.boundary()[0], apply=True)
    harness.drafts = [StatementDraft(content="Der alte Satz.", source_index=0)]

    report = harness.run_capture()

    assert report.published == 1
    row = harness.statements()[0]
    assert row["visibility_scope"] == "author_only"
