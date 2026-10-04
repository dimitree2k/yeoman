"""Read-only, aggregate-only proposals for historical speaker attribution."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ._history_reader import _NATIVE_SOURCE_AUTHORITIES
from ._identity_audit import (
    IdentityAuditError,
    _assert_snapshot_stable,
    _audit_redirects,
    _binding_key,
    _has_native_locator,
    _load_store,
    _observation_identifiers,
    _open_snapshot,
    _resolve_observed_identifier,
)

_STATUSES = ("candidate", "confirmed", "conflict", "unresolved")
_EXACT_TIMES = frozenset({"native", "provider_timestamp"})
_SAFE_KINDS = frozenset(
    {"message", "reaction", "receipt", "delete", "edit", "media", "external_action", "outbound_request", "outbound_result", "text"}
)


def plan_speaker_attribution(
    history_db: Path, knowledge_db: Path, *, include_details: bool = False
) -> dict[str, Any]:
    """Summarize source-bound speaker proposals without changing either input.

    The denominator is retained, non-denied inbound message revisions. The result
    contains counts only: identity values, source principals, roles and ACL data are
    never returned or modified.
    """
    history_connection: sqlite3.Connection | None = None
    knowledge_connection: sqlite3.Connection | None = None
    try:
        history_connection, history_snapshot = _open_snapshot("history", history_db)
        knowledge_connection, knowledge_snapshot = _open_snapshot("knowledge", knowledge_db)
        _require_columns(
            history_connection,
            "history_event_details",
            ("event_id", "revision", "normalized_json", "semantic_kind", "semantic_direction", "retention_status", "denied"),
        )
        _require_columns(
            history_connection,
            "history_event_copies",
            ("copy_id", "event_id", "revision", "source_id", "source_hash", "locator_json", "provenance_class", "source_authority", "channel", "account", "chat_id", "disposition"),
        )
        _require_columns(
            history_connection,
            "history_source_proofs",
            ("event_id", "revision", "source_id", "locator_json", "channel", "chat_id", "occurred_ms", "eligible", "revoked_at_ms", "denial_reason"),
        )
        _require_columns(
            knowledge_connection,
            "knowledge_identifier_bindings",
            ("binding_id", "channel", "kind", "namespace", "value", "person_id", "status", "valid_from_ms", "valid_until_ms", "mapping_verified", "evidence_ref"),
        )
        _require_columns(
            knowledge_connection,
            "knowledge_identity_redirects",
            ("operation_id", "seq", "source_id", "target_id", "active"),
        )

        identity = _load_store(knowledge_connection)
        canonical_ids = _audit_redirects(
            identity["knowledge_identity_redirects"],
            {str(row.get("id")) for row in identity["contacts"]["rows"]},
        )["canonical_ids"]
        bindings = identity["knowledge_identifier_bindings"]["rows"]
        contacts = {
            str(row.get("id")): row
            for row in identity["contacts"]["rows"]
            if row.get("id") is not None
        }

        excluded = Counter(
            {"denied": _count(history_connection, "denied = 1"),
             "denial_unknown": _count(history_connection, "denied IS NULL"),
             "retention": _count(history_connection, "denied = 0 AND retention_status != 'retained'")}
        )
        excluded += Counter()  # discard zero-valued categories
        total_revisions, skipped = _revision_counts(history_connection)
        decisions: Counter[str] = Counter()
        reasons: dict[str, Counter[str]] = defaultdict(Counter)
        details: list[dict[str, Any]] = []
        denominator = 0
        for event_id, revision, event, source_rows in _events(history_connection):
            denominator += 1
            detail = {} if include_details else None
            status, reason = _decide(
                event, source_rows, bindings, canonical_ids, detail=detail
            )
            decisions[status] += 1
            if reason is not None:
                reasons[status][reason] += 1
            if detail is not None:
                detail["decision"] = {"status": status, "reason": reason}
                candidate_ids = sorted(
                    {
                        item["canonical_person_id"]
                        for item in detail.get("binding_evidence", [])
                        if item["status"] in {"active", "ended"}
                    }
                )
                detail["canonical_candidate_person_ids"] = candidate_ids
                detail["canonical_people"] = [
                    {
                        "canonical_person_id": person_id,
                        "display_only_label": _display_label(
                            person_id, contacts, canonical_ids
                        ),
                    }
                    for person_id in candidate_ids
                ]
                details.append(detail)

        _assert_snapshot_stable("history", history_snapshot)
        _assert_snapshot_stable("knowledge", knowledge_snapshot)
        counts = {status: decisions[status] for status in _STATUSES}
        result = {
            "schema_version": 1,
            "aggregate": {
                "scope": "retained_non_denied_inbound_message_event_revisions",
                "history_event_revisions": total_revisions,
                "denominator": denominator,
                "skipped_event_revisions": total_revisions - denominator,
                "skipped_event_revision_counts": dict(sorted(skipped.items())),
                "decision_counts": counts,
                "confirmed_fraction": {
                    "numerator": counts["confirmed"],
                    "denominator": denominator,
                },
                "reason_counts": {
                    status: dict(sorted(reasons[status].items()))
                    for status in _STATUSES
                    if reasons[status]
                },
                "excluded_counts": dict(sorted(excluded.items())),
                "inputs_unchanged": True,
            },
        }
        if include_details:
            result["details"] = details
        return result
    except IdentityAuditError:
        raise
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise IdentityAuditError("cannot safely plan speaker attribution") from exc
    finally:
        if history_connection is not None:
            history_connection.close()
        if knowledge_connection is not None:
            knowledge_connection.close()


def _require_columns(
    connection: sqlite3.Connection, table: str, required: tuple[str, ...]
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if not set(required).issubset(columns):
        raise IdentityAuditError(f"input lacks required {table} fields")


def _count(connection: sqlite3.Connection, where: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM history_event_details "
            "WHERE semantic_direction = 'in' AND semantic_kind = 'message' AND "
            "(" + where + ")"
        ).fetchone()[0]
    )


def _revision_counts(connection: sqlite3.Connection) -> tuple[int, Counter[str]]:
    skipped: Counter[str] = Counter()
    total = 0
    for row in connection.execute(
        "SELECT semantic_direction, semantic_kind, retention_status, denied "
        "FROM history_event_details"
    ):
        total += 1
        if (
            row["semantic_direction"] == "in"
            and row["semantic_kind"] == "message"
            and row["retention_status"] == "retained"
            and row["denied"] == 0
        ):
            continue
        direction = row["semantic_direction"]
        direction = direction if direction in {"in", "out"} else "other"
        kind = str(row["semantic_kind"] or "").casefold()
        kind = kind if kind in _SAFE_KINDS else "other"
        skipped[f"{direction}:{kind}"] += 1
    return total, skipped


def _events(connection: sqlite3.Connection) -> Iterator[tuple[str, str, dict[str, Any], list[dict[str, Any]]]]:
    rows = connection.execute(
        "SELECT d.event_id, d.revision, "
        "json_extract(d.normalized_json, '$.channel') AS event_channel, "
        "json_extract(d.normalized_json, '$.account') AS event_account, "
        "json_extract(d.normalized_json, '$.chat_id') AS event_chat_id, "
        "json_extract(d.normalized_json, '$.sender_raw') AS sender_raw, "
        "json_extract(d.normalized_json, '$.sender_id_raw') AS sender_id_raw, "
        "json_extract(d.normalized_json, '$.participant_jid_raw') AS participant_jid_raw, "
        "json_extract(d.normalized_json, '$.occurred_ms') AS occurred_ms, "
        "json_extract(d.normalized_json, '$.time_certainty') AS time_certainty, "
        "c.source_id, c.source_hash, c.locator_json, c.provenance_class, "
        "c.source_authority, c.channel AS copy_channel, c.account AS copy_account, "
        "c.chat_id AS copy_chat_id, p.channel AS proof_channel, p.chat_id AS proof_chat_id, "
        "p.occurred_ms AS proof_occurred_ms, p.eligible, p.revoked_at_ms, p.denial_reason "
        "FROM history_event_details AS d "
        "LEFT JOIN history_event_copies AS c ON c.event_id=d.event_id "
        "AND c.revision=d.revision AND c.disposition='logical_copy' "
        "LEFT JOIN history_source_proofs AS p ON p.event_id=c.event_id "
        "AND p.revision=c.revision AND p.source_id=c.source_id "
        "AND p.locator_json=c.locator_json "
        "WHERE d.semantic_direction='in' AND d.semantic_kind='message' "
        "AND d.retention_status='retained' AND d.denied=0 "
        "ORDER BY d.event_id, d.revision, c.copy_id"
    )
    current_key: tuple[str, str] | None = None
    current_event: dict[str, Any] = {}
    source_rows: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["event_id"]), str(row["revision"]))
        if current_key is not None and key != current_key:
            yield current_key[0], current_key[1], current_event, source_rows
            source_rows = []
        current_key = key
        current_event = {
            "event_id": key[0],
            "revision": key[1],
            "channel": row["event_channel"],
            "account": row["event_account"],
            "chat_id": row["event_chat_id"],
            "sender_raw": row["sender_raw"],
            "sender_id_raw": row["sender_id_raw"],
            "participant_jid_raw": row["participant_jid_raw"],
            "occurred_ms": row["occurred_ms"],
            "time_certainty": row["time_certainty"],
        }
        if row["source_id"] is not None:
            source_rows.append(dict(row))
    if current_key is not None:
        yield current_key[0], current_key[1], current_event, source_rows


def _decide(
    event: Mapping[str, Any],
    source_rows: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    canonical_ids: dict[str, str],
    *,
    detail: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    if detail is not None:
        detail.update(
            {
                "event": {
                    "event_id": event.get("event_id"),
                    "revision": event.get("revision"),
                    "occurred_ms": event.get("occurred_ms"),
                    "time_certainty": event.get("time_certainty"),
                },
                "supporting_sources": [],
                "binding_evidence": [],
                "resolved_person_id": None,
            }
        )
    explicit_account = event.get("account")
    explicit_channel = event.get("channel")
    event_chat = event.get("chat_id")
    native_time = _has_native_time(event)
    valid_sources: list[dict[str, Any]] = []
    scope_mismatch = False
    time_mismatch = False
    for row in source_rows:
        copy_account = row.get("copy_account")
        copy_channel = row.get("copy_channel")
        proof_channel = row.get("proof_channel")
        proof_chat = row.get("proof_chat_id")
        copy_chat = row.get("copy_chat_id")
        locator = _json_object(row.get("locator_json"))
        if (
            row.get("eligible") != 1
            or row.get("revoked_at_ms") is not None
            or row.get("denial_reason") not in (None, "")
            or row.get("source_authority") not in _NATIVE_SOURCE_AUTHORITIES
            or not _has_native_locator(
                {
                    "source_hash": row.get("source_hash"),
                    "locator": locator,
                    "provenance_class": row.get("provenance_class"),
                }
            )
        ):
            continue
        if detail is not None:
            detail["supporting_sources"].append(
                _source_evidence(row, event.get("occurred_ms"))
            )
        if (
            (explicit_account is not None and not isinstance(explicit_account, str))
            or (explicit_channel is not None and not isinstance(explicit_channel, str))
            or (
                isinstance(explicit_account, str)
                and isinstance(copy_account, str)
                and copy_account.casefold() != explicit_account.casefold()
            )
            or (
                isinstance(explicit_channel, str)
                and isinstance(copy_channel, str)
                and copy_channel.casefold() != explicit_channel.casefold()
            )
            or (
                isinstance(copy_channel, str)
                and isinstance(proof_channel, str)
                and proof_channel.casefold() != copy_channel.casefold()
            )
            or (isinstance(event_chat, str) and isinstance(copy_chat, str) and copy_chat != event_chat)
            or (isinstance(copy_chat, str) and isinstance(proof_chat, str) and proof_chat != copy_chat)
        ):
            scope_mismatch = True
            continue
        if (
            not isinstance(copy_account, str)
            or not isinstance(copy_channel, str)
            or not isinstance(proof_channel, str)
            or not isinstance(event_chat, str)
            or not isinstance(copy_chat, str)
            or not isinstance(proof_chat, str)
            or copy_chat != event_chat
            or proof_chat != event_chat
        ):
            continue
        proof_time = row.get("proof_occurred_ms")
        event_time = event.get("occurred_ms")
        if (
            native_time
            and _positive_timestamp(proof_time)
            and _positive_timestamp(event_time)
            and proof_time != event_time
        ):
            time_mismatch = True
            continue
        valid_sources.append(row)
    if time_mismatch:
        return "conflict", "source_time_conflict"
    if scope_mismatch:
        return "unresolved", "account_mismatch"
    if not valid_sources:
        return "unresolved", "source_unproven"
    scopes = {
        (str(row["copy_channel"]).casefold(), str(row["copy_account"]).casefold())
        for row in valid_sources
    }
    if len(scopes) != 1:
        return "conflict", "source_scope_conflict"
    event_channel, event_account = next(iter(scopes))
    identifier_rows, parse_reason = _observation_identifiers(
        {
            "channel": event_channel,
            "account": event_account,
            "sender_raw": event.get("sender_raw"),
            "sender_id_raw": event.get("sender_id_raw"),
            "participant_jid_raw": event.get("participant_jid_raw"),
        }
    )
    if len(identifier_rows) != 1:
        return "unresolved", parse_reason or "sender_identifier_missing"
    identifier = identifier_rows[0]
    identifier_key = _binding_key(identifier)
    matching = [row for row in bindings if _binding_key(row) == identifier_key]
    if detail is not None:
        detail["binding_evidence"] = [
            _binding_evidence(row, canonical_ids) for row in matching
        ]
    if not matching:
        return "unresolved", "no_binding"
    occurred_ms = event.get("occurred_ms")
    exact_time = native_time and all(
        _positive_timestamp(row.get("proof_occurred_ms"))
        and row["proof_occurred_ms"] == occurred_ms
        for row in valid_sources
    )
    if not exact_time:
        return "candidate", "event_time_not_native"
    resolution = _resolve_observed_identifier(
        identifier,
        event,
        bindings,
        canonical_ids,
    )
    if resolution.get("status") == "resolved":
        binding = next(
            (row for row in matching if row.get("binding_id") == resolution.get("binding_id")),
            None,
        )
        if binding is None or not bool(int(binding.get("mapping_verified") or 0)):
            return "candidate", "binding_not_verified"
        if not str(binding.get("evidence_ref") or "").strip():
            return "candidate", "binding_evidence_missing"
        if detail is not None:
            detail["resolved_person_id"] = resolution["canonical_person_id"]
        return "confirmed", None
    reason = str(resolution.get("reason") or "unresolved")
    if reason in {"binding_conflict", "binding_start_conflict"}:
        return "conflict", reason
    if reason in {"binding_start_unknown", "binding_not_authoritative"}:
        return "candidate", reason
    return "unresolved", reason


def _source_evidence(
    row: Mapping[str, Any], event_occurred_ms: Any
) -> dict[str, Any]:
    return {
        "source_id": row.get("source_id"),
        "source_hash": row.get("source_hash"),
        "locator": _json_object(row.get("locator_json")),
        "provenance_class": row.get("provenance_class"),
        "source_authority": row.get("source_authority"),
        "channel": row.get("copy_channel"),
        "account": row.get("copy_account"),
        "proof_channel": row.get("proof_channel"),
        "event_occurred_ms": event_occurred_ms,
        "proof_occurred_ms": row.get("proof_occurred_ms"),
        "proof_eligible": row.get("eligible") == 1,
    }


def _binding_evidence(
    row: Mapping[str, Any], canonical_ids: Mapping[str, str]
) -> dict[str, Any]:
    person_id = str(row.get("person_id") or "")
    return {
        "binding_id": row.get("binding_id"),
        "person_id": person_id,
        "canonical_person_id": canonical_ids.get(person_id, person_id),
        "status": row.get("status"),
        "valid_from_ms": row.get("valid_from_ms"),
        "valid_until_ms": row.get("valid_until_ms"),
        "mapping_verified": bool(int(row.get("mapping_verified") or 0)),
        "evidence_ref": row.get("evidence_ref"),
    }


def _display_label(
    canonical_person_id: str,
    contacts: Mapping[str, Mapping[str, Any]],
    canonical_ids: Mapping[str, str],
) -> str | None:
    person_ids = [
        person_id
        for person_id in contacts
        if canonical_ids.get(person_id, person_id) == canonical_person_id
    ]
    person_ids.sort(key=lambda person_id: (person_id != canonical_person_id, person_id))
    for person_id in person_ids:
        contact = contacts[person_id]
        for field in ("display_name", "preferred_name"):
            label = contact.get(field)
            if isinstance(label, str) and label.strip():
                return label.strip()
    return None


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_native_time(event: Mapping[str, Any]) -> bool:
    occurred_ms = event.get("occurred_ms")
    return (
        _positive_timestamp(occurred_ms)
        and str(event.get("time_certainty") or "") in _EXACT_TIMES
    )


def _positive_timestamp(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
