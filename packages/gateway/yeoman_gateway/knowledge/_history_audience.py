"""Fail-closed audience proofs for preserved historical events."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from yeoman_gateway.knowledge.models import KnowledgeError, TrustedAdminContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy

from ._history import HistoricalJournal

_TIME_CERTAINTIES = frozenset(
    {"canonical", "certain", "exact", "source_exact", "verified", "native", "provider_timestamp"}
)
_SNAPSHOT_CLASSES = frozenset({"membership_snapshot", "native_snapshot", "source_snapshot"})
_DIRECT_CHAT_KINDS = frozenset({"direct", "dm", "private"})
_UNKNOWN_SCOPE = "unknown"


@dataclass(frozen=True, slots=True)
class AudienceProof:
    status: str
    evidence_class: str
    members: frozenset[str]
    source_refs: tuple[Any, ...]
    valid_from_ms: int | None
    valid_until_ms: int | None
    proof_id: str | None


def roster_confirmation(
    *,
    channel: str,
    account: str,
    chat_id: str,
    members: Iterable[str],
    valid_from_ms: int,
    valid_until_ms: int,
) -> str:
    """Return the exact scope, roster and validity period an owner must confirm."""
    start, end = _integer(valid_from_ms), _integer(valid_until_ms)
    if start is None or end is None or end <= start:
        raise ValueError("confirmation requires a non-empty half-open validity period")
    canonical_members = sorted(_member_set(members))
    payload = json.dumps(
        [start, end, canonical_members], ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"ATTEST HISTORICAL ROSTER {channel}/{account}/{chat_id} [{start},{end}) {digest}"


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value.strip() != value:
        return None
    return value


def _member_set(value: Iterable[str]) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError("members must be an explicit sequence of principal strings")
    try:
        members = tuple(value)
    except TypeError as exc:
        raise ValueError("members must be an explicit sequence of principal strings") from exc
    if any(_text(member) is None for member in members):
        raise ValueError("members must contain non-empty canonical principal strings")
    if len(set(members)) != len(members):
        raise ValueError("members must not contain duplicates")
    return frozenset(members)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _event_time(event: Mapping[str, Any]) -> int | None:
    occurred = _integer(event.get("occurred_ms"))
    certainty = event.get("time_certainty")
    if occurred is None or certainty not in _TIME_CERTAINTIES:
        return None
    return occurred


def _scope(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    values = tuple(_text(event.get(key)) for key in ("channel", "account", "chat_id"))
    return values if all(values) else None  # type: ignore[return-value]


def _scope_key(scope: tuple[str, str, str]) -> str:
    return "/".join(scope)


def _valid_period(item: Mapping[str, Any]) -> tuple[int, int] | None:
    start = _integer(item.get("valid_from_ms"))
    end = _integer(item.get("valid_until_ms"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _refs(value: Any) -> tuple[Any, ...] | None:
    if isinstance(value, str):
        refs = (value,) if value.strip() else ()
    elif isinstance(value, Mapping):
        refs = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        refs = tuple(value)
    else:
        return None
    if not refs:
        return None
    for reference in refs:
        if isinstance(reference, str):
            if not reference.strip():
                return None
        elif isinstance(reference, Mapping):
            source_id = reference.get("source_id")
            meaningful_source = isinstance(source_id, str) and bool(source_id.strip())
            event_id = reference.get("event_id")
            meaningful_locator = isinstance(event_id, str) and bool(event_id.strip())
            if not meaningful_source and not meaningful_locator:
                return None
        else:
            return None
    try:
        json.dumps(refs, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return refs


def _unique_refs(refs: Iterable[Any]) -> tuple[Any, ...]:
    unique: list[Any] = []
    seen: set[str] = set()
    for reference in refs:
        key = json.dumps(reference, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            unique.append(reference)
            seen.add(key)
    return tuple(unique)


def _month(event: Mapping[str, Any]) -> str:
    moment = _event_time(event)
    if moment is None:
        return _UNKNOWN_SCOPE
    try:
        return datetime.fromtimestamp(moment / 1000, tz=UTC).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return _UNKNOWN_SCOPE


def _evidence_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _scope_is_compatible(item: Mapping[str, Any], scope: tuple[str, str, str]) -> bool:
    supplied = tuple(item.get(key) for key in ("channel", "account", "chat_id"))
    return all(value is None for value in supplied) or supplied == scope


class HistoryAudience:
    """Resolve native or owner-attested audiences without widening unknown proof."""

    def __init__(self, journal: HistoricalJournal) -> None:
        self.journal = journal

    def attest(
        self,
        *,
        channel: str,
        account: str,
        chat_id: str,
        members: Iterable[str],
        valid_from_ms: int,
        valid_until_ms: int,
        confirmation: str,
        context: TrustedAdminContext,
        policy: RuntimeKnowledgePolicy,
    ) -> str:
        scope = tuple(_text(value) for value in (channel, account, chat_id))
        if not all(scope):
            raise ValueError("channel, account and chat_id are required")
        member_set = _member_set(members)
        start, end = _integer(valid_from_ms), _integer(valid_until_ms)
        if start is None or end is None or end <= start:
            raise ValueError("attestation requires a non-empty half-open validity period")
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            confirmation,
            roster_confirmation(
                channel=channel,
                account=account,
                chat_id=chat_id,
                members=member_set,
                valid_from_ms=start,
                valid_until_ms=end,
            ),
        ):
            raise KnowledgeError("invalid_input", "exact roster confirmation is required")

        authorization_ref = policy.require_admin(context)
        proof_id = "attest-" + uuid.uuid4().hex
        references = ({"kind": "owner_attestation", "authorization_ref": authorization_ref},)
        with self.journal.store._write() as connection:
            connection.execute(
                """
                INSERT INTO history_audience_proofs (
                    proof_id, channel, account, chat_id, status, evidence_class,
                    members_json, source_refs_json, valid_from_ms, valid_until_ms,
                    created_ms, actor_principal, authorization_ref, confirmation
                ) VALUES (?, ?, ?, ?, 'known', 'owner_attested_roster', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proof_id,
                    channel,
                    account,
                    chat_id,
                    json.dumps(sorted(member_set), ensure_ascii=False),
                    json.dumps(references, ensure_ascii=False, sort_keys=True),
                    start,
                    end,
                    int(time.time() * 1000),
                    context.actor_principal,
                    authorization_ref,
                    confirmation,
                ),
            )
        return proof_id

    def revoke(
        self,
        proof_id: str,
        *,
        context: TrustedAdminContext,
        policy: RuntimeKnowledgePolicy,
    ) -> None:
        if not _text(proof_id):
            raise ValueError("proof_id is required")
        authorization_ref = policy.require_admin(context)
        with self.journal.store._write() as connection:
            cursor = connection.execute(
                """
                UPDATE history_audience_proofs
                SET revoked_ms = ?, revoked_by = ?, revocation_authorization_ref = ?
                WHERE proof_id = ? AND revoked_ms IS NULL
                """,
                (
                    int(time.time() * 1000),
                    context.actor_principal,
                    authorization_ref,
                    proof_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KnowledgeError("unresolved", "active audience proof was not found")

    def resolve(
        self,
        event: Mapping[str, Any],
        *,
        native_evidence: Sequence[Mapping[str, Any]] = (),
    ) -> AudienceProof:
        scope = _scope(event)
        moment = _event_time(event)
        if scope is None or moment is None:
            return self._unknown()

        raw_native = list(_evidence_items(event.get("native_evidence")))
        raw_native.extend(_evidence_items(native_evidence))
        candidates = self._native_proofs(event, scope, moment, raw_native)
        # Reconcile against the journal's current source authority as well as the
        # immutable rebuild snapshot.  A roster attestation is a disclosure proof; it
        # must not replace a still-valid native proof, and source revocation must win
        # over a copied, formerly eligible proof row.
        current_native = self._source_authority_proof(event, scope, moment)
        if current_native is not None:
            candidates.append(current_native)
        with self.journal.store._lock:
            rows = self.journal.store._conn.execute(
                """
                SELECT proof_id, status, evidence_class, members_json, source_refs_json,
                       valid_from_ms, valid_until_ms
                FROM history_audience_proofs
                WHERE channel = ? AND account = ? AND chat_id = ?
                  AND revoked_ms IS NULL AND valid_from_ms <= ? AND valid_until_ms > ?
                """,
                (*scope, moment, moment),
            ).fetchall()
        for row in rows:
            try:
                members = _member_set(json.loads(row["members_json"]))
                references = tuple(json.loads(row["source_refs_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            candidates.append(
                AudienceProof(
                    status=str(row["status"]),
                    evidence_class=str(row["evidence_class"]),
                    members=members,
                    source_refs=references,
                    valid_from_ms=int(row["valid_from_ms"]),
                    valid_until_ms=int(row["valid_until_ms"]),
                    proof_id=str(row["proof_id"]),
                )
            )
        if not candidates:
            return self._unknown()
        member_sets = {proof.members for proof in candidates}
        if len(member_sets) != 1:
            return self._unknown()
        selected = min(candidates, key=self._proof_precedence)
        return AudienceProof(
            status=selected.status,
            evidence_class=selected.evidence_class,
            members=selected.members,
            source_refs=_unique_refs(
                reference for proof in candidates for reference in proof.source_refs
            ),
            valid_from_ms=selected.valid_from_ms,
            valid_until_ms=selected.valid_until_ms,
            proof_id=selected.proof_id,
        )

    def _source_authority_proof(
        self,
        event: Mapping[str, Any],
        scope: tuple[str, str, str],
        moment: int,
    ) -> AudienceProof | None:
        event_id = _text(event.get("event_id"))
        revision = _integer(event.get("revision"))
        if event_id is None or revision is None or revision < 1:
            return None
        authority = self.journal.store.get_event_source_authority(event_id, revision)
        if (
            authority is None
            or authority.get("revoked_at_ms") is not None
            or authority.get("source_channel") != scope[0]
            or authority.get("source_chat_id") != scope[2]
            or _integer(authority.get("occurred_at_ms")) != moment
        ):
            return None
        author = _text(authority.get("author_principal"))
        status = str(authority.get("audience_status") or "unknown")
        try:
            members = _member_set(authority.get("audience_members") or ())
        except (TypeError, ValueError):
            return None
        if status == "known" and not members:
            return None
        if status == "author_only":
            if event.get("chat_kind") not in _DIRECT_CHAT_KINDS or author is None:
                return None
            members = frozenset({author})
        elif status != "known":
            return None

        with self.journal.store._lock:
            rows = self.journal.store._conn.execute(
                "SELECT p.source_id,p.locator_json,p.author_principal,p.channel,p.chat_id,"
                "p.occurred_ms,c.channel AS copy_channel,c.account,c.chat_id AS copy_chat_id "
                "FROM history_source_proofs p JOIN history_event_copies c "
                "ON c.event_id=p.event_id AND c.revision=p.revision "
                "AND c.source_id=p.source_id AND c.locator_json=p.locator_json "
                "WHERE p.event_id=? AND p.revision=? AND p.eligible=1 "
                "AND p.revoked_at_ms IS NULL AND c.channel=? AND c.account=? AND c.chat_id=? "
                "AND c.disposition<>'denied'",
                (event_id, str(revision), *scope),
            ).fetchall()
        if not rows:
            return None
        refs: list[dict[str, Any]] = []
        for row in rows:
            if (
                row["author_principal"] != author
                or row["channel"] != scope[0]
                or row["chat_id"] != scope[2]
                or _integer(row["occurred_ms"]) != moment
                or row["copy_channel"] != scope[0]
                or row["account"] != scope[1]
                or row["copy_chat_id"] != scope[2]
            ):
                continue
            try:
                locator = json.loads(str(row["locator_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(locator, Mapping):
                continue
            refs.append(
                {
                    "source_id": str(row["source_id"]),
                    "event_id": event_id,
                    "revision": revision,
                    "locator": dict(locator),
                }
            )
        if not refs:
            return None
        proof_id = str(authority.get("audience_snapshot_id") or "") or None
        if proof_id is None:
            proof_id = "native-source-" + hashlib.sha256(
                json.dumps(refs, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:24]
        return AudienceProof(
            status="author_only" if status == "author_only" else "known",
            evidence_class="native_source_authority",
            members=members,
            source_refs=tuple(refs),
            valid_from_ms=moment,
            valid_until_ms=moment + 1,
            proof_id=proof_id,
        )

    @staticmethod
    def _proof_precedence(proof: AudienceProof) -> int:
        return {
            "native_snapshot": 0,
            "native_membership_events": 1,
            "author_only": 2,
            "owner_attested_roster": 3,
        }.get(proof.evidence_class, 4)

    @staticmethod
    def _unknown() -> AudienceProof:
        return AudienceProof("unknown", "unknown", frozenset(), (), None, None, None)

    def _native_proofs(
        self,
        event: Mapping[str, Any],
        scope: tuple[str, str, str],
        moment: int,
        evidence: list[Mapping[str, Any]],
    ) -> list[AudienceProof]:
        snapshots: list[Mapping[str, Any]] = []
        changes: list[Mapping[str, Any]] = []
        author_proofs: list[Mapping[str, Any]] = []
        for item in evidence:
            evidence_class = item.get("evidence_class")
            if evidence_class == "native_membership_events":
                anchor = item.get("complete_anchor") or item.get("anchor")
                if isinstance(anchor, Mapping):
                    snapshots.append(anchor)
                changes.extend(_evidence_items(item.get("changes")))
            elif evidence_class in _SNAPSHOT_CLASSES:
                snapshots.append(item)
            elif evidence_class in {"membership_add", "membership_remove"}:
                anchor = item.get("complete_anchor") or item.get("anchor")
                if isinstance(anchor, Mapping):
                    snapshots.append(anchor)
                changes.append(item)
            elif evidence_class == "author_only":
                author_proofs.append(item)

        proofs: list[AudienceProof] = []
        direct_kind = event.get("chat_kind") or event.get("conversation_type")
        if direct_kind in _DIRECT_CHAT_KINDS:
            for item in author_proofs:
                proof = self._author_proof(item, event, scope, moment)
                if proof is not None:
                    proofs.append(proof)

        for snapshot in snapshots:
            proof_data = self._snapshot_data(snapshot, scope, moment)
            if proof_data is None:
                continue
            members, references, start, end = proof_data
            active_changes: list[tuple[str, str, tuple[Any, ...], int, int]] = []
            invalid_active_change = False
            for change in changes:
                if change.get("evidence_class") not in {"membership_add", "membership_remove"}:
                    continue
                if not _scope_is_compatible(change, scope):
                    continue
                period = _valid_period(change)
                raw_member = _text(change.get("member"))
                action = str(change.get("evidence_class"))
                if period is None or not (period[0] <= moment < period[1]):
                    continue
                change_refs = _refs(change.get("source_refs"))
                if raw_member is None or change_refs is None:
                    invalid_active_change = True
                    break
                if period[0] < start or period[1] > end:
                    invalid_active_change = True
                    break
                active_changes.append((raw_member, action, change_refs, *period))
            if invalid_active_change:
                continue
            actions: dict[str, set[str]] = defaultdict(set)
            for member, action, _, _, _ in active_changes:
                actions[member].add(action)
            if any(len(value) > 1 for value in actions.values()):
                continue
            if active_changes:
                resolved_members = set(members)
                for member, action in actions.items():
                    if "membership_add" in action:
                        resolved_members.add(member)
                    else:
                        resolved_members.discard(member)
                refs = _unique_refs(
                    (*references, *(ref for _, _, refs_, _, _ in active_changes for ref in refs_))
                )
                proof_start = max([start, *(item[3] for item in active_changes)])
                proof_end = min([end, *(item[4] for item in active_changes)])
                evidence_class = "native_membership_events"
            else:
                resolved_members = set(members)
                refs = references
                proof_start, proof_end = start, end
                evidence_class = "native_snapshot"
            proofs.append(
                self._native_result(
                    evidence_class,
                    frozenset(resolved_members),
                    refs,
                    proof_start,
                    proof_end,
                )
            )
        return proofs

    def _snapshot_data(
        self,
        item: Mapping[str, Any],
        scope: tuple[str, str, str],
        moment: int,
    ) -> tuple[frozenset[str], tuple[Any, ...], int, int] | None:
        if item.get("evidence_class") not in _SNAPSHOT_CLASSES:
            return None
        if not _scope_is_compatible(item, scope):
            return None
        period = _valid_period(item)
        references = _refs(item.get("source_refs"))
        raw_members = item.get("members")
        if (
            period is None
            or references is None
            or not isinstance(raw_members, Sequence)
            or isinstance(raw_members, (str, bytes))
            or not period[0] <= moment < period[1]
        ):
            return None
        try:
            members = _member_set(raw_members)
        except (TypeError, ValueError):
            return None
        return members, references, period[0], period[1]

    def _author_proof(
        self,
        item: Mapping[str, Any],
        event: Mapping[str, Any],
        scope: tuple[str, str, str],
        moment: int,
    ) -> AudienceProof | None:
        if not _scope_is_compatible(item, scope):
            return None
        period = _valid_period(item)
        references = _refs(item.get("source_refs"))
        author = _text(item.get("author_principal")) or _text(event.get("principal"))
        if period is None or references is None or author is None or not period[0] <= moment < period[1]:
            return None
        return self._native_result(
            "author_only", frozenset({author}), references, period[0], period[1], status="author_only"
        )

    @staticmethod
    def _native_result(
        evidence_class: str,
        members: frozenset[str],
        source_refs: tuple[Any, ...],
        start: int,
        end: int,
        *,
        status: str = "known",
    ) -> AudienceProof:
        material = json.dumps(
            [evidence_class, source_refs, start, end, sorted(members)],
            ensure_ascii=False,
            sort_keys=True,
        )
        proof_id = "native-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        return AudienceProof(
            status, evidence_class, members, source_refs, start, end, proof_id
        )

    def coverage(self, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        chat_events: dict[tuple[str, str, str], list[tuple[Mapping[str, Any], AudienceProof]]] = defaultdict(list)
        proposals: Counter[tuple[str, str]] = Counter()
        status_totals: Counter[str] = Counter()
        for event in events:
            if not isinstance(event, Mapping):
                continue
            # Coverage is metadata-only: retain each known component even when the
            # account is unknown. resolve() above still requires a complete strict scope.
            scope = tuple(
                _text(event.get(key)) or _UNKNOWN_SCOPE for key in ("channel", "account", "chat_id")
            )
            month = _month(event)
            key = (*scope, month)
            bucket = buckets.setdefault(
                key,
                {
                    "channel": scope[0],
                    "account": scope[1],
                    "chat_id": scope[2],
                    "month": month,
                    "event_count": 0,
                    "unknown_time_count": 0,
                    "source_gap_count": 0,
                    "known_audience_count": 0,
                    "author_only_count": 0,
                    "unknown_audience_count": 0,
                    "known_roster_sizes": [],
                    "seen_sender_proposals": Counter(),
                },
            )
            bucket["event_count"] += 1
            if _event_time(event) is None:
                bucket["unknown_time_count"] += 1
            native = _evidence_items(event.get("native_evidence"))
            raw_refs = _refs(event.get("source_refs"))
            if raw_refs is None and not any(_refs(item.get("source_refs")) for item in native):
                bucket["source_gap_count"] += 1
            for field in ("sender_raw", "principal"):
                candidate = _text(event.get(field))
                if candidate is None:
                    continue
                proposals[(field, candidate)] += 1
                bucket["seen_sender_proposals"][(field, candidate)] += 1
            proof = self.resolve(event)
            status_totals[proof.status] += 1
            if proof.status == "known":
                bucket["known_audience_count"] += 1
                bucket["known_roster_sizes"].append(len(proof.members))
            elif proof.status == "author_only":
                bucket["author_only_count"] += 1
                bucket["known_roster_sizes"].append(len(proof.members))
            else:
                bucket["unknown_audience_count"] += 1
            chat_events[scope].append((event, proof))

        months = sorted({key[3] for key in buckets if key[3] != _UNKNOWN_SCOPE})
        month_age = {month: len(months) - index for index, month in enumerate(months)}
        rendered_buckets: list[dict[str, Any]] = []
        for key in sorted(buckets):
            bucket = buckets[key]
            sizes = bucket.pop("known_roster_sizes")
            proposal_counts = bucket.pop("seen_sender_proposals")
            bucket["smallest_known_roster_size"] = min(sizes) if sizes else None
            bucket["seen_sender_proposals"] = [
                {"source_field": field, "candidate": candidate, "count": count, "status": "unverified_proposal"}
                for (field, candidate), count in sorted(proposal_counts.items())
            ]
            bucket["historic_rank"] = month_age.get(bucket["month"], 0)
            rendered_buckets.append(bucket)
        priority = sorted(
            rendered_buckets,
            key=lambda item: (
                item["smallest_known_roster_size"] is None,
                item["smallest_known_roster_size"]
                if item["smallest_known_roster_size"] is not None
                else 0,
                -item["historic_rank"],
                item["channel"],
                item["account"],
                item["chat_id"],
                item["month"],
            ),
        )
        for index, item in enumerate(priority, start=1):
            item["priority_rank"] = index
        return {
            "schema": "history-coverage-v1",
            "event_count": sum(item["event_count"] for item in rendered_buckets),
            "chat_count": len(chat_events),
            "audience_status_counts": dict(sorted(status_totals.items())),
            "buckets": rendered_buckets,
            "priority_order": [
                {
                    "channel": item["channel"],
                    "account": item["account"],
                    "chat_id": item["chat_id"],
                    "month": item["month"],
                    "priority_rank": item["priority_rank"],
                    "smallest_known_roster_size": item["smallest_known_roster_size"],
                    "historic_rank": item["historic_rank"],
                }
                for item in priority
            ],
            "seen_sender_proposals": [
                {"source_field": field, "candidate": candidate, "count": count, "status": "unverified_proposal"}
                for (field, candidate), count in sorted(proposals.items())
            ],
        }

    def roster_review(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        current_members: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_list = [event for event in events if isinstance(event, Mapping)]
        coverage = self.coverage(event_list)
        by_chat: dict[tuple[str, str, str], list[tuple[Mapping[str, Any], AudienceProof]]] = defaultdict(list)
        for event in event_list:
            scope = _scope(event) or (_UNKNOWN_SCOPE,) * 3
            by_chat[scope].append((event, self.resolve(event)))
        chats: list[dict[str, Any]] = []
        for scope, entries in sorted(by_chat.items()):
            proofs = [proof for _, proof in entries]
            known_sets = {proof.members for proof in proofs if proof.status in {"known", "author_only"}}
            complete = bool(proofs) and all(proof.status in {"known", "author_only"} for proof in proofs) and len(known_sets) == 1
            current = self._current_for(current_members, scope)
            sender_proposals: Counter[tuple[str, str]] = Counter(
                (field, candidate)
                for event, _proof in entries
                for field in ("sender_raw", "principal")
                if (candidate := _text(event.get(field))) is not None
            )
            by_month: dict[str, list[AudienceProof]] = defaultdict(list)
            for event, proof in entries:
                by_month[_month(event)].append(proof)
            chats.append(
                {
                    "channel": scope[0],
                    "account": scope[1],
                    "chat_id": scope[2],
                    "event_count": len(entries),
                    "roster": {
                        "status": proofs[0].status if complete else "unknown",
                        "members": sorted(next(iter(known_sets))) if complete else [],
                        "source_refs": list(_unique_refs(ref for proof in proofs for ref in proof.source_refs)),
                        "evidence_classes": sorted({proof.evidence_class for proof in proofs}),
                    },
                    "months": [
                        {
                            "month": month,
                            "event_count": len(month_proofs),
                            "roster": self._proof_summary(month_proofs),
                        }
                        for month, month_proofs in sorted(by_month.items())
                    ],
                    "current_members": {
                        "available": current is not None,
                        "member_count": len(current) if current is not None else None,
                    },
                    "seen_sender_proposals": [
                        {"source_field": field, "candidate": candidate, "count": count, "status": "unverified_proposal"}
                        for (field, candidate), count in sorted(sender_proposals.items())
                    ],
                }
            )
        return {"schema": "history-roster-review-v1", "coverage": coverage, "chats": chats}

    @staticmethod
    def _proof_summary(proofs: Sequence[AudienceProof]) -> dict[str, Any]:
        member_sets = {proof.members for proof in proofs if proof.status in {"known", "author_only"}}
        complete = bool(proofs) and all(proof.status in {"known", "author_only"} for proof in proofs) and len(member_sets) == 1
        return {
            "status": proofs[0].status if complete else "unknown",
            "members": sorted(next(iter(member_sets))) if complete else [],
            "source_refs": list(_unique_refs(ref for proof in proofs for ref in proof.source_refs)),
            "evidence_classes": sorted({proof.evidence_class for proof in proofs}),
        }

    @staticmethod
    def _current_for(current_members: Mapping[str, Any] | None, scope: tuple[str, str, str]) -> tuple[Any, ...] | None:
        if current_members is None:
            return None
        value = None
        for key in (_scope_key(scope), ":".join(scope)):
            if key in current_members:
                value = current_members[key]
                break
        if isinstance(value, Mapping):
            value = value.get("members")
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        return tuple(value)
