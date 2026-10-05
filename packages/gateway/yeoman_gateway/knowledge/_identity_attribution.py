"""Read-only, aggregate-only proposals for historical speaker attribution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ._history_reader import _NATIVE_SOURCE_AUTHORITIES
from ._identity_attribution_basis import (
    account_for_event,
    resolve_attribution_binding,
    validate_attribution_evidence,
)
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
from .models import Identifier

_STATUSES = ("candidate", "confirmed", "conflict", "unresolved")
_EXACT_TIMES = frozenset({"native", "provider_timestamp"})
_SAFE_KINDS = frozenset(
    {"message", "reaction", "receipt", "delete", "edit", "media", "external_action", "outbound_request", "outbound_result", "text"}
)


def plan_speaker_attribution(
    history_db: Path,
    knowledge_db: Path,
    *,
    include_details: bool = False,
    attribution_evidence: Mapping[str, Any] | None = None,
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
        evidence = None
        if attribution_evidence is not None:
            evidence = validate_attribution_evidence(
                attribution_evidence,
                input_hashes={
                    "history": _sha256(history_db),
                    "knowledge": _sha256(knowledge_db),
                },
            )
            evidence = _validate_evidence_locators(
                evidence, history_connection, knowledge_connection
            )
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
        basis_counts: dict[str, Counter[str]] = defaultdict(Counter)
        reasons: dict[str, Counter[str]] = defaultdict(Counter)
        source_unproven_diagnostics: Counter[str] = Counter()
        unknown_audience: Counter[str] = Counter()
        details: list[dict[str, Any]] = []
        denominator = 0
        for event_id, revision, event, source_rows in _events(history_connection):
            denominator += 1
            detail = {} if include_details else None
            inferred_account, account_reason, account_refs = (None, None, [])
            if evidence is not None and event.get("account") is None:
                if _positive_timestamp(event.get("occurred_ms")):
                    inferred_account, account_reason, account_refs = account_for_event(
                        evidence,
                        channel=str(event.get("channel") or ""),
                        at_ms=event["occurred_ms"],
                    )
                else:
                    account_reason = "event_time_unknown"
            status, reason, basis = _decide(
                event,
                source_rows,
                bindings,
                canonical_ids,
                detail=detail,
                evidence=evidence,
                inferred_account=inferred_account,
                account_refs=account_refs,
                account_reason=account_reason,
                source_unproven_diagnostics=source_unproven_diagnostics,
            )
            decisions[status] += 1
            basis_counts[status][basis or "unresolved_or_unbased"] += 1
            if reason is not None:
                reasons[status][reason] += 1
            if any(row.get("audience_status") in (None, "unknown") for row in source_rows):
                unknown_audience[status] += 1
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
                "decision_counts_by_basis": {
                    status: dict(sorted(basis_counts[status].items()))
                    for status in _STATUSES
                    if basis_counts[status]
                },
                "source_unproven_diagnostics": dict(sorted(source_unproven_diagnostics.items())),
                "unknown_audience_proposal_counts": dict(sorted(unknown_audience.items())),
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_evidence_locators(
    evidence: Mapping[str, Any],
    history: sqlite3.Connection,
    knowledge: sqlite3.Connection,
) -> dict[str, Any]:
    inventories = evidence["account_inventories"]
    for inventory in inventories:
        start, end = (int(inventory["covered_period"][key]) for key in ("start_ms", "end_ms"))
        channel = str(inventory["channel"])
        config = inventory["sources"]["config"]
        if config["complete"]:
            if config.get("reviewed") is not True or config.get("coverage_complete") is not True:
                raise IdentityAuditError("config inventory lacks reviewed period coverage")
        for name, source in inventory["sources"].items():
            for ref in source["source_refs"]:
                if name == "config":
                    _validate_config_ref(ref, source, evidence)
                elif name == "journal":
                    _history_copy_for_ref(
                        history, ref, expected_channel=channel, require_proof_time=False
                    )
                else:
                    _binding_for_ref(knowledge, ref, expected_channel=channel)
        if config["complete"]:
            actual = _config_accounts(config, inventory, evidence)
            if actual != _account_set(config["accounts"]):
                raise IdentityAuditError("config account inventory mismatch")
        journal = inventory["sources"]["journal"]
        if journal["complete"]:
            rows = _history_inventory_rows(history, channel, start, end)
            expected_ids = {int(ref["locator"]["copy_id"]) for ref in journal["source_refs"]}
            if expected_ids != {int(row["copy_id"]) for row in rows}:
                raise IdentityAuditError("journal inventory does not cover pinned rows")
            if _account_set(row["account"] for row in rows if row["account"]) != _account_set(journal["accounts"]):
                raise IdentityAuditError("journal account inventory mismatch")
        binding_inventory = inventory["sources"]["bindings"]
        if binding_inventory["complete"]:
            rows = _binding_inventory_rows(knowledge, channel, start, end)
            expected_ids = {str(ref["locator"]["binding_id"]) for ref in binding_inventory["source_refs"]}
            if expected_ids != {str(row["binding_id"]) for row in rows}:
                raise IdentityAuditError("binding inventory does not cover pinned rows")
            if _account_set(row["namespace"] for row in rows if row["namespace"]) != _account_set(binding_inventory["accounts"]):
                raise IdentityAuditError("binding account inventory mismatch")
    for observation in evidence.get("observations", []):
        row, event = _history_copy_for_ref(history, observation["source_ref"])
        _validate_observation_source(observation, row, event, evidence)
    valid_spans: list[Mapping[str, Any]] = []
    for span in evidence.get("continuity_spans", []):
        if span.get("reviewed") is not True or span.get("coverage_complete") is not True:
            continue
        expected = {_reference_key(ref) for ref in span["evidence_refs"]}
        observed = {
            _reference_key(row["source_ref"])
            for row in evidence.get("observations", [])
            if row.get("observed_ms") is not None
            and int(span["start_ms"]) <= int(row["observed_ms"]) < int(span["end_ms"])
            and _evidence_identifier_matches(row.get("identifier"), span.get("identifier"))
        }
        pinned = _pinned_phone_span_refs(history, evidence, span)
        if pinned is not None and expected == observed == pinned:
            valid_spans.append(span)
    return {**evidence, "continuity_spans": valid_spans}


def _pinned_phone_span_refs(
    history: sqlite3.Connection,
    evidence: Mapping[str, Any],
    span: Mapping[str, Any],
) -> set[tuple[Any, ...]] | None:
    phone_value = span["identifier"]
    phone = phone_value if isinstance(phone_value, Identifier) else Identifier(**phone_value)
    start_ms, end_ms = int(span["start_ms"]), int(span["end_ms"])
    result: set[tuple[Any, ...]] = set()
    rows = history.execute(
        "SELECT c.copy_id,c.source_id,c.source_hash,c.locator_json,c.provenance_class,"
        "c.source_authority,c.channel,c.account,c.chat_id,c.disposition,"
        "d.normalized_json,d.semantic_direction,d.semantic_kind,d.retention_status,d.denied,"
        "p.channel AS proof_channel,p.chat_id AS proof_chat_id,p.occurred_ms AS proof_occurred_ms,"
        "p.eligible,p.revoked_at_ms,p.denial_reason "
        "FROM history_event_copies c JOIN history_event_details d ON d.event_id=c.event_id "
        "AND d.revision=c.revision LEFT JOIN history_source_proofs p ON p.event_id=c.event_id "
        "AND p.revision=c.revision AND p.source_id=c.source_id AND p.locator_json=c.locator_json "
        "WHERE lower(c.channel)=lower(?) AND c.disposition='logical_copy' "
        "AND d.semantic_direction='in' AND d.semantic_kind='message' "
        "AND d.retention_status='retained' AND d.denied=0",
        (phone.channel,),
    )
    for row in rows:
        event = json.loads(row["normalized_json"])
        at_ms = event.get("occurred_ms")
        if not isinstance(at_ms, int) or isinstance(at_ms, bool) or not start_ms <= at_ms < end_ms:
            continue
        account = row["account"] or event.get("account")
        if not account:
            account, _, _ = account_for_event(
                evidence, channel=str(row["channel"]), at_ms=at_ms
            )
        account_is_known = isinstance(account, str) and bool(account)
        parse_account = account if account_is_known else phone.namespace
        parsed, _ = _observation_identifiers({
            "channel": row["channel"],
            "account": parse_account,
            "sender_raw": event.get("sender_raw"),
            "sender_id_raw": event.get("sender_id_raw"),
            "participant_jid_raw": event.get("participant_jid_raw"),
        })
        phones = [item for item in parsed if item.get("kind") == "phone_jid"]
        if not any(_evidence_identifier_matches(item, phone) for item in phones):
            continue
        if not account_is_known:
            return None
        if (
            row["eligible"] != 1
            or row["revoked_at_ms"] is not None
            or row["denial_reason"] not in (None, "")
            or row["proof_occurred_ms"] not in (None, at_ms)
            or row["provenance_class"] != "native"
            or row["source_authority"] not in _NATIVE_SOURCE_AUTHORITIES
            or not _has_native_locator({
                "source_hash": row["source_hash"],
                "locator": _json_object(row["locator_json"]),
                "provenance_class": row["provenance_class"],
            })
        ):
            continue
        if (
            event.get("time_certainty") not in {"native", "provider_timestamp"}
            or row["proof_channel"] is None
            or str(row["proof_channel"]).casefold() != str(row["channel"]).casefold()
            or not isinstance(row["chat_id"], str)
            or row["proof_chat_id"] != row["chat_id"]
            or event.get("channel") is not None
            and str(event["channel"]).casefold() != str(row["channel"]).casefold()
            or event.get("account") is not None
            and str(event["account"]).casefold() != str(account).casefold()
        ):
            return None
        lids = [item for item in parsed if item.get("kind") == "lid"]
        if len(lids) != 1 or not _evidence_identifier_matches(lids[0], span.get("lid_identifier")):
            return None
        names = event.get("name_observations")
        name_rows = [
            item for item in names if isinstance(item, Mapping)
            and item.get("raw_identifier") == phone.value
            and item.get("occurred_ms") == at_ms
            and item.get("observed_ms") == at_ms
        ] if isinstance(names, list) else []
        if not name_rows or any(
            item.get("name") != span.get("pair_name")
            or item.get("time_certainty") != event.get("time_certainty")
            or item.get("provenance_class") != "native"
            for item in name_rows
        ):
            return None
        locator = {
            "table": "history_event_copies",
            "copy_id": int(row["copy_id"]),
            "source_id": str(row["source_id"]),
        }
        result.add(_reference_key({
            "input": "history",
            "source_id": str(row["source_id"]),
            "sha256": evidence["input_hashes"]["history"],
            "locator": locator,
            "time_ms": at_ms,
        }))
    return result or None


def _evidence_identifier_matches(value: Any, expected: Any) -> bool:
    try:
        identifier = value if isinstance(value, Identifier) else Identifier(**value)
        other = expected if isinstance(expected, Identifier) else Identifier(**expected)
    except (TypeError, ValueError):
        return False
    return identifier.full_key == other.full_key


def _history_copy_for_ref(
    connection: sqlite3.Connection,
    ref: Mapping[str, Any],
    *,
    expected_channel: str | None = None,
    require_proof_time: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    locator = ref["locator"]
    if (ref.get("input") != "history" or not isinstance(locator, Mapping)
            or locator.get("table") != "history_event_copies"):
        raise IdentityAuditError("unsupported history evidence locator")
    row = connection.execute(
        "SELECT c.copy_id,c.event_id,c.revision,c.source_id,c.source_hash,c.locator_json,"
        "c.provenance_class,c.source_authority,c.channel,c.account,c.chat_id,c.disposition,"
        "d.normalized_json,p.occurred_ms AS proof_occurred_ms,p.eligible,p.revoked_at_ms,p.denial_reason "
        "FROM history_event_copies c JOIN history_event_details d ON d.event_id=c.event_id "
        "AND d.revision=c.revision LEFT JOIN history_source_proofs p ON p.event_id=c.event_id "
        "AND p.revision=c.revision AND p.source_id=c.source_id AND p.locator_json=c.locator_json "
        "WHERE c.copy_id=?", (locator.get("copy_id"),)
    ).fetchone()
    if (row is None or row["source_id"] != ref.get("source_id")
            or locator.get("source_id") != ref.get("source_id")
            or (expected_channel is not None and str(row["channel"] or "").casefold() != expected_channel.casefold())):
        raise IdentityAuditError("history evidence locator mismatch")
    event = json.loads(row["normalized_json"])
    event_time = event.get("occurred_ms")
    if (not _positive_timestamp(event_time) or ref.get("time_ms") != event_time
            or (require_proof_time and row["proof_occurred_ms"] is not None
                and row["proof_occurred_ms"] != event_time)):
        raise IdentityAuditError("history evidence time mismatch")
    return dict(row), event


def _binding_for_ref(
    connection: sqlite3.Connection, ref: Mapping[str, Any], *, expected_channel: str
) -> dict[str, Any]:
    locator = ref["locator"]
    if (ref.get("input") != "knowledge" or not isinstance(locator, Mapping)
            or locator.get("table") != "knowledge_identifier_bindings"):
        raise IdentityAuditError("unsupported knowledge evidence locator")
    row = connection.execute(
        "SELECT binding_id,channel,namespace,kind,value,person_id,status,valid_from_ms,valid_until_ms,"
        "mapping_verified,evidence_ref FROM knowledge_identifier_bindings WHERE binding_id=?",
        (locator.get("binding_id"),),
    ).fetchone()
    if (row is None or row["binding_id"] != ref.get("source_id")
            or locator.get("binding_id") != ref.get("source_id")
            or str(row["channel"]).casefold() != expected_channel.casefold()):
        raise IdentityAuditError("knowledge evidence locator mismatch")
    return dict(row)


def _validate_config_ref(
    ref: Mapping[str, Any], source: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    locator = ref.get("locator")
    if ref.get("input") != "config" or not isinstance(locator, Mapping) or locator.get("key") != "accounts":
        raise IdentityAuditError("unsupported config evidence locator")
    path = Path(str(locator.get("path") or ""))
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise IdentityAuditError("config evidence artifact is unavailable")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.get("sha256") or digest != evidence["input_hashes"].get("config"):
        raise IdentityAuditError("config evidence hash mismatch")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise IdentityAuditError("config evidence is malformed") from exc
    if (not isinstance(parsed, Mapping) or parsed.get("reviewed") is not True
            or parsed.get("coverage_complete") is not True
            or parsed.get("covered_period") != evidence.get("covered_period")):
        raise IdentityAuditError("config evidence lacks whole-period review")
    if _account_set(parsed.get("accounts", [])) != _account_set(source.get("accounts", [])):
        raise IdentityAuditError("config artifact account set mismatch")


def _config_accounts(source: Mapping[str, Any], inventory: Mapping[str, Any], evidence: Mapping[str, Any]) -> set[str]:
    refs = source["source_refs"]
    if len(refs) != 1:
        raise IdentityAuditError("config inventory requires one frozen artifact")
    ref = refs[0]
    locator = ref["locator"]
    parsed = json.loads(Path(locator["path"]).read_bytes())
    if parsed.get("channel", "").casefold() != inventory["channel"].casefold():
        raise IdentityAuditError("config artifact channel mismatch")
    return _account_set(parsed.get("accounts", []))


def _history_inventory_rows(connection: sqlite3.Connection, channel: str, start: int, end: int) -> list[dict[str, Any]]:
    result = []
    for row in connection.execute(
        "SELECT c.copy_id,c.source_id,c.channel,c.account,d.normalized_json FROM history_event_copies c "
        "JOIN history_event_details d ON d.event_id=c.event_id AND d.revision=c.revision "
        "WHERE lower(c.channel)=lower(?) AND c.disposition='logical_copy'", (channel,)
    ):
        event = json.loads(row["normalized_json"])
        if isinstance(event.get("occurred_ms"), int) and start <= event["occurred_ms"] < end:
            result.append(dict(row))
    return result


def _binding_inventory_rows(connection: sqlite3.Connection, channel: str, start: int, end: int) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        "SELECT binding_id,channel,namespace,status,valid_from_ms,valid_until_ms "
        "FROM knowledge_identifier_bindings WHERE lower(channel)=lower(?) AND status IN ('active','ended')",
        (channel,),
    )]
    return [row for row in rows
            if (int(row["valid_from_ms"] or 0) <= 0 or int(row["valid_from_ms"]) < end)
            and (int(row["valid_until_ms"] or 0) <= 0 or int(row["valid_until_ms"]) > start)]


def _validate_observation_source(
    observation: Mapping[str, Any], row: Mapping[str, Any], event: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    ref = observation["source_ref"]
    identifier = observation["identifier"]
    if not isinstance(identifier, Identifier):
        identifier = Identifier(**identifier)
    source_locator = _json_object(row["locator_json"])
    if (ref.get("source_hash") != row["source_hash"]
            or ref.get("source_locator") != source_locator
            or ref.get("provenance_class") != row["provenance_class"]
            or ref.get("source_authority") != row["source_authority"]
            or row["provenance_class"] != "native"
            or row["source_authority"] not in _NATIVE_SOURCE_AUTHORITIES
            or row["eligible"] != 1 or row["revoked_at_ms"] is not None or row["denial_reason"] not in (None, "")):
        raise IdentityAuditError("identifier evidence source is not native and eligible")
    inventory_account, reason, _ = account_for_event(
        evidence, channel=str(event.get("channel") or row["channel"]), at_ms=int(observation["observed_ms"])
    )
    account = event.get("account") or row.get("account") or inventory_account
    if observation.get("time_certainty") != event.get("time_certainty"):
        raise IdentityAuditError("identifier observation time certainty mismatch")
    if (str(event.get("channel") or "").casefold() != identifier.channel.casefold()
            or not isinstance(account, str) or account.casefold() != str(identifier.namespace).casefold()
            or observation["observed_ms"] != event.get("occurred_ms")):
        raise IdentityAuditError("typed identifier does not match its source scope or time")
    parsed, _ = _observation_identifiers({
        "channel": event.get("channel"), "account": account,
        "sender_raw": event.get("sender_raw"), "sender_id_raw": event.get("sender_id_raw"),
        "participant_jid_raw": event.get("participant_jid_raw"),
    })
    if not any(tuple(item.get(key) for key in ("channel", "kind", "namespace", "value")) == identifier.full_key for item in parsed):
        raise IdentityAuditError("typed identifier is absent from the pinned event")
    paired_lid = observation.get("paired_lid")
    if paired_lid is not None:
        if not isinstance(paired_lid, Identifier):
            paired_lid = Identifier(**paired_lid)
        if not any(tuple(item.get(key) for key in ("channel", "kind", "namespace", "value")) == paired_lid.full_key for item in parsed):
            raise IdentityAuditError("paired LID is absent from the pinned event")
    if observation.get("name") is not None:
        names = event.get("name_observations")
        found = isinstance(names, list) and any(
            isinstance(item, Mapping) and item.get("name") == observation.get("name")
            and item.get("raw_identifier") == identifier.value
            and item.get("occurred_ms") == event.get("occurred_ms")
            and item.get("observed_ms") == observation.get("observed_ms")
            and item.get("time_certainty") == observation.get("time_certainty")
            and item.get("provenance_class") == observation.get("provenance_class") == "native"
            for item in names
        )
        if not found:
            raise IdentityAuditError("name is absent from the pinned source event")


def _reference_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    return (ref.get("input"), ref.get("source_id"), ref.get("sha256"), str(ref.get("locator")), ref.get("time_ms"))


def _account_set(values: Any) -> set[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise IdentityAuditError("malformed account set")
    return {str(value).casefold() for value in values if isinstance(value, str) and value.strip()}


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
        "p.occurred_ms AS proof_occurred_ms, p.eligible, p.revoked_at_ms, p.denial_reason, p.audience_status "
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
    evidence: Mapping[str, Any] | None = None,
    inferred_account: str | None = None,
    account_refs: list[dict[str, Any]] | None = None,
    account_reason: str | None = None,
    source_unproven_diagnostics: Counter[str] | None = None,
) -> tuple[str, str | None, str | None]:
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
                "attribution_basis": None,
                "resolution_proposal": None,
                "basis_components": [],
            }
        )
        if inferred_account is not None:
            detail["inferred_scope"] = {
                "channel": event.get("channel"),
                "account": inferred_account,
                "basis": "single_account_inferred",
                "evidence_refs": account_refs or [],
            }
    explicit_account = event.get("account")
    explicit_channel = event.get("channel")
    event_chat = event.get("chat_id")
    native_time = _has_native_time(event)
    valid_sources: list[dict[str, Any]] = []
    scope_mismatch = False
    time_mismatch = False
    rejected_for_proof = False
    rejected_for_source_integrity = False
    missing_account = False
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
        ):
            rejected_for_proof = True
            continue
        if (
            row.get("source_authority") not in _NATIVE_SOURCE_AUTHORITIES
            or not _has_native_locator(
                {
                    "source_hash": row.get("source_hash"),
                    "locator": locator,
                    "provenance_class": row.get("provenance_class"),
                }
            )
        ):
            rejected_for_source_integrity = True
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
            not isinstance(copy_channel, str)
            or not isinstance(proof_channel, str)
            or not isinstance(event_chat, str)
            or not isinstance(copy_chat, str)
            or not isinstance(proof_chat, str)
            or copy_chat != event_chat
            or proof_chat != event_chat
        ):
            continue
        working_account = copy_account if isinstance(copy_account, str) else inferred_account
        if not isinstance(working_account, str):
            missing_account = True
            continue
        if isinstance(explicit_account, str) and working_account.casefold() != explicit_account.casefold():
            scope_mismatch = True
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
        valid_sources.append({**row, "_working_account": working_account})
    if time_mismatch:
        return "conflict", "source_time_conflict", None
    if scope_mismatch:
        return "unresolved", "account_mismatch", None
    if not valid_sources:
        if source_unproven_diagnostics is not None:
            diagnostic = (
                "no_source_copy" if not source_rows else
                "source_proof_missing_or_denied" if rejected_for_proof else
                "unsupported_source_authority_or_locator" if rejected_for_source_integrity else
                (account_reason or "missing_account") if missing_account else
                "source_scope_or_time_missing"
            )
            source_unproven_diagnostics[diagnostic] += 1
        return "unresolved", "source_unproven", None
    account_inference_required = inferred_account is not None and not any(
        isinstance(row.get("copy_account"), str) and row["copy_account"].strip()
        for row in valid_sources
    )
    if detail is not None and not account_inference_required:
        detail.pop("inferred_scope", None)
    scopes = {
        (str(row["copy_channel"]).casefold(), str(row["_working_account"]).casefold())
        for row in valid_sources
    }
    if len(scopes) != 1:
        return "conflict", "source_scope_conflict", None
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
    pair_resolution: dict[str, Any] | None = None
    if evidence is not None and len(identifier_rows) == 2:
        phones = [row for row in identifier_rows if row.get("kind") == "phone_jid"]
        lids = [row for row in identifier_rows if row.get("kind") == "lid"]
        occurred_ms = event.get("occurred_ms")
        paired_spans = []
        if len(phones) == len(lids) == 1 and _positive_timestamp(occurred_ms):
            for span in evidence.get("continuity_spans", []):
                if (
                    _evidence_identifier_matches(span.get("identifier"), phones[0])
                    and _evidence_identifier_matches(span.get("lid_identifier"), lids[0])
                    and int(span["start_ms"]) <= occurred_ms < int(span["end_ms"])
                ):
                    paired_spans.append(span)
        if len(paired_spans) != 1:
            return "unresolved", "sender_identifier_missing", None
        pair_span = paired_spans[0]
        paired_observations = [
            row for row in evidence.get("observations", [])
            if _evidence_identifier_matches(row.get("identifier"), phones[0])
            and _evidence_identifier_matches(row.get("paired_lid"), lids[0])
            and row.get("observed_ms") == occurred_ms
            and row.get("name") == pair_span.get("pair_name")
        ]
        if not paired_observations:
            return "unresolved", "sender_identifier_missing", None
        pair_identifiers = [
            Identifier(channel=str(row["channel"]), kind=str(row["kind"]),
                       namespace=event_account, value=str(row["value"]))
            for row in (phones[0], lids[0])
        ]
        pair_results = [
            resolve_attribution_binding(
                pair_identifier, int(occurred_ms), bindings=bindings,
                canonical_ids=canonical_ids,
                observations=evidence.get("observations", []),
                continuity_spans=evidence.get("continuity_spans", []),
            )
            for pair_identifier in pair_identifiers
        ]
        span_person = str(pair_span.get("canonical_person_id") or "")
        span_person = canonical_ids.get(span_person, span_person)
        resolved_people = {
            str(result["canonical_person_id"])
            for result in pair_results if result.get("canonical_person_id")
        }
        if any(
            result.get("reason") in {"binding_conflict", "binding_start_conflict"}
            for result in pair_results
        ) or any(person != span_person for person in resolved_people):
            return "conflict", "binding_conflict", None
        if any(
            not result.get("canonical_person_id") and result.get("reason") != "no_binding"
            for result in pair_results
        ) or span_person not in resolved_people:
            failure = next(
                (str(result.get("reason")) for result in pair_results if result.get("reason")),
                "sender_identifier_missing",
            )
            return "unresolved", failure, None
        pair_refs: list[dict[str, Any]] = []
        for result in pair_results:
            for ref in result.get("evidence_refs", []):
                if ref not in pair_refs:
                    pair_refs.append(ref)
        if all(
            result.get("status") == "resolved" and result.get("basis") == "proven"
            and result.get("canonical_person_id") == span_person
            for result in pair_results
        ):
            pair_resolution = {
                **pair_results[0],
                "canonical_person_id": span_person,
                "evidence_refs": pair_refs,
            }
        else:
            pair_resolution = {
                "status": "candidate",
                "canonical_person_id": span_person,
                "binding_id": pair_results[0].get("binding_id"),
                "basis": "observed_continuity",
                "evidence_refs": pair_refs,
                "reason": None,
            }
        identifier_rows = phones
    if len(identifier_rows) != 1:
        return "unresolved", parse_reason or "sender_identifier_missing", None
    identifier = identifier_rows[0]
    identifier_key = _binding_key(identifier)
    matching = [row for row in bindings if _binding_key(row) == identifier_key]
    if detail is not None:
        detail["binding_evidence"] = [
            _binding_evidence(row, canonical_ids) for row in matching
        ]
    if not matching:
        return "unresolved", "no_binding", None
    occurred_ms = event.get("occurred_ms")
    exact_time = native_time and all(
        _positive_timestamp(row.get("proof_occurred_ms"))
        and row["proof_occurred_ms"] == occurred_ms
        for row in valid_sources
    )
    if not exact_time:
        return "candidate", "event_time_not_native", None
    if evidence is not None:
        resolution = pair_resolution or resolve_attribution_binding(
            Identifier(
                channel=identifier["channel"],
                kind=identifier["kind"],
                namespace=event_account,
                value=identifier["value"],
            ),
            int(occurred_ms),
            bindings=bindings,
            canonical_ids=canonical_ids,
            observations=evidence.get("observations", []),
            continuity_spans=evidence.get("continuity_spans", []),
        )
        person = resolution.get("canonical_person_id")
        basis = resolution.get("basis")
        if detail is not None and person and basis == "observed_continuity":
            detail["resolution_proposal"] = {
                "canonical_person_id": person,
                "evidence_refs": resolution.get("evidence_refs", []),
                "reason": "observed_continuity",
            }
        if resolution.get("status") == "resolved" and basis == "proven":
            binding = next((row for row in matching if row.get("binding_id") == resolution.get("binding_id")), None)
            if binding is None or not bool(int(binding.get("mapping_verified") or 0)):
                return "candidate", "binding_not_verified", None
            if not str(binding.get("evidence_ref") or "").strip():
                return "candidate", "binding_evidence_missing", None
            if account_inference_required:
                if detail is not None:
                    detail["resolution_proposal"] = {
                        "canonical_person_id": person,
                        "evidence_refs": resolution.get("evidence_refs", []),
                        "reason": "account_scope_inferred",
                    }
                    detail["attribution_basis"] = "single_account_inferred"
                    detail["basis_components"] = ["single_account_inferred", "proven"]
                return "candidate", "account_scope_inferred", "single_account_inferred"
            if detail is not None:
                detail["resolved_person_id"] = person
                detail["attribution_basis"] = "proven"
                detail["basis_components"] = ["proven"]
            return "confirmed", None, "proven"
        if basis == "observed_continuity" and person:
            final_basis = "single_account_inferred" if account_inference_required else "observed_continuity"
            if detail is not None:
                detail["attribution_basis"] = final_basis
                detail["basis_components"] = (
                    ["single_account_inferred", "observed_continuity"]
                    if account_inference_required else ["observed_continuity"]
                )
            return "candidate", "observed_continuity", final_basis
        failure = str(resolution.get("reason") or "unresolved")
        if failure in {"binding_conflict", "binding_start_conflict"}:
            return "conflict", failure, None
        if resolution.get("status") == "candidate":
            return "candidate", failure, None
        return "unresolved", failure, None
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
            return "candidate", "binding_not_verified", None
        if not str(binding.get("evidence_ref") or "").strip():
            return "candidate", "binding_evidence_missing", None
        if detail is not None:
            detail["resolved_person_id"] = resolution["canonical_person_id"]
            detail["attribution_basis"] = "proven"
            detail["basis_components"] = ["proven"]
        return "confirmed", None, "proven"
    reason = str(resolution.get("reason") or "unresolved")
    if reason in {"binding_conflict", "binding_start_conflict"}:
        return "conflict", reason, None
    if reason in {"binding_start_unknown", "binding_not_authoritative"}:
        return "candidate", reason, None
    return "unresolved", reason, None


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
        "audience_status": row.get("audience_status"),
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
