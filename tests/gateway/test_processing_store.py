"""Plan 01 / R01, R06, R10: durable event journal, retention and lineage view."""

from __future__ import annotations

import pytest
from yeoman_gateway.processing.models import (
    DAY_MS,
    CanonicalEvent,
    DecisionRecord,
    RetentionSettings,
    canonical_hash,
)
from yeoman_gateway.processing.store import ProcessingStore


def test_event_identity_survives_reopen(tmp_path):
    path = tmp_path / "processing.db"
    db = ProcessingStore(path)
    assert db.append_event(event_key="wa:event-1", event_id="e1",
                           trace_id="tr1", payload={"kind": "message"}) == "e1"
    db.close()
    db = ProcessingStore(path)
    assert db.append_event(event_key="wa:event-1", event_id="e2",
                           trace_id="tr2", payload={"kind": "message"}) == "e1"
    db.close()


def test_event_payload_conflict_is_rejected(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:event-1",
        event_id="e1",
        trace_id="tr1",
        payload={"kind": "message", "text": "first"},
    )
    with pytest.raises(ValueError):
        db.append_event(
            event_key="wa:event-1",
            event_id="e2",
            trace_id="tr1",
            payload={"kind": "message", "text": "second"},
        )
    assert len(db.get_lineage("tr1").events) == 1
    assert db.get_event("e2") is None
    db.close()


def test_failed_append_does_not_leave_partial_event(tmp_path, monkeypatch):
    db = ProcessingStore(tmp_path / "processing.db")

    def _boom(*args, **kwargs):
        raise RuntimeError("relation write failed")

    monkeypatch.setattr(db, "_record_relations", _boom)
    with pytest.raises(RuntimeError):
        db.append_event(
            event_key="wa:event-1",
            event_id="e1",
            trace_id="tr1",
            payload={"kind": "message", "text": "hello"},
        )
    monkeypatch.undo()

    assert db.get_event("e1") is None
    assert db.append_event(
        event_key="wa:event-1", event_id="e1", trace_id="tr1", payload={"kind": "message"}
    ) == "e1"
    db.close()


