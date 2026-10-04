"""Complete provider snapshots project only issuer-backed account-scoped pairs."""

from __future__ import annotations

import hashlib
from typing import Any

from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore

ACCOUNT = "synthetic-membership-account"
CHAT = "synthetic-membership-room@g.us"
SNAPSHOT_AT = 1_780_000_000_000


class _Issuer:
    def __init__(self, authority: Any, store: ProcessingStore, event_id: str) -> None:
        self.authority = authority
        self.store = store
        self.event_id = event_id
        self.observations = []

    def observe(self, observation) -> str:
        # The observation may only be issued after its canonical snapshot is durable.
        assert self.store.get_event(self.event_id) is not None
        self.observations.append(observation)
        return self.authority.issue_observation(observation)


class _OrderedKnowledge:
    def __init__(self, service: Any, store: ProcessingStore, event_id: str) -> None:
        self.service = service
        self.store = store
        self.event_id = event_id
        self.observations = []
        self.results = []

    def record_provider_pair(self, observation):
        # Pair projection runs synchronously after journal append and before capture returns.
        assert self.store.get_event(self.event_id) is not None
        self.observations.append(observation)
        result = self.service.record_provider_pair(observation)
        self.results.append(result)
        return result

    def __getattr__(self, name: str):
        return getattr(self.service, name)


def _snapshot(*participants: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    return {
        "chatJid": CHAT,
        "snapshotAtMs": SNAPSHOT_AT,
        "complete": complete,
        "memberCount": len(participants) if complete else len(participants) + 1,
        "participants": list(participants),
    }


def _capture(h, payload: dict[str, Any], *, event_id: str = "snapshot-event"):
    store = ProcessingStore(h.tmp_path / f"{event_id}.processing.db")
    issuer = _Issuer(h.authority, store, event_id)
    ordered_knowledge = _OrderedKnowledge(h.service, store, event_id)
    sink = SignalJournalSink(
        store,
        identity_observation_issuer=issuer,
        statements=ordered_knowledge,
    )
    stored_id = sink.capture(
        "membership_snapshot",
        payload,
        event_id=event_id,
        event_key=f"whatsapp:{ACCOUNT}:{CHAT}:membership_snapshot:{SNAPSHOT_AT}",
        account=ACCOUNT,
        observed_at_ms=SNAPSHOT_AT,
        strict=True,
    )
    return store, issuer, ordered_knowledge, stored_id


def _pair(phone: str, lid: str) -> tuple[Identifier, Identifier]:
    return (
        Identifier("whatsapp", "phone_jid", phone, namespace=ACCOUNT),
        Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),
    )


def _seed_identifier(h, identifier: Identifier, evidence_ref: str):
    observation = TrustedIdentityObservation(
        identifiers=(identifier,),
        evidence_ref=evidence_ref,
        observed_at_ms=h.clock.now_ms(),
        account_namespace=ACCOUNT,
    )
    h.authority.issue_observation(observation)
    return h.service.resolve_person(observation)


def _is_owner_flagged(h, person_id: str) -> bool:
    row = h.service._store.query_one(
        "SELECT is_owner FROM contacts WHERE id = ?", (person_id,)
    )
    assert row is not None
    return bool(int(row["is_owner"]))


def test_snapshot_records_each_complete_namespaced_provider_pair(knowledge_harness):
    h = knowledge_harness
    store, issuer, ordered, event_id = _capture(
        h,
        _snapshot(
            {"phoneJid": "4910000000101@s.whatsapp.net", "lid": "8420000101@lid", "admin": False},
            {"phoneJid": "4910000000102@s.whatsapp.net", "lid": "8420000102@lid", "admin": True},
        ),
    )
    try:
        assert event_id == "snapshot-event"
        assert store.get_event("snapshot-event") is not None
        assert len(issuer.observations) == 2
        assert ordered.observations == issuer.observations
        for observation in issuer.observations:
            assert len(observation.identifiers) == 2
            assert {item.channel for item in observation.identifiers} == {"whatsapp"}
            assert {item.namespace for item in observation.identifiers} == {ACCOUNT}
            assert {item.kind for item in observation.identifiers} == {"phone_jid", "lid"}
            assert observation.mapping_verified is True
            assert observation.observed_at_ms == SNAPSHOT_AT
            assert observation.account_namespace == ACCOUNT
            phone = next(item.value for item in observation.identifiers if item.kind == "phone_jid")
            lid = next(item.value for item in observation.identifiers if item.kind == "lid")
            pair_identity = (
                f"whatsapp\0{ACCOUNT}\0phone_jid\0{phone}\0"
                f"whatsapp\0{ACCOUNT}\0lid\0{lid}"
            )
            assert observation.evidence_ref == (
                f"whatsapp-membership:snapshot-event:"
                f"{hashlib.sha256(pair_identity.encode('utf-8')).hexdigest()}"
            )
            result = ordered.results[issuer.observations.index(observation)]
            assert result.person_id is not None
            resolved = h.service.resolve_identifier(observation.identifiers[0])
            other = h.service.resolve_identifier(observation.identifiers[1])
            assert resolved.person_id is not None
            assert resolved.person_id == other.person_id
    finally:
        store.close()


