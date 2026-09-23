"""Plan 01 / R01, R06, R10: durable event journal, retention and lineage view."""

from __future__ import annotations

import pytest
from yeoman_gateway.processing.models import (
    CANONICAL_WHATSAPP_ORIGIN,
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


def test_event_metadata_conflict_is_rejected_even_when_payload_matches(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    payload = {
        "kind": "message",
        "channel": "whatsapp",
        "account": "account-a",
        "direction": "in",
        "revision": 1,
        "text": "same bytes",
    }
    assert db.append_event(
        event_key="wa:event-metadata",
        event_id="metadata-1",
        trace_id="trace-1",
        payload=payload,
    ) == "metadata-1"

    with pytest.raises(ValueError):
        db.append_event(
            event_key="wa:event-metadata",
            event_id="metadata-2",
            trace_id="trace-1",
            payload=payload,
            account="account-b",
        )
    assert db.count_events() == 1
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
        account="account-a",
        direction="in",
        revision=2,
        audience_ref="audience:captured",
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
    assert stored.account == "account-a"
    assert stored.direction == "in"
    assert stored.revision == 2
    assert stored.audience_ref == "audience:captured"
    assert stored.source_message_id == "3A1"
    assert stored.occurred_ms == 1_700_000_000_000
    assert stored.created_ms == 1_700_000_000_500
    assert stored.payload == {"kind": "message", "text": "hallo"}
    assert stored.payload_hash == canonical_hash({"kind": "message", "text": "hallo"})
    db.close()


def _canonical(event_id: str, provider_id: str, chat: str = "g@g.us") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_key=f"whatsapp:{chat}:{event_id}",
        trace_id=provider_id,
        kind="message",
        origin="whatsapp_canonical",
        channel="whatsapp",
        chat_id=chat,
        source_message_id=provider_id,
        payload={"kind": "message", "text": "x"},
    )


def test_event_assignment_resolves_a_provider_id_of_a_canonical_event(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    event = _canonical("wa_1", "AC01")
    db.append_event(
        event_key=event.event_key, event_id=event.event_id, trace_id=event.trace_id,
        payload=event, now_ms=1,
    )
    db.attach_event_assignment(event_id="wa_1", thread_id="th_1", turn_id="tu_1", now_ms=2)

    assert db.event_assignment("wa_1") == ("th_1", "tu_1")
    assert db.event_assignment("AC01") == ("th_1", "tu_1")
    assert db.event_assignment("unknown") is None
    db.close()


def test_event_assignment_prefers_the_newest_assigned_event_for_a_provider_id(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    for event_id, now in (("wa_old", 1), ("wa_new", 5), ("wa_unassigned", 9)):
        event = _canonical(event_id, "AC02")
        db.append_event(
            event_key=event.event_key, event_id=event.event_id, trace_id=event.trace_id,
            payload=event, now_ms=now,
        )
    db.attach_event_assignment(event_id="wa_old", thread_id="th_a", turn_id="tu_a", now_ms=6)
    db.attach_event_assignment(event_id="wa_new", thread_id="th_b", turn_id="tu_b", now_ms=7)

    assert db.event_assignment("AC02") == ("th_b", "tu_b")
    db.close()


def test_event_assignment_keeps_plain_event_ids_working(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(event_key="wa:m1", event_id="m1", trace_id="m1", payload={"kind": "message"})
    db.attach_event_assignment(event_id="m1", thread_id="th_1", turn_id="tu_1", now_ms=2)
    assert db.event_assignment("m1") == ("th_1", "tu_1")
    db.close()


def test_long_payload_roundtrip_keeps_canonical_json_bytes(tmp_path):
    from yeoman_gateway.processing.models import canonical_json

    text = "  " + ("ä" * 8_001) + " \n"
    payload = {"kind": "message", "channel": "whatsapp", "text": text}
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:long",
        event_id="long-1",
        trace_id="trace-long",
        payload=payload,
    )

    stored = db.get_event("long-1")
    assert stored is not None and stored.payload == payload
    row = db._conn.execute(
        "SELECT payload_json FROM events WHERE event_id = ?", ("long-1",)
    ).fetchone()
    assert row is not None and row["payload_json"] == canonical_json(payload)
    db.close()


def test_confirmed_whatsapp_receipt_appends_one_correlated_outbound_event(tmp_path):
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    db.enqueue_effect(
        effect_id="fx-out-1",
        operation_key="turn:send:1",
        payload={"kind": "text", "text": "confirmed text"},
        target={"channel": "whatsapp", "chat_id": "chat@g.us"},
        now_ms=now,
    )

    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE direction = 'out'"
    ).fetchone()["n"] == 0

    db.record_transport_receipt(
        "fx-out-1",
        channel="whatsapp",
        chat_id="chat@g.us",
        attempt_id="attempt-1",
        provider_message_id="provider-out-1",
        client_message_id="client-out-1",
        now_ms=now,
    )
    db.record_transport_receipt(
        "fx-out-1",
        channel="whatsapp",
        chat_id="chat@g.us",
        attempt_id="attempt-1",
        provider_message_id="provider-out-1",
        client_message_id="client-out-1",
        now_ms=now + 1,
    )

    rows = db._conn.execute(
        "SELECT event_id FROM events WHERE direction = 'out' ORDER BY event_id"
    ).fetchall()
    assert len(rows) == 1
    event = db.get_event(str(rows[0]["event_id"]))
    assert event is not None
    assert event.origin == CANONICAL_WHATSAPP_ORIGIN
    assert event.direction == "out"
    assert event.payload is not None
    assert event.payload["effect_id"] == "fx-out-1"
    assert event.payload["attempt_id"] == "attempt-1"
    assert event.payload["provider_message_id"] == "provider-out-1"
    assert event.payload["client_message_id"] == "client-out-1"
    assert event.payload["text"] == "confirmed text"
    assert len(db.transport_receipts("fx-out-1")) == 1
    db.close()


def test_store_normalizes_revision_in_payload_and_column(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:revision",
        event_id="revision-1",
        trace_id="trace-revision",
        payload={"kind": "edit", "channel": "whatsapp", "revision": "4"},
    )
    stored = db.get_event("revision-1")
    assert stored is not None and stored.revision == 4
    assert stored.payload is not None and stored.payload["revision"] == 4

    with pytest.raises(ValueError):
        db.append_event(
            event_key="wa:revision-invalid",
            event_id="revision-invalid",
            trace_id="trace-revision",
            payload={"kind": "edit", "revision": 0},
        )
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


def test_voice_send_quota_is_atomic_and_rolling(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    day = 24 * 60 * 60 * 1000

    assert db.claim_voice_send("person@lid", now_ms=1_000, cooldown_ms=day) is True
    assert db.claim_voice_send("person@lid", now_ms=1_000 + day - 1, cooldown_ms=day) is False
    assert db.claim_voice_send("person@lid", now_ms=1_000 + day, cooldown_ms=day) is True
    assert db.claim_voice_send("someone-else@lid", now_ms=1_001, cooldown_ms=day) is True
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


def test_retention_exempts_the_canonical_whatsapp_channel_not_one_origin_label(tmp_path):
    """Two writers journal the same canonical channel, so the exemption keys on it.

    The journal has been written by the Bridge signal sink and by the policy gate under
    different origin labels.  Keying the exemption on one label deleted the other
    writer's rows at the lineage window, which spec section 1.9 forbids: a retention sweep
    removes neither row nor payload of a canonical WhatsApp event.  Telegram stays
    retention-managed, so the exemption is a channel boundary, not a global one.
    """
    now = 1_700_000_000_000
    db = ProcessingStore(tmp_path / "processing.db")
    db.append_event(
        event_key="wa:canonical",
        event_id="wa-canonical",
        trace_id="trace-canonical",
        payload={
            "kind": "message",
            "origin": CANONICAL_WHATSAPP_ORIGIN,
            "channel": "whatsapp",
            "text": "canonical",
        },
        now_ms=now,
    )
    db.append_event(
        event_key="wa:operational",
        event_id="wa-operational",
        trace_id="trace-operational",
        payload={
            "kind": "message",
            "origin": "whatsapp_bridge",
            "channel": "whatsapp",
            "text": "operational",
        },
        now_ms=now,
    )
    db.append_event(
        event_key="tg:operational",
        event_id="tg-operational",
        trace_id="trace-telegram",
        payload={
            "kind": "message",
            "origin": "telegram",
            "channel": "telegram",
            "text": "operational",
        },
        now_ms=now,
    )

    db.purge(now_ms=now + 31 * DAY_MS)

    canonical = db.get_event("wa-canonical")
    assert canonical is not None and canonical.payload is not None
    operational = db.get_event("wa-operational")
    assert operational is not None and operational.payload is not None
    assert db.get_event("tg-operational") is None
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


def test_processing_store_opens_even_when_processing_is_disabled(tmp_path, monkeypatch):
    """Canonical capture is durable even while responders/effects stay disabled."""
    from yeoman_gateway.app.bootstrap import build_processing_store
    from yeoman_shared.config.schema import Config

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    store = build_processing_store(Config())
    assert store is not None
    assert (tmp_path / "data" / "processing" / "processing.db").exists()
    store.close()

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


def test_recent_reactions_lists_a_chats_reactions_newest_first(tmp_path):
    from yeoman_gateway.processing.models import ReactionPayload, canonical_hash, canonical_json

    db = ProcessingStore(tmp_path / "processing.db")

    def react(effect_id, chat, emoji, now, state="sent", origin="legacy"):
        admission_id = None
        if origin == "participation":
            admission_id = f"admission:{effect_id}"
            payload_hash = canonical_hash(ReactionPayload(message_id=effect_id, emoji=emoji).to_dict())
            with db._write() as conn:
                conn.execute(
                    "INSERT INTO participation_admissions VALUES (?, ?, ?)",
                    (admission_id, canonical_json({"channel": "whatsapp", "chat_id": chat,
                                                   "payload_hash": payload_hash}), now),
                )
        db.enqueue_effect(
            effect_id=effect_id,
            operation_key=f"reaction:whatsapp:{chat}:{effect_id}:{emoji}",
            payload=ReactionPayload(message_id=effect_id, emoji=emoji),
            now_ms=now,
            capability="send_reaction",
            target={"channel": "whatsapp", "chat_id": chat},
            state=state,
            origin=origin,
            admission_id=admission_id,
        )

    react("e1", "a@g.us", "👀", 1_000)
    react("e2", "a@g.us", "😂", 2_000, origin="participation")
    react("e3", "b@g.us", "🔥", 3_000)
    react("e4", "a@g.us", "👍", 4_000, state="failed")
    react("e5", "a@g.us", "🤙", 500)

    recent = db.recent_reactions(channel="whatsapp", chat_id="a@g.us", since_ms=900, limit=10)
    assert [(item.emoji, item.created_ms) for item in recent] == [("😂", 2_000), ("👀", 1_000)]
    assert len(db.recent_reactions(channel="whatsapp", chat_id="a@g.us", since_ms=0, limit=1)) == 1
    db.close()


def test_short_reply_rate_claim_is_atomic_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from yeoman_gateway.processing.models import ReactionPayload

    path = tmp_path / "processing.db"
    seed = ProcessingStore(path)
    seed.enqueue_effect(
        effect_id="e1", operation_key="reaction:whatsapp:a@g.us:m0:👍",
        payload=ReactionPayload(message_id="m0", emoji="👍"), now_ms=9_000,
        trace_id="m0", capability="send_reaction",
        target={"channel": "whatsapp", "chat_id": "a@g.us"}, state="sent",
    )
    seed.close()

    def claim(message_id):
        store = ProcessingStore(path)
        try:
            return store.claim_short_reply(
                channel="whatsapp", chat_id="a@g.us", message_id=message_id,
                now_ms=10_000, count=2, window_seconds=120, cooldown_seconds=600,
            ).status
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(claim, ("m1", "m2")))
    assert sorted(statuses) == ["claimed", "cooldown"]


def test_message_claim_survives_reopen_and_more_than_1024_other_messages(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    claim = db.claim_short_reply(
        channel="whatsapp", chat_id="a@g.us", message_id="same",
        now_ms=10_000, count=20, window_seconds=120, cooldown_seconds=600,
    )
    assert claim.status == "claimed"
    db.complete_short_reply(
        channel="whatsapp", chat_id="a@g.us", message_id="same",
        outcome="silence", emoji=None, now_ms=10_001,
    )
    for index in range(1025):
        db.claim_short_reply(
            channel="whatsapp", chat_id="a@g.us", message_id=f"other-{index}",
            now_ms=20_000 + index, count=20, window_seconds=120, cooldown_seconds=600,
        )
    db.close()
    reopened = ProcessingStore(tmp_path / "processing.db")
    duplicate = reopened.claim_short_reply(
        channel="whatsapp", chat_id="a@g.us", message_id="same",
        now_ms=30_000, count=20, window_seconds=120, cooldown_seconds=600,
    )
    assert duplicate.status == "duplicate"
    reopened.close()


_CLAIM = dict(channel="whatsapp", chat_id="a@g.us", count=2, window_seconds=120,
              cooldown_seconds=600)


def test_short_reply_completed_live_reactions_count_before_their_effects_exist(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    for message_id, now in (("m1", 10_000), ("m2", 10_002)):
        assert db.claim_short_reply(message_id=message_id, now_ms=now, **_CLAIM).status == "claimed"
        db.complete_short_reply(channel="whatsapp", chat_id="a@g.us", message_id=message_id,
                                outcome="react", emoji="😄", now_ms=now + 1)
    assert db.claim_short_reply(message_id="m3", now_ms=10_004, **_CLAIM).status == "cooldown"
    db.close()


def test_short_reply_shadow_and_non_reaction_claims_free_their_slot(tmp_path):
    db = ProcessingStore(tmp_path / "processing.db")
    for index, (mode, outcome) in enumerate(
        (("shadow", "react"), ("live", "silence"), ("live", "answer"))
    ):
        message_id = f"s{index}"
        claim = db.claim_short_reply(message_id=message_id, now_ms=10_000 + index, mode=mode, **_CLAIM)
        assert claim.status == "claimed"
        db.complete_short_reply(channel="whatsapp", chat_id="a@g.us", message_id=message_id,
                                outcome=outcome, emoji="😂" if outcome == "react" else None,
                                now_ms=10_000 + index)
    assert db.claim_short_reply(message_id="next", now_ms=10_010, **_CLAIM).status == "claimed"
    db.close()


def test_short_reply_effect_and_claim_of_one_message_count_once(tmp_path):
    from yeoman_gateway.processing.models import ReactionPayload

    db = ProcessingStore(tmp_path / "processing.db")
    assert db.claim_short_reply(message_id="m1", now_ms=10_000, **_CLAIM).status == "claimed"
    db.complete_short_reply(channel="whatsapp", chat_id="a@g.us", message_id="m1",
                            outcome="react", emoji="👍", now_ms=10_001)
    db.enqueue_effect(
        effect_id="e1", operation_key="reaction:whatsapp:a@g.us:m1:👍",
        payload=ReactionPayload(message_id="m1", emoji="👍"), now_ms=10_001,
        trace_id="m1", capability="send_reaction",
        target={"channel": "whatsapp", "chat_id": "a@g.us"}, state="sent",
    )
    assert db.claim_short_reply(message_id="m2", now_ms=10_002, **_CLAIM).status == "claimed"
    db.close()


def test_short_reply_rate_claim_counts_unknown_nonrepeatable_effects(tmp_path):
    from yeoman_gateway.processing.models import ReactionPayload

    db = ProcessingStore(tmp_path / "processing.db")
    for effect_id, state, now in (
        ("sent", "sent", 10_000),
        ("unknown", "unknown_nonrepeatable", 10_001),
    ):
        db.enqueue_effect(
            effect_id=effect_id,
            operation_key=f"reaction:whatsapp:a@g.us:{effect_id}:👍",
            payload=ReactionPayload(message_id=effect_id, emoji="👍"),
            now_ms=now,
            capability="send_reaction",
            target={"channel": "whatsapp", "chat_id": "a@g.us"},
            state=state,
        )

    assert db.claim_short_reply(message_id="next", now_ms=10_002, **_CLAIM).status == "cooldown"
    db.close()