def test_canonical_event_roundtrip_keeps_metadata_and_hash(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    event = CanonicalEvent(
        event_id="e1",
        event_key="wa:event-1",
        trace_id="tr1",
        kind="message",
        origin="whatsapp",
        principal="4915111@s.whatsapp.net",
        channel="whatsapp",
        chat_id="group@g.us",
        occurred_ms=1_700_000_000_000,
        source_message_id="3A1",
        payload={"kind": "message", "text": "hallo"},
    )
    assert db.append_event(
        event_key=event.event_key,
        event_id=event.event_id,
        trace_id=event.trace_id,
        payload=event,
        now_ms=1_700_000_000_500,
    ) == "e1"

    stored = db.get_event("e1")
    assert stored is not None
    assert stored.kind == "message"
    assert stored.origin == "whatsapp"
    assert stored.principal == "4915111@s.whatsapp.net"
    assert stored.chat_id == "group@g.us"
    assert stored.source_message_id == "3A1"
    assert stored.occurred_ms == 1_700_000_000_000
    assert stored.created_ms == 1_700_000_000_500
    assert stored.payload == {"kind": "message", "text": "hallo"}
    assert stored.payload_hash == canonical_hash({"kind": "message", "text": "hallo"})
    db.close()


def test_missing_reference_stays_unresolved_and_is_never_invented(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:event-1",
        event_id="e1",
        trace_id="tr1",
        payload={"kind": "message", "source_message_id": "m1"},
    )
    db.append_event(
        event_key="wa:event-2",
        event_id="e2",
        trace_id="tr1",
        payload={"kind": "message", "source_message_id": "m2", "reply_to_message_id": "m1"},
    )
    db.append_event(
        event_key="wa:event-3",
        event_id="e3",
        trace_id="tr1",
        payload={"kind": "message", "source_message_id": "m3", "reply_to_message_id": "m404"},
    )

    # m1 is known (e1), m404 was never seen: the relation stays open, nothing invented.
    unresolved = db.unresolved_relations()
    assert [(rel.event_id, rel.ref_id, rel.resolved) for rel in unresolved] == [
        ("e3", "m404", 0)
    ]

    # A late event with the referenced identity resolves the relation.
    db.append_event(
        event_key="wa:event-4",
        event_id="e4",
        trace_id="tr1",
        payload={"kind": "message", "source_message_id": "m404"},
    )
    assert db.unresolved_relations() == ()
    events = {event.event_id: event for event in db.get_lineage("tr1").events}
    assert {(rel.relation, rel.ref_id, rel.resolved) for rel in events["e3"].relations} == {
        ("source", "m3", 1),
        ("reply_to", "m404", 1),
    }
    db.close()


def test_schema_version_and_quick_check(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    assert db.schema_version >= 1
    assert db.quick_check() == "ok"
    db.close()


def test_purge_strips_payloads_but_keeps_tombstones(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:event-1",
        event_id="e1",
        trace_id="tr1",
        payload={"kind": "message", "text": "secret"},
        now_ms=now,
    )
    db.enqueue_effect(
        effect_id="fx1", operation_key="turn1:send1", payload={"text": "A"}, now_ms=now
    )
    assert db.claim_effect("fx1", "worker-1", now, 30_000) is True
    assert db.transition(
        "fx1", expected="executing", target="sent", now_ms=now, worker_id="worker-1"
    ) is True

    report = db.purge(now_ms=now + 8 * DAY_MS)

    stored = db.get_event("e1")
    assert stored is not None
    assert stored.payload is None
    assert stored.payload_purged_ms == now + 8 * DAY_MS
    assert stored.payload_hash  # tombstone keeps the hash, not the text
    assert report.event_payloads_purged == 1

    effect = db.get_effect("fx1")
    assert effect is not None
    assert effect.payload is None
    assert effect.payload_hash
    assert db.effect_state("fx1") == "sent"

    # A purged journal must not silently accept the same identity as new input.
    assert db.append_event(
        event_key="wa:event-1", event_id="e9", trace_id="tr1", payload={"kind": "message"}
    ) == "e1"
    db.close()


def test_purge_drops_old_metadata_but_protects_unresolved(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:resolved",
        event_id="e-resolved",
        trace_id="tr1",
        payload={"kind": "message", "text": "old"},
        now_ms=now,
    )
    db.append_event(
        event_key="wa:unresolved",
        event_id="e-unresolved",
        trace_id="tr1",
        payload={"kind": "message", "text": "old", "reply_to_message_id": "never-seen"},
        now_ms=now,
    )
    db.record_decision(_decision(now))

    report = db.purge(now_ms=now + 31 * DAY_MS)

    assert db.get_event("e-resolved") is None
    assert db.get_event("e-unresolved") is not None
    assert db.get_decision("d1") is None
    assert report.events_deleted == 1

    # After the unresolved grace period the relation is dropped too.
    db.purge(now_ms=now + 91 * DAY_MS)
    assert db.get_event("e-unresolved") is None
    assert db.unresolved_relations() == ()
    db.close()


def test_lineage_view_exposes_metadata_not_raw_text(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:event-1",
        event_id="e1",
        trace_id="tr1",
        payload={"kind": "message", "text": "geheim"},
        now_ms=now,
    )
    db.enqueue_effect(
        effect_id="fx1",
        operation_key="turn1:send1",
        payload={"text": "antwort"},
        trace_id="tr1",
        turn_id="turn1",
        turn_revision=1,
        principal="owner",
        capability="send_text",
        target={"channel": "whatsapp", "chat_id": "chat1"},
        expires_at_ms=now + 120_000,
        now_ms=now,
    )

    view = db.get_lineage("tr1")
    assert [event.event_id for event in view.events] == ["e1"]
    assert view.events[0].payload_available is True
    assert "geheim" not in repr(view)
    assert [effect.effect_id for effect in view.effects] == ["fx1"]
    assert view.effects[0].target_hash
    assert view.effects[0].payload_available is True

    unknown = db.get_lineage("does-not-exist")
    assert unknown.events == ()
    assert unknown.effects == ()
    db.close()


def test_retention_settings_reject_negative_values():
    with pytest.raises(ValueError):
        RetentionSettings(journal_payload_ms=-1)
    with pytest.raises(ValueError):
        RetentionSettings(metadata_ms=DAY_MS, journal_payload_ms=2 * DAY_MS)
    with pytest.raises(ValueError):
        RetentionSettings(unresolved_ms=DAY_MS, metadata_ms=2 * DAY_MS)


def test_disabled_processing_creates_no_database(tmp_path, monkeypatch):
    """The default must stay inert: disabled mode adds no second database."""
    from yeoman_gateway.app.bootstrap import build_processing_store
    from yeoman_shared.config.schema import Config

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    assert build_processing_store(Config()) is None
    assert not (tmp_path / "data" / "processing").exists()

    enabled = Config.model_validate({"processing": {"enabled": True}})
    store = build_processing_store(enabled)
    assert store is not None
    assert (tmp_path / "data" / "processing" / "processing.db").exists()
    assert store.path.endswith("data/processing/processing.db")
    store.close()


def test_bootstrap_retention_follows_config(tmp_path, monkeypatch):
    from yeoman_gateway.app.bootstrap import build_processing_store
    from yeoman_shared.config.schema import Config

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    config = Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "retention": {
                    "journal_payload_days": 2,
                    "lineage_metadata_days": 10,
                    "unresolved_days": 40,
                },
            }
        }
    )
    store = build_processing_store(config)
    assert store is not None
    assert store.retention.journal_payload_ms == 2 * DAY_MS
    assert store.retention.metadata_ms == 10 * DAY_MS
    assert store.retention.unresolved_ms == 40 * DAY_MS
    store.close()


