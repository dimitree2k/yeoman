"""Owner review queue for duplicate-person evidence."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from yeoman_gateway.knowledge._identity import IdentityEngine
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.models import (
    IDENTITY_CANDIDATE_STATUSES,
    IdentityCandidate,
    IdentityCandidateWeights,
    KnowledgeError,
    TrustedAdminContext,
    ValidationError,
    normalize_alias_value,
)


class IdentityCandidateEngine:
    """Build text-free evidence snapshots and persist owner decisions."""

    def __init__(self, store: KnowledgeStore, *, identity: IdentityEngine) -> None:
        self._store = store
        self._identity = identity

    def propose(
        self,
        *,
        context: TrustedAdminContext,
        weights: IdentityCandidateWeights | None = None,
        persist: bool = True,
    ) -> tuple[IdentityCandidate, ...]:
        self._identity._require_owner(context)
        if weights is not None and not isinstance(weights, IdentityCandidateWeights):
            raise ValidationError("weights must be an IdentityCandidateWeights value")
        identity_revision = self._store.identity_revision
        evidence_by_pair = self._collect_evidence()
        results: list[IdentityCandidate] = []
        for pair in sorted(evidence_by_pair):
            evidence = evidence_by_pair[pair]
            evidence_json = json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            weights_json = (
                None
                if weights is None
                else json.dumps(dict(weights.weights), sort_keys=True, separators=(",", ":"))
            )
            score = (
                None
                if weights is None
                else math.fsum(
                    float(dict(evidence["signals"]).get(name, 0)) * value
                    for name, value in weights.weights
                )
            )
            if not persist:
                digest = hashlib.sha256((pair[0] + "\0" + pair[1]).encode()).hexdigest()[:24]
                results.append(
                    self._to_candidate(
                        candidate_id=f"preview-{digest}",
                        person_ids=pair,
                        status="pending",
                        candidate_revision=1,
                        evidence_version=1,
                        identity_revision=identity_revision,
                        evidence=evidence,
                        score=score,
                        weights_version=None if weights is None else weights.version,
                        operation_id=None,
                    )
                )
                continue

            row = self._store.query_one(
                "SELECT * FROM knowledge_identity_candidates"
                " WHERE person_low_id = ? AND person_high_id = ?",
                pair,
            )
            if row is None:
                candidate_id = self._store.new_id()
                now = self._store.now_ms()
                self._store.execute(
                    "INSERT INTO knowledge_identity_candidates"
                    " (candidate_id, person_low_id, person_high_id, status, candidate_revision,"
                    " evidence_version, identity_revision, created_ms, updated_ms)"
                    " VALUES (?, ?, ?, 'pending', 1, 1, ?, ?, ?)",
                    (candidate_id, *pair, identity_revision, now, now),
                )
                self._insert_evidence(
                    candidate_id,
                    1,
                    identity_revision,
                    evidence_json,
                    None if weights is None else weights.version,
                    weights_json,
                    score,
                    now,
                )
                row = self._store.query_one(
                    "SELECT * FROM knowledge_identity_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )
                assert row is not None
            else:
                candidate_id = str(row["candidate_id"])
                old = self._store.query_one(
                    "SELECT evidence_json, weights_version, weights_json, score"
                    " FROM knowledge_identity_candidate_evidence"
                    " WHERE candidate_id = ? AND evidence_version = ?",
                    (candidate_id, int(row["evidence_version"])),
                )
                assert old is not None
                changed = (
                    str(old["evidence_json"]) != evidence_json
                    or old["weights_version"] != (None if weights is None else weights.version)
                    or old["weights_json"] != weights_json
                    or old["score"] != score
                )
                revision_changed = int(row["identity_revision"]) != identity_revision
                if changed:
                    next_version = int(row["evidence_version"]) + 1
                    self._insert_evidence(
                        candidate_id,
                        next_version,
                        identity_revision,
                        evidence_json,
                        None if weights is None else weights.version,
                        weights_json,
                        score,
                        self._store.now_ms(),
                    )
                else:
                    next_version = int(row["evidence_version"])
                if changed or revision_changed:
                    self._store.execute(
                        "UPDATE knowledge_identity_candidates"
                        " SET evidence_version = ?, identity_revision = ?,"
                        " candidate_revision = candidate_revision + 1, updated_ms = ?"
                        " WHERE candidate_id = ?",
                        (next_version, identity_revision, self._store.now_ms(), candidate_id),
                    )
                    row = self._store.query_one(
                        "SELECT * FROM knowledge_identity_candidates WHERE candidate_id = ?",
                        (candidate_id,),
                    )
                    assert row is not None
            assert row is not None
            results.append(self._load_candidate(row))
        return tuple(results)

    def list_candidates(
        self,
        *,
        context: TrustedAdminContext,
        statuses: tuple[str, ...] = (),
    ) -> tuple[IdentityCandidate, ...]:
        self._identity._require_owner(context)
        checked = tuple(statuses)
        if any(item not in IDENTITY_CANDIDATE_STATUSES for item in checked):
            raise ValidationError("unknown identity candidate status")
        sql = "SELECT * FROM knowledge_identity_candidates"
        params: tuple[Any, ...] = ()
        if checked:
            sql += " WHERE status IN (" + ",".join("?" for _ in checked) + ")"
            params = checked
        sql += " ORDER BY updated_ms DESC, person_low_id, person_high_id"
        return tuple(self._load_candidate(row) for row in self._store.query(sql, params))

    def decide(
        self,
        candidate_id: str,
        *,
        decision: str,
        expected_candidate_revision: int,
        expected_identity_revision: int,
        context: TrustedAdminContext,
        target_id: str | None = None,
    ) -> IdentityCandidate:
        self._identity._require_owner(context)
        if decision not in ("merge", "not_same", "later"):
            raise ValidationError("decision must be merge, not_same or later")
        if int(expected_identity_revision) != self._store.identity_revision:
            raise KnowledgeError("stale_revision", "identity revision changed")
        row = self._store.query_one(
            "SELECT * FROM knowledge_identity_candidates WHERE candidate_id = ?",
            (str(candidate_id),),
        )
        if row is None:
            raise KnowledgeError("unresolved", "unknown identity candidate")
        if int(row["candidate_revision"]) != int(expected_candidate_revision):
            raise KnowledgeError("stale_revision", "candidate revision changed")
        if str(row["status"]) != "pending":
            raise KnowledgeError("identity_conflict", "candidate is no longer pending")
        people = (str(row["person_low_id"]), str(row["person_high_id"]))
        operation_id: str | None = None
        if decision == "merge":
            if target_id not in people:
                raise ValidationError("merge target must be one of the candidate people")
            if tuple(sorted(self._identity.canonical_ids(people))) != people:
                raise KnowledgeError("stale_revision", "candidate people have changed")
            protection = self._identity.provider_merge_protection_reason(people)
            if protection:
                raise KnowledgeError("identity_conflict", protection)
            source_id = people[1] if target_id == people[0] else people[0]
            receipt = self._identity.merge_people(
                str(target_id),
                source_id,
                expected_revision=expected_identity_revision,
                context=context,
            )
            operation_id = receipt.operation_id
            status = "merged"
        else:
            status = "rejected" if decision == "not_same" else "later"
        self._store.execute(
            "UPDATE knowledge_identity_candidates SET status = ?, operation_id = ?,"
            " candidate_revision = candidate_revision + 1, identity_revision = ?, updated_ms = ?"
            " WHERE candidate_id = ?",
            (
                status,
                operation_id,
                self._store.identity_revision,
                self._store.now_ms(),
                str(candidate_id),
            ),
        )
        updated = self._store.query_one(
            "SELECT * FROM knowledge_identity_candidates WHERE candidate_id = ?",
            (str(candidate_id),),
        )
        assert updated is not None
        return self._load_candidate(updated)

    def _collect_evidence(self) -> dict[tuple[str, str], dict[str, Any]]:
        active = {
            str(row["id"])
            for row in self._store.query("SELECT id FROM contacts WHERE status = 'active'")
        }
        names: dict[str, set[str]] = {item: set() for item in active}
        for row in self._store.query(
            "SELECT contact_id, alias, normalized_alias FROM contact_aliases"
            " WHERE status IN ('observed','candidate','confirmed') AND mapping_retracted = 0"
            " AND (valid_until_ms IS NULL OR valid_until_ms > ?)",
            (self._store.now_ms(),),
        ):
            person = self._identity.canonical_id(str(row["contact_id"]))
            if person in names:
                raw = str(row["normalized_alias"] or row["alias"])
                try:
                    names[person].add(normalize_alias_value(raw))
                except ValidationError:
                    continue
        for row in self._store.query(
            "SELECT id, display_name, preferred_name FROM contacts WHERE status = 'active'"
        ):
            person = self._identity.canonical_id(str(row["id"]))
            if person not in names:
                continue
            for raw in (row["display_name"], row["preferred_name"]):
                if raw:
                    try:
                        names[person].add(normalize_alias_value(str(raw)))
                    except ValidationError:
                        pass

        evidence: dict[tuple[str, str], dict[str, Any]] = {}

        def ensure(pair: tuple[str, str]) -> dict[str, Any]:
            return evidence.setdefault(
                pair,
                {"signals": {}, "common_names": [], "provider_pair_sources": []},
            )

        buckets: dict[str, list[str]] = {}
        for person_id, values in names.items():
            for value in values:
                buckets.setdefault(value, []).append(person_id)
        for value, people in buckets.items():
            for index, left in enumerate(sorted(set(people))):
                for right in sorted(set(people))[index + 1 :]:
                    pair = tuple(sorted((left, right)))
                    item = ensure(pair)
                    item["common_names"].append(value)

        provider_sources: dict[tuple[str, str], dict[tuple[str, str], set[str]]] = {}
        rows = self._store.query(
            "SELECT src.channel, src.namespace, src.source_locator,"
            " phone.person_id AS phone_person, lid.person_id AS lid_person"
            " FROM knowledge_provider_pair_sources AS src"
            " JOIN knowledge_identifier_bindings AS phone"
            " ON phone.channel = src.channel AND phone.kind = 'phone_jid'"
            " AND phone.namespace = src.namespace AND phone.value = src.phone_value"
            " AND phone.status = 'active'"
            " JOIN knowledge_identifier_bindings AS lid"
            " ON lid.channel = src.channel AND lid.kind = 'lid'"
            " AND lid.namespace = src.namespace AND lid.value = src.lid_value"
            " AND lid.status = 'active'"
        )
        for row in rows:
            left = self._identity.canonical_id(str(row["phone_person"]))
            right = self._identity.canonical_id(str(row["lid_person"]))
            if left == right or left not in names or right not in names:
                continue
            pair = tuple(sorted((left, right)))
            provider_sources.setdefault(pair, {}).setdefault(
                (str(row["channel"]), str(row["namespace"])), set()
            ).add(str(row["source_locator"]))

        for pair, by_account in provider_sources.items():
            item = ensure(pair)
            item["provider_pair_sources"] = [
                (channel, namespace, len(locators))
                for (channel, namespace), locators in sorted(by_account.items())
            ]

        for item in evidence.values():
            signals: dict[str, int] = {}
            if item["common_names"]:
                signals["normalized_name_match"] = len(set(item["common_names"]))
                item["common_names"] = sorted(set(item["common_names"]))
            pair_proofs = item["provider_pair_sources"]
            if pair_proofs:
                signals["provider_pair_source_count"] = sum(count for _, _, count in pair_proofs)
            item["signals"] = sorted(signals.items())
        return evidence

    def _insert_evidence(
        self,
        candidate_id: str,
        version: int,
        identity_revision: int,
        evidence_json: str,
        weights_version: str | None,
        weights_json: str | None,
        score: float | None,
        created_ms: int,
    ) -> None:
        self._store.execute(
            "INSERT INTO knowledge_identity_candidate_evidence"
            " (candidate_id, evidence_version, identity_revision, evidence_json,"
            " weights_version, weights_json, score, created_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                version,
                identity_revision,
                evidence_json,
                weights_version,
                weights_json,
                score,
                created_ms,
            ),
        )

    def _load_candidate(self, row: Any) -> IdentityCandidate:
        evidence_row = self._store.query_one(
            "SELECT evidence_json, weights_version, score"
            " FROM knowledge_identity_candidate_evidence"
            " WHERE candidate_id = ? AND evidence_version = ?",
            (str(row["candidate_id"]), int(row["evidence_version"])),
        )
        assert evidence_row is not None
        evidence = json.loads(str(evidence_row["evidence_json"]))
        return self._to_candidate(
            candidate_id=str(row["candidate_id"]),
            person_ids=(str(row["person_low_id"]), str(row["person_high_id"])),
            status=str(row["status"]),
            candidate_revision=int(row["candidate_revision"]),
            evidence_version=int(row["evidence_version"]),
            identity_revision=int(row["identity_revision"]),
            evidence=evidence,
            score=None if evidence_row["score"] is None else float(evidence_row["score"]),
            weights_version=(
                None if evidence_row["weights_version"] is None else str(evidence_row["weights_version"])
            ),
            operation_id=None if row["operation_id"] is None else str(row["operation_id"]),
        )

    @staticmethod
    def _to_candidate(
        *,
        candidate_id: str,
        person_ids: tuple[str, str],
        status: str,
        candidate_revision: int,
        evidence_version: int,
        identity_revision: int,
        evidence: dict[str, Any],
        score: float | None,
        weights_version: str | None,
        operation_id: str | None,
    ) -> IdentityCandidate:
        return IdentityCandidate(
            candidate_id=candidate_id,
            person_ids=person_ids,
            status=status,
            candidate_revision=candidate_revision,
            evidence_version=evidence_version,
            identity_revision=identity_revision,
            evidence=tuple((str(key), int(value)) for key, value in evidence["signals"]),
            common_names=tuple(str(item) for item in evidence["common_names"]),
            provider_pair_sources=tuple(
                (str(channel), str(namespace), int(count))
                for channel, namespace, count in evidence["provider_pair_sources"]
            ),
            score=score,
            weights_version=weights_version,
            operation_id=operation_id,
        )
