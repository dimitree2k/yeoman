"""Phase 2 / Task 4: lineage inventory and idempotent legacy import.

The inventory is metadata only: counts, schema and identities.  It reads no content into
its output, makes no provider or model call, never parses or OCRs a PDF, and every row
gets exactly one decision with a reason and a stable fingerprint so a second import is
provably a no-op.

Offline and synthetic: temporary fixtures only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from yeoman_gateway.knowledge._migration import (
    LINEAGE_DECISIONS,
    LINEAGE_SOURCE_CLASSES,
    LineageInventory,
    import_lineage,
    inspect_lineage_sources,
    lineage_fingerprint,
)
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


class _FakeTarget:
    """A canonical-log stand-in that dedupes on the event key, like the real store."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def append_event(self, **kwargs: Any) -> str:
        key = str(kwargs["event_key"])
        if key in self.rows:
            return str(self.rows[key]["event_id"])
        self.rows[key] = dict(kwargs)
        return str(kwargs["event_id"])


class _FakeQuarantine:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record_quarantine(self, **kwargs: Any) -> None:
        row = (
            str(kwargs["source_table"]),
            str(kwargs["source_pk"]),
            str(kwargs["reason"]),
        )
        if row not in self.rows:
            self.rows.append(row)


def _processing_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "processing.db"
    store = ProcessingStore(path)
    sink = SignalJournalSink(store, clock=lambda: T0)
    sink.capture(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "provider-1",
            "senderId": "4915@s.whatsapp.net",
            "text": "a legacy inbound message",
            "timestamp": T0,
        },
        event_id="event-in",
        event_key="wa:event-in",
        account="account-a",
        observed_at_ms=T0,
        strict=True,
    )
    sink.capture(
        "receipt",
        {"chatJid": CHAT, "messageId": "provider-1", "status": "delivered"},
        event_id="event-receipt",
        event_key="wa:event-receipt",
        account="account-a",
        observed_at_ms=T0 + 1,
        strict=True,
    )
    sink.capture(
        "reaction",
        {"chatJid": CHAT, "targetMessageId": "provider-1", "senderId": "4915", "emoji": "x"},
        event_id="event-reaction",
        event_key="wa:event-reaction",
        account="account-a",
        observed_at_ms=T0 + 2,
        strict=True,
    )
    # An actually transported outbound message is recorded as direction="out".
    store.append_event(
        event_key="wa:event-out",
        event_id="event-out",
        trace_id="trace-out",
        payload={"kind": "message", "text": "a bot answer"},
        now_ms=T0 + 3,
        direction="out",
    )
    store.close()
    return path


def _inbound_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "inbound"
    directory.mkdir()
    records = [
        {"chat_id": CHAT, "channel": "whatsapp"},
        {
            "message_id": "m-1",
            "timestamp": T0,
            "from": "4915@s.whatsapp.net",
            "type": "text",
            "content": "legacy archive line",
        },
        {"message_id": "", "timestamp": T0, "from": "4915@s.whatsapp.net", "content": "no id"},
        {"message_id": "m-2", "timestamp": None, "from": "4915@s.whatsapp.net", "content": "no ts"},
        {"message_id": "m-3", "timestamp": T0, "from": "", "content": "no proven sender"},
        {
            "message_id": "m-4",
            "timestamp": T0,
            "from": "4915@s.whatsapp.net",
            "role": "assistant",
            "content": "bot text",
        },
    ]
    path = directory / "whatsapp_chat.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")
    return directory


def _session_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "session-state"
    directory.mkdir()
    (directory / "chat.jsonl").write_text(
        json.dumps({"session": "whatsapp:chat", "ts": T0, "turn": "PRE"}),
        encoding="utf-8",
    )
    return directory


