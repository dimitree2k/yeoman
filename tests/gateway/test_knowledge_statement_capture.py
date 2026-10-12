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

from tests.gateway.capture_harness import AUTHOR, GROUP, CaptureHarness

IDLE = 60_000


@pytest.fixture
def harness(tmp_path: Path) -> CaptureHarness:
    runtime = CaptureHarness(tmp_path, idle_ms=IDLE)
    try:
        yield runtime
    finally:
        runtime.close()


# ── sources ──────────────────────────────────────────────────────────────────


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


# ── worker ───────────────────────────────────────────────────────────────────


def test_worker_is_a_no_op_when_no_worker_is_built(harness: CaptureHarness) -> None:
    """``capture_enabled = false``: the runtime builder returns no worker."""
    from yeoman_gateway.app.bootstrap import build_statement_capture
    from yeoman_shared.config.schema import Config

    config = Config.model_validate({"knowledge": {"enabled": True, "captureEnabled": False}})
    assert (
        build_statement_capture(config, knowledge=harness.knowledge, processing=harness.store)
        is None
    )


# ── revocation ───────────────────────────────────────────────────────────────


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

    from tests.gateway.history_test_helpers import ProjectedHistory

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
    projector = ProjectedHistory(tmp_path / "history")
    worker = bootstrap_module.build_statement_capture(
        on,
        knowledge=harness.knowledge,
        processing=harness.store,
        projector=projector,
    )
    assert worker is not None
    from yeoman_gateway.knowledge._history_capture import HistoryCaptureWorker

    assert isinstance(worker, HistoryCaptureWorker)
    assert built, "the enabled switch must reach the extractor construction"
    projector.close()


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
    harness.promote()
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
    harness.promote()
    harness.advance(IDLE + 1)
    harness.drafts = [
        StatementDraft(content="Das wird nichts.", source_index=0, basis="speculation")
    ]
    harness.run_capture()

    counters = harness.knowledge.capture_status(now_ms=harness.now)

    assert counters["states"].get("done") == 1
    assert counters["reasons"] == {"no_candidates": 1}
    assert "Das wird nichts." not in str(counters)


def test_cli_capture_status_is_read_only(tmp_path: Path, harness: CaptureHarness) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app

    harness.activate()
    harness.observe("Wir treffen uns am Freitag um acht.")
    harness.promote()
    harness.advance(IDLE + 1)
    harness.promote()

    result = CliRunner().invoke(
        app, ["knowledge", "capture", "status", "--target", str(harness.knowledge.db_path)]
    )

    assert result.exit_code == 0, result.output
    assert "queued" in result.output
    assert "Treffen" not in result.output


# ── historic repair and bounded backfill (owner-authorized operations) ───────
