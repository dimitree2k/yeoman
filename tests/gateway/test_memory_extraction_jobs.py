"""Plan 05 / Aufgabe 3: extraction is bounded, source-versioned and screened deterministically."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from yeoman_gateway.memory.extraction_jobs import (
    SharedFactCandidate,
    SharedFactExtractionQueue,
    check_candidate,
    confirmation_upgrades,
    extraction_job_key,
    initial_assertion_status,
    publish_visibility,
    resolve_relative_time,
    turn_settled_job,
)
from yeoman_gateway.memory.store import MemoryStore

T0 = 1_700_000_000_000
WORKSPACE = "ws1"
CHAT = "gruppe-a"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class _Event:
    def __init__(self, event_id: str, *, payload: dict | None = None) -> None:
        self.event_id = event_id
        self.payload = payload if payload is not None else {"text": "hallo"}
        self.payload_available = self.payload is not None
        self.payload_hash = f"hash-{event_id}"


class _Journal:
    def __init__(self, events: dict[str, _Event]) -> None:
        self._events = events

    def get_event(self, event_id: str) -> _Event | None:
        return self._events.get(event_id)


def _candidate(**overrides) -> SharedFactCandidate:
    base = dict(
        content="Der Stammtisch ist donnerstags.",
        author_principal="member-old",
        source_role="user",
        basis="explicit_statement",
        visibility_scope="chat_shared",
        source_refs=(("ev1", 1),),
    )
    base.update(overrides)
    return SharedFactCandidate(**base)  # type: ignore[arg-type]


def _queue(store: MemoryStore, *, extractor=None, journal=None, max_waiting: int = 32):
    return SharedFactExtractionQueue(
        store=store,
        extractor=extractor,
        journal=journal,
        idle_ms=60_000,
        max_delay_ms=300_000,
        max_waiting=max_waiting,
        clock=lambda: T0,
    )


def _enqueue(queue: SharedFactExtractionQueue, refs=(("ev1", 1),), now_ms: int = T0) -> str:
    return queue.enqueue(
        turn_ref="tu_1",
        source_refs=refs,
        now_ms=now_ms,
        workspace_id=WORKSPACE,
        chat_scope_key=CHAT,
    )


def test_idle_or_cap_triggers_extraction() -> None:
    args = {"first_activity_ms": 0, "last_activity_ms": 0, "idle_ms": 60_000, "max_delay_ms": 300_000}

    assert not turn_settled_job(now_ms=59_999, **args)
    assert turn_settled_job(now_ms=60_000, **args)
    assert turn_settled_job(
        now_ms=300_000, first_activity_ms=0, last_activity_ms=299_999, idle_ms=60_000,
        max_delay_ms=300_000,
    )


def test_job_key_is_stable_over_order_and_revision() -> None:
    assert extraction_job_key([("ev2", 1), ("ev1", 2)]) == extraction_job_key([("ev1", 2), ("ev2", 1)])
    assert extraction_job_key([("ev1", 1)]) == extraction_job_key([("ev1", 1)], "v1")
    assert extraction_job_key([("ev1", 1)]) != extraction_job_key([("ev1", 2)])
    assert extraction_job_key([("ev1", 1)]) != extraction_job_key([("ev1", 1)], "v2")


def test_tomorrow_is_resolved_from_source_time_or_stays_candidate() -> None:
    source = _ms("2026-09-10T10:00:00+00:00")

    assert resolve_relative_time(
        "morgen fliegen wir", source_ms=source, tz_offset_minutes=120
    ) == _ms("2026-09-11T00:00:00+02:00")
    assert resolve_relative_time("morgen fliegen wir", source_ms=source, tz_offset_minutes=None) is None
    assert resolve_relative_time(
        "heute Abend", source_ms=source, tz_offset_minutes=0
    ) == _ms("2026-09-10T00:00:00+00:00")


def test_unresolved_time_is_never_published() -> None:
    verdict = check_candidate(_candidate(temporal_basis="unresolved"))

    assert verdict.rejected
    assert verdict.reason == "unresolved_time"


def test_assistant_text_and_opinions_are_refused() -> None:
    assert check_candidate(_candidate(source_role="assistant")).reason == "not_user_source"
    assert check_candidate(_candidate(basis="opinion")).reason == "opinion"
    assert check_candidate(_candidate(basis="speculation")).reason == "speculation"
    assert check_candidate(_candidate(basis="inference")).reason == "inference"
    assert check_candidate(_candidate(basis="person_speculation")).reason == "person_speculation"
    assert check_candidate(_candidate(basis="delivery_claim")).reason == "delivery_claim"
    assert check_candidate(_candidate(basis="unknown_basis")).reason == "uncertain"


def test_missing_author_and_unknown_visibility_are_refused() -> None:
    assert check_candidate(_candidate(author_principal="  ")).reason == "missing_author"
    assert check_candidate(_candidate(visibility_scope=None)).reason == "unknown_visibility"
    assert check_candidate(_candidate(visibility_scope="public")).reason == "unknown_visibility"


def test_private_handoff_is_never_a_shared_source() -> None:
    assert check_candidate(_candidate(private_handoff=True)).reason == "private_handoff"


def test_mixed_private_sources_are_refused() -> None:
    verdict = check_candidate(
        _candidate(source_scopes=("private:a", "private:b"))
    )

    assert verdict.rejected
    assert verdict.reason == "mixed_private_sources"


def test_confidence_never_confirms_a_fact() -> None:
    assert initial_assertion_status(_candidate(confidence=0.99)) == "assertion"


def test_confirmation_requires_same_author_and_newer_revision() -> None:
    assert confirmation_upgrades(
        previous_author="member-old", previous_source_revision=1,
        candidate=_candidate(source_refs=(("ev1", 2),)),
    )
    assert not confirmation_upgrades(
        previous_author="member-old", previous_source_revision=2,
        candidate=_candidate(source_refs=(("ev1", 2),)),
    )
    assert not confirmation_upgrades(
        previous_author="member-new", previous_source_revision=1,
        candidate=_candidate(source_refs=(("ev1", 2),)),
    )
    assert not confirmation_upgrades(
        previous_author=None, previous_source_revision=None, candidate=_candidate()
    )


def test_visibility_fails_closed_without_proven_membership() -> None:
    assert publish_visibility(
        source_audiences=(frozenset({"a"}),), membership_proven=False, is_group=True
    ) == "author_only"
    assert publish_visibility(
        source_audiences=(frozenset(),), membership_proven=True, is_group=True
    ) == "author_only"
    assert publish_visibility(
        source_audiences=(frozenset({"a"}),), membership_proven=True, is_group=True
    ) == "chat_shared"
    assert publish_visibility(
        source_audiences=(frozenset({"a"}),), membership_proven=True, is_group=False
    ) == "principals"


def test_same_revision_never_creates_a_second_job(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    calls: list[str] = []

    def extractor(events):
        calls.append("called")
        return [_candidate()]

    queue = _queue(store, extractor=extractor, journal=_Journal({"ev1": _Event("ev1")}))
    first = _enqueue(queue)
    second = _enqueue(queue)

    assert first == second
    assert store.count_fact_jobs() == 1
    report = queue.run_due(now_ms=T0)
    assert report.published == 1
    assert len(calls) == 1

    # A new revision is a new job and may publish again.
    third = _enqueue(queue, refs=(("ev1", 2),))
    assert third != first
    assert store.count_fact_jobs() == 2
    store.close()


def test_payload_unavailable_is_skipped_not_guessed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    event = _Event("ev1")
    event.payload = None
    event.payload_available = False
    queue = _queue(store, extractor=lambda events: [_candidate()], journal=_Journal({"ev1": event}))

    _enqueue(queue)
    report = queue.run_due(now_ms=T0)

    assert report.reasons == {"payload_unavailable": 1}
    job = store.list_fact_jobs()[0]
    assert job["state"] == "skipped"
    assert job["reason"] == "payload_unavailable"
    assert store.list_facts() == []
    store.close()


def test_private_handoff_job_is_skipped(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    event = _Event("ev1", payload={"metadata": {"private_handoff_active": True}})
    queue = _queue(store, extractor=lambda events: [_candidate()], journal=_Journal({"ev1": event}))

    _enqueue(queue)
    report = queue.run_due(now_ms=T0)

    assert report.reasons == {"private_handoff": 1}
    assert store.list_facts() == []
    store.close()


def test_queue_overflow_is_visible_as_skipped(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue(store, extractor=lambda events: [], journal=_Journal({}), max_waiting=1)

    _enqueue(queue, refs=(("ev1", 1),))
    overflow_key = _enqueue(queue, refs=(("ev2", 1),))

    job = store.get_fact_job(overflow_key)
    assert job is not None
    assert job["state"] == "skipped"
    assert job["reason"] == "queue_full"
    assert queue.overflows == 1
    store.close()


def test_extractor_failure_is_failed_without_inventing_a_fact(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")

    def broken(events):
        raise TimeoutError("model timeout")

    queue = _queue(store, extractor=broken, journal=_Journal({"ev1": _Event("ev1")}))
    _enqueue(queue)
    report = queue.run_due(now_ms=T0)

    assert report.failed == 1
    job = store.list_fact_jobs()[0]
    assert job["state"] == "failed"
    assert job["reason"] == "extractor_error:TimeoutError"
    assert store.list_facts() == []
    store.close()


def test_refused_candidate_is_recorded_with_its_reason(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue(
        store,
        extractor=lambda events: [_candidate(basis="opinion")],
        journal=_Journal({"ev1": _Event("ev1")}),
    )
    _enqueue(queue)
    report = queue.run_due(now_ms=T0)

    assert report.reasons == {"opinion": 1}
    assert store.list_facts() == []
    assert store.list_fact_jobs()[0]["reason"] == "opinion"
    store.close()


def test_cancelled_source_stops_a_queued_job(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue(store, extractor=lambda events: [_candidate()], journal=_Journal({"ev1": _Event("ev1")}))
    _enqueue(queue)

    cancelled = queue.cancel_sources(["ev1"], now_ms=T0 + 1)

    assert cancelled == 1
    report = queue.run_due(now_ms=T0 + 2)
    assert report.processed == 0
    assert store.list_facts() == []
    store.close()


def test_published_fact_carries_sources_and_ttl(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    queue = _queue(
        store,
        extractor=lambda events: [_candidate(source_refs=(("ev1", 1),))],
        journal=_Journal({"ev1": _Event("ev1")}),
    )
    queue._fact_ttl_ms = 90 * 24 * 3600 * 1000
    _enqueue(queue)
    queue.run_due(now_ms=T0 + 1)

    facts = store.list_facts()
    assert len(facts) == 1
    assert facts[0].sources[0].source_event_id == "ev1"
    assert facts[0].sources[0].source_revision == 1
    assert facts[0].valid_until_ms == T0 + 1 + 90 * 24 * 3600 * 1000
    assert facts[0].assertion_status == "assertion"
    jobs = store.list_fact_jobs()
    assert jobs[0]["state"] == "done"
    assert json.loads(jobs[0]["source_refs_json"]) == [["ev1", 1]]
    store.close()