def _decision(now: int) -> DecisionRecord:
    return DecisionRecord(
        decision_id="d1",
        trace_id="tr1",
        policy_version="v1",
        policy_hash="hash-v1",
        principal="owner",
        target="whatsapp:chat1",
        capability="send_text",
        turn_revision=1,
        outcome="allow",
        reason="allow",
        created_ms=now,
    )


def test_decisions_are_immutable_and_keep_policy_version(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    record = _decision(now)

    assert db.record_decision(record) == "d1"
    assert db.record_decision(record) == "d1"
    stored = db.get_decision("d1")
    assert stored is not None
    assert stored.policy_version == "v1"
    assert stored.policy_hash == "hash-v1"
    assert stored.outcome == "allow"

    with pytest.raises(ValueError):
        db.record_decision(
            DecisionRecord(
                decision_id="d1",
                trace_id="tr1",
                policy_version="v2",
                policy_hash="hash-v2",
                principal="owner",
                target="whatsapp:chat1",
                capability="send_text",
                turn_revision=1,
                outcome="deny",
                reason="denied",
                created_ms=now,
            )
        )
    db.close()


# --------------------------------------------------------------------------------------
# Plan 04: reconciliation probe records
# --------------------------------------------------------------------------------------


def test_probe_records_are_idempotent_and_claimable(tmp_path):
    from yeoman_gateway.processing.models import ProcessingError

    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(
        effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=now
    )

    first = db.schedule_probe("fx1", attempt_number=1, due_ms=now + 5_000, now_ms=now)
    again = db.schedule_probe("fx1", attempt_number=1, due_ms=now + 9_999, now_ms=now)

    assert first == again  # idempotent per (effect, attempt)
    assert db.count_probes("fx1") == 1
    assert db.next_probe_number("fx1") == 2
    assert db.due_probes(now) == ()
    due = db.due_probes(now + 5_000)
    assert [probe.probe_id for probe in due] == [first]
    assert db.open_probe("fx1").probe_id == first
    with pytest.raises(ProcessingError):
        db.schedule_probe("nope", attempt_number=1, due_ms=now, now_ms=now)
    db.close()


def test_probe_claim_is_exclusive_but_takeable_after_expiry(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=now)
    probe_id = db.schedule_probe("fx1", attempt_number=1, due_ms=now, now_ms=now)

    assert db.claim_probe(probe_id, "worker-a", now, 30_000) is True
    assert db.claim_probe(probe_id, "worker-b", now, 30_000) is False
    # A lost claim is taken over once its lease expires.
    assert db.claim_probe(probe_id, "worker-b", now + 30_001, 30_000) is True
    # Only the current leaseholder may finish.
    assert db.finish_probe(probe_id, outcome="confirmed", now_ms=now + 30_002, worker_id="worker-a") is False
    assert db.finish_probe(probe_id, outcome="confirmed", now_ms=now + 30_002, worker_id="worker-b") is True
    assert db.count_probes("fx1", outcome="confirmed") == 1
    assert db.open_probe("fx1") is None
    assert db.due_probes(now + 60_000) == ()  # finished probes are never due again
    db.close()


def test_retention_keeps_open_probes_and_drops_finished_ones(tmp_path):
    from yeoman_gateway.processing.models import DAY_MS

    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=now)
    done = db.schedule_probe("fx1", attempt_number=1, due_ms=now, now_ms=now)
    open_probe = db.schedule_probe("fx1", attempt_number=2, due_ms=now + DAY_MS, now_ms=now)
    db.claim_probe(done, "worker-a", now, 30_000)
    db.finish_probe(done, outcome="inconclusive", now_ms=now, worker_id="worker-a")
    db.record_transport_receipt(
        "fx1", channel="whatsapp", chat_id="chat@g.us", provider_message_id="3EB0", now_ms=now
    )

    report = db.purge(now_ms=now + 31 * DAY_MS)

    assert db.get_probe(done) is None
    assert db.get_probe(open_probe) is not None  # an open probe survives retention
    assert db.transport_receipts("fx1") == ()
    assert report.probes_deleted == 1 and report.receipts_deleted == 1
    db.close()