def _media_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    (root / "document.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    return root


@pytest.fixture
def fixtures(tmp_path: Path) -> Iterator[dict[str, Path]]:
    yield {
        "processing_db": _processing_fixture(tmp_path),
        "inbound_dir": _inbound_fixture(tmp_path),
        "session_state_dir": _session_fixture(tmp_path),
        "media_root": _media_fixture(tmp_path),
    }


# ── vocabulary ───────────────────────────────────────────────────────────────


def test_every_row_is_classified_into_exactly_one_decision(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    assert inventory.decisions
    assert set(LINEAGE_DECISIONS) == {"import", "link", "rebuild", "skip", "quarantine"}
    for item in inventory.decisions:
        assert item.decision in LINEAGE_DECISIONS
        assert item.reason
        assert len(item.fingerprint) == 32
        assert item.source_class in LINEAGE_SOURCE_CLASSES
    assert sum(inventory.counts().values()) == len(inventory.decisions)


def test_fingerprints_are_stable_across_runs(fixtures) -> None:
    first = inspect_lineage_sources(**fixtures)
    second = inspect_lineage_sources(**fixtures)
    assert [item.fingerprint for item in first.decisions] == [
        item.fingerprint for item in second.decisions
    ]
    assert first.counts() == second.counts()
    assert lineage_fingerprint("a", "b") == lineage_fingerprint("a", "b")
    assert lineage_fingerprint("a", "b") != lineage_fingerprint("b", "a")


# ── semantic safeguards ──────────────────────────────────────────────────────


def test_receipts_are_not_messages_and_not_read_proof(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    receipts = [item for item in inventory.decisions if item.source_ref == "event-receipt"]
    assert receipts
    assert {item.decision for item in receipts} == {"skip"}
    assert {item.reason for item in receipts} == {"receipt_is_not_a_message"}


def test_reactions_are_linked_not_imported(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    reactions = [item for item in inventory.decisions if item.source_ref == "event-reaction"]
    assert {item.decision for item in reactions} == {"link"}
    assert {item.reason for item in reactions} == {"reaction_is_metadata_only"}


def test_bot_answers_are_not_independent_human_facts(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    outbound = [item for item in inventory.decisions if item.source_ref == "event-out"]
    assert {item.decision for item in outbound} == {"link"}
    assert {item.reason for item in outbound} == {"bot_answer_is_not_human_evidence"}


def test_missing_ids_and_revisions_are_quarantined_not_invented(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    reasons = {item.reason for item in inventory.decisions if item.decision == "quarantine"}
    assert "missing_provider_id" in reasons
    assert "missing_source_revision" in reasons


def test_legacy_rights_without_evidence_fail_closed(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    quarantined = [item for item in inventory.decisions if item.decision == "quarantine"]
    assert "unproven_audience" in {item.reason for item in quarantined}


def test_inventory_reports_no_content_and_makes_no_model_call(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    blob = inventory.to_json()
    for secret in ("a legacy inbound message", "legacy archive line", "bot text", CHAT):
        assert secret not in blob
    assert inventory.eligible_model_jobs >= 0
    assert any("no provider or model call" in line for line in inventory.statements)


def test_media_references_are_skipped_and_never_opened(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    media = [item for item in inventory.decisions if item.source_class == "media_reference"]
    assert media
    assert {item.decision for item in media} == {"skip"}
    assert {item.reason for item in media} == {"media_reference_only"}


# ── apply mode ───────────────────────────────────────────────────────────────


def test_dry_run_reports_without_scheduling_anything(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    target = _FakeTarget()
    report = import_lineage(inventory, apply=False, target=target)

    assert report.dry_run is True
    assert target.rows == {}
    assert report.model_jobs_scheduled == 0
    assert report.eligible_model_jobs == inventory.eligible_model_jobs


def test_model_jobs_are_never_scheduled_without_the_explicit_flag(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    report = import_lineage(inventory, apply=True, target=_FakeTarget(), allow_model_jobs=False)
    assert report.eligible_model_jobs == inventory.eligible_model_jobs
    assert report.model_jobs_scheduled == 0


def test_the_explicit_flag_schedules_only_the_reported_eligible_count(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    report = import_lineage(
        inventory, apply=True, target=_FakeTarget(), allow_model_jobs=True
    )
    assert report.model_jobs_scheduled == report.eligible_model_jobs


def test_a_second_apply_creates_no_duplicate_event(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    target = _FakeTarget()

    first = import_lineage(inventory, apply=True, target=target)
    after_first = dict(target.rows)
    second = import_lineage(inventory, apply=True, target=target)

    assert first.imported > 0
    assert second.imported == first.imported
    assert target.rows == after_first
    assert len(target.rows) == first.imported


def test_a_second_apply_creates_no_duplicate_quarantine_row(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    sink = _FakeQuarantine()
    import_lineage(inventory, apply=True, quarantine_sink=sink)
    after_first = list(sink.rows)
    assert after_first
    import_lineage(inventory, apply=True, quarantine_sink=sink)
    assert sink.rows == after_first


def test_apply_imports_only_eligible_rows(fixtures) -> None:
    inventory = inspect_lineage_sources(**fixtures)
    target = _FakeTarget()
    import_lineage(inventory, apply=True, target=target)
    assert len(target.rows) == inventory.counts()["import"]
    # No derived object is fabricated by the importer itself.
    assert not any(
        key for key in target.rows if "membership" in key or "episode" in key
    )


def test_empty_inventory_is_a_no_op() -> None:
    inventory = LineageInventory()
    assert inventory.counts() == dict.fromkeys(LINEAGE_DECISIONS, 0)
    report = import_lineage(inventory, apply=True, target=_FakeTarget())
    assert report.imported == 0