def test_incomplete_snapshot_members_are_not_stitched(knowledge_harness):
    h = knowledge_harness
    incomplete_store, incomplete_issuer, _, _ = _capture(
        h,
        _snapshot(
            {"phoneJid": "4910000000111@s.whatsapp.net", "lid": "8420000111@lid", "admin": False},
            complete=False,
        ),
        event_id="incomplete-snapshot",
    )
    complete_store, complete_issuer, _, _ = _capture(
        h,
        _snapshot({"lid": "8420000112@lid", "admin": False}),
        event_id="one-sided-snapshot",
    )
    try:
        assert incomplete_store.get_event("incomplete-snapshot") is not None
        assert complete_store.get_event("one-sided-snapshot") is not None
        assert incomplete_issuer.observations == []
        assert complete_issuer.observations == []
        assert h.service.resolve_identifier(
            Identifier("whatsapp", "lid", "8420000111@lid", namespace=ACCOUNT)
        ).person_id is None
        assert h.service.resolve_identifier(
            Identifier("whatsapp", "lid", "8420000112@lid", namespace=ACCOUNT)
        ).person_id is None
    finally:
        incomplete_store.close()
        complete_store.close()


def test_pair_conflict_does_not_merge_people_or_grant_owner(knowledge_harness):
    h = knowledge_harness
    phone, lid = _pair("4910000000121@s.whatsapp.net", "8420000121@lid")
    phone_person = _seed_identifier(h, phone, "seed-phone")
    lid_person = _seed_identifier(h, lid, "seed-lid")
    assert phone_person.person_id is not None
    assert lid_person.person_id is not None
    assert phone_person.person_id != lid_person.person_id
    assert not _is_owner_flagged(h, phone_person.person_id)
    assert not _is_owner_flagged(h, lid_person.person_id)

    store, issuer, ordered, _ = _capture(
        h,
        _snapshot({"phoneJid": phone.value, "lid": lid.value, "admin": True}),
        event_id="conflicting-snapshot",
    )
    try:
        assert len(issuer.observations) == 1
        assert ordered.results[0].status == "conflict"
        assert h.service.resolve_identifier(phone).person_id == phone_person.person_id
        assert h.service.resolve_identifier(lid).person_id == lid_person.person_id
        assert h.service.owners_of_identifier_value(phone.value, channel="whatsapp") == (
            phone_person.person_id,
        )
        assert h.service.owners_of_identifier_value(lid.value, channel="whatsapp") == (
            lid_person.person_id,
        )
        assert not _is_owner_flagged(h, phone_person.person_id)
        assert not _is_owner_flagged(h, lid_person.person_id)
    finally:
        store.close()


def test_replayed_snapshot_is_idempotent(knowledge_harness):
    h = knowledge_harness
    payload = _snapshot({"phoneJid": "4910000000131@s.whatsapp.net", "lid": "8420000131@lid", "admin": False})
    store = ProcessingStore(h.tmp_path / "replayed-snapshot.processing.db")
    issuer = _Issuer(h.authority, store, "snapshot-event")
    ordered = _OrderedKnowledge(h.service, store, "snapshot-event")
    sink = SignalJournalSink(store, identity_observation_issuer=issuer, statements=ordered)
    capture_args = {
        "event_id": "snapshot-event",
        "event_key": f"whatsapp:{ACCOUNT}:{CHAT}:membership_snapshot:{SNAPSHOT_AT}",
        "account": ACCOUNT,
        "observed_at_ms": SNAPSHOT_AT,
        "strict": True,
    }
    try:
        first = sink.capture("membership_snapshot", payload, **capture_args)
        refs = [observation.evidence_ref for observation in issuer.observations]
        bindings_before = [
            h.service.resolve_identifier(identifier).person_id
            for identifier in _pair("4910000000131@s.whatsapp.net", "8420000131@lid")
        ]
        second = sink.capture("membership_snapshot", payload, **capture_args)
        bindings_after = [
            h.service.resolve_identifier(identifier).person_id
            for identifier in _pair("4910000000131@s.whatsapp.net", "8420000131@lid")
        ]

        assert first == second == "snapshot-event"
        assert store.count_events() == 1
        assert len(issuer.observations) == 2
        assert issuer.observations[0] == issuer.observations[1]
        assert issuer.observations[1].evidence_ref == refs[0]
        assert bindings_after == bindings_before
        assert bindings_after[0] == bindings_after[1]
    finally:
        store.close()
