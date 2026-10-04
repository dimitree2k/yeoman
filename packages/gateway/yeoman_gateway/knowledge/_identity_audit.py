"""Read-only reconciliation of Knowledge and legacy person identity stores."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yeoman_gateway.knowledge.models import normalize_alias_value, normalize_identifier_value

_TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "contacts": ("id",),
    "contact_identifiers": ("channel", "identifier"),
    "contact_aliases": ("contact_id", "alias", "source"),
    "contact_fields": ("contact_id", "kind", "value"),
}
_IDENTITY_TABLES = (
    "contacts",
    "contact_identifiers",
    "contact_aliases",
    "contact_fields",
    "knowledge_identifier_bindings",
    "knowledge_identity_redirects",
)
_NON_PERSON_SUFFIXES = {
    "@newsletter": "non_person_newsletter",
    "@broadcast": "non_person_address",
    "@g.us": "non_person_address",
}


class IdentityAuditError(ValueError):
    """An input database or observation manifest could not be safely audited."""


def audit_person_stores(
    knowledge_db: Path,
    legacy_db: Path | None = None,
    *,
    observations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Compare identity stores without initializing or changing either input.

    ``observations`` accepts metadata records with direct channel/account/time fields and
    typed ``raw_identifier`` or sender-identifier fields. Missing fields stay missing;
    names and policy principals are never used to create identifier matches.
    """
    knowledge_connection, knowledge_read = _open_snapshot("knowledge", knowledge_db)
    legacy_connection: sqlite3.Connection | None = None
    legacy_read: dict[str, str] | None = None
    try:
        if legacy_db is not None:
            legacy_connection, legacy_read = _open_snapshot("legacy", legacy_db)
        knowledge = _load_store(knowledge_connection)
        legacy = _load_store(legacy_connection) if legacy_connection is not None else None
        _assert_snapshot_stable("knowledge", knowledge_read)
        if legacy_read is not None:
            _assert_snapshot_stable("legacy", legacy_read)
        reconciliation = _reconcile_stores(knowledge, legacy)
        redirect_result = _audit_redirects(
            knowledge["knowledge_identity_redirects"],
            {str(row.get("id")) for row in knowledge["contacts"]["rows"]},
        )
        observation_result = _audit_observations(
            observations,
            knowledge["knowledge_identifier_bindings"]["rows"],
            redirect_result["canonical_ids"],
        )
        binding_result = _audit_bindings(
            knowledge["knowledge_identifier_bindings"], redirect_result["canonical_ids"]
        )
        name_result = _audit_names(
            knowledge["contacts"]["rows"], knowledge["contact_aliases"]["rows"],
            redirect_result["canonical_ids"],
        )
        owner_result = _audit_owners(
            knowledge["contacts"]["rows"],
            knowledge["knowledge_identifier_bindings"]["rows"],
            redirect_result["canonical_ids"],
        )
        identifier_result = {
            "knowledge": _audit_contact_identifiers(
                knowledge["contact_identifiers"]["rows"],
                knowledge["knowledge_identifier_bindings"]["rows"],
                knowledge["contacts"]["rows"],
            ),
            "legacy": (
                _audit_contact_identifiers(
                    legacy["contact_identifiers"]["rows"],
                    knowledge["knowledge_identifier_bindings"]["rows"],
                    knowledge["contacts"]["rows"],
                )
                if legacy is not None
                else None
            ),
        }

        aggregate = {
            "schema_version": 1,
            "read_snapshots": {
                "knowledge": {"snapshot_read_at_utc": knowledge_read["snapshot_read_at_utc"]},
                "legacy": (
                    {"snapshot_read_at_utc": legacy_read["snapshot_read_at_utc"]}
                    if legacy_read is not None
                    else None
                ),
                "atomic_across_databases": False if legacy is not None else None,
            },
            "databases": {
                "knowledge": _store_summary(knowledge),
                "legacy": _store_summary(legacy) if legacy is not None else None,
            },
            "field_reconciliation": reconciliation["aggregate"],
            "identifier_without_binding": {
                key: None if value is None else {"count": value["count"], "categories": value["categories"]}
                for key, value in identifier_result.items()
            },
            "bindings": binding_result["aggregate"],
            "redirects": redirect_result["aggregate"],
            "people": {**name_result["aggregate"], **owner_result["aggregate"]},
            "observations": observation_result["aggregate"],
            "evidence_candidates": {"count": len(observation_result["details"]["evidence_candidates"])},
        }
        details = {
            "field_differences": reconciliation["details"]["field_differences"],
            "unmatched_rows": reconciliation["details"]["unmatched_rows"],
            "ambiguous_row_keys": reconciliation["details"]["ambiguous_row_keys"],
            "identifier_without_binding": {
                key: None if value is None else value["details"]
                for key, value in identifier_result.items()
            },
            "binding_conflicts": binding_result["details"]["conflicts"],
            "reused_identifiers": binding_result["details"]["reused_identifiers"],
            "binding_assignments": binding_result["details"]["assignments"],
            "redirect_conflicts": redirect_result["details"]["conflicts"],
            "name_conflicts": name_result["details"]["conflicts"],
            "name_variants": name_result["details"]["variants"],
            "owner_records": owner_result["details"],
            **observation_result["details"],
        }
        return {"schema_version": 1, "aggregate": aggregate, "details": details}
    finally:
        knowledge_connection.close()
        if legacy_connection is not None:
            legacy_connection.close()


def _open_snapshot(label: str, path: Path) -> tuple[sqlite3.Connection, dict[str, Any]]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise IdentityAuditError(f"{label} database is not a readable file")
    try:
        target = target.resolve(strict=True)
        if _sqlite_sidecars(target):
            raise IdentityAuditError(f"cannot safely inspect {label} database with SQLite sidecars")
        before = _file_signature(target)
        connection = sqlite3.connect(
            f"{target.as_uri()}?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
        after = _file_signature(target)
        if _sqlite_sidecars(target) or before != after:
            connection.close()
            raise IdentityAuditError(f"{label} database changed while being inspected")
    except IdentityAuditError:
        if "connection" in locals():
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if "connection" in locals():
            connection.close()
        raise IdentityAuditError(f"cannot open {label} database read-only") from exc
    return connection, {
        "snapshot_read_at_utc": _utc_now(),
        "database_path": target,
        "file_signature": before,
    }


def _file_signature(database: Path) -> tuple[int, int, int, int, int]:
    stat = database.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _assert_snapshot_stable(label: str, snapshot: dict[str, Any]) -> None:
    database = snapshot["database_path"]
    try:
        if _sqlite_sidecars(database) or _file_signature(database) != snapshot["file_signature"]:
            raise IdentityAuditError(f"{label} database changed while being inspected")
    except OSError as exc:
        raise IdentityAuditError(f"cannot verify {label} database stability") from exc


def _sqlite_sidecars(database: Path) -> list[Path]:
    sidecars = [Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    try:
        sidecars.extend(
            item
            for item in database.parent.iterdir()
            if item.name.startswith(f"{database.name}-mj")
        )
    except OSError as exc:
        raise IdentityAuditError("cannot safely inspect SQLite sidecars") from exc
    return [item for item in sidecars if item.exists()]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_store(connection: sqlite3.Connection | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for table in _IDENTITY_TABLES:
        if connection is None:
            result[table] = {"columns": [], "rows": [], "exists": False}
            continue
        exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None
        if not exists:
            result[table] = {"columns": [], "rows": [], "exists": False}
            continue
        columns = [str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
        result[table] = {"columns": columns, "rows": rows, "exists": True}
    return result


def _store_summary(store: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "tables": {
            name: {
                "present": table["exists"],
                "rows": len(table["rows"]),
                "columns": table["columns"],
            }
            for name, table in store.items()
        }
    }


def _reconcile_stores(
    knowledge: dict[str, dict[str, Any]], legacy: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    field_differences: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []
    ambiguous_row_keys: list[dict[str, Any]] = []
    for table, key_columns in _TABLE_KEYS.items():
        current = knowledge[table]
        old = legacy[table] if legacy is not None else {"columns": [], "rows": [], "exists": False}
        common_columns = sorted(set(current["columns"]) & set(old["columns"]))
        missing_keys = [name for name in key_columns if name not in common_columns]
        entry: dict[str, Any] = {
            "knowledge_rows": len(current["rows"]),
            "legacy_rows": len(old["rows"]) if legacy is not None else None,
            "knowledge_only_columns": sorted(set(current["columns"]) - set(old["columns"])) if legacy else [],
            "legacy_only_columns": sorted(set(old["columns"]) - set(current["columns"])) if legacy else [],
            "common_columns": common_columns,
            "identical_rows": 0,
            "changed_rows": 0,
            "legacy_only_rows": None if legacy is None else 0,
            "knowledge_only_rows": None if legacy is None else 0,
            "ambiguous_key_groups": 0,
            "comparison": "legacy_store_not_supplied" if legacy is None else "compared_by_typed_row_key",
        }
        if legacy is not None and not missing_keys:
            old_groups = _group_rows(old["rows"], key_columns)
            current_groups = _group_rows(current["rows"], key_columns)
            old_keys, current_keys = set(old_groups), set(current_groups)
            entry["legacy_only_rows"] = sum(len(old_groups[key]) for key in old_keys - current_keys)
            entry["knowledge_only_rows"] = sum(
                len(current_groups[key]) for key in current_keys - old_keys
            )
            for key in sorted(old_keys & current_keys, key=repr):
                old_rows, current_rows = old_groups[key], current_groups[key]
                if len(old_rows) != 1 or len(current_rows) != 1:
                    entry["ambiguous_key_groups"] += 1
                    ambiguous_row_keys.append(
                        {
                            "table": table,
                            "key": _key_detail(key_columns, key),
                            "legacy_rows": len(old_rows),
                            "knowledge_rows": len(current_rows),
                        }
                    )
                    continue
                old_row, current_row = old_rows[0], current_rows[0]
                changed_fields = [
                    column for column in common_columns if old_row.get(column) != current_row.get(column)
                ]
                if changed_fields:
                    entry["changed_rows"] += 1
                    for column in changed_fields:
                        field_differences.append(
                            {
                                "table": table,
                                "key": _key_detail(key_columns, key),
                                "field": column,
                                "legacy": _json_value(old_row.get(column)),
                                "knowledge": _json_value(current_row.get(column)),
                            }
                        )
                else:
                    entry["identical_rows"] += 1
            for key in old_keys - current_keys:
                for row in old_groups[key]:
                    unmatched_rows.append({"store": "legacy", "table": table, "row": _json_row(row)})
            for key in current_keys - old_keys:
                for row in current_groups[key]:
                    unmatched_rows.append({"store": "knowledge", "table": table, "row": _json_row(row)})
        elif legacy is not None:
            entry["comparison"] = "required_key_column_missing"
        aggregate[table] = entry
    return {
        "aggregate": aggregate,
        "details": {
            "field_differences": field_differences,
            "unmatched_rows": unmatched_rows,
            "ambiguous_row_keys": ambiguous_row_keys,
        },
    }


def _group_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key) for key in keys)].append(row)
    return groups


def _key_detail(columns: tuple[str, ...], values: tuple[Any, ...]) -> dict[str, Any]:
    return {column: _json_value(value) for column, value in zip(columns, values, strict=True)}


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(value) for key, value in row.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _audit_contact_identifiers(
    identifiers: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    people: list[dict[str, Any]],
) -> dict[str, Any]:
    people_by_id = {str(row.get("id")): row for row in people if row.get("id") is not None}
    categories: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    for row in identifiers:
        channel = str(row.get("channel") or "").lower()
        kind = str(row.get("kind") or "").lower()
        value = str(row.get("identifier") or "")
        person_id = str(row.get("contact_id") or "")
        category = _legacy_identifier_category(channel, kind, value, person_id, bindings, people_by_id)
        categories[category] += 1
        details.append(
            {
                "channel": channel,
                "kind": kind,
                "value": value,
                "person_id": person_id,
                "category": category,
                "matching_bindings": [
                    _json_row(binding)
                    for binding in bindings
                    if str(binding.get("channel") or "").lower() == channel
                    and str(binding.get("value") or "") == value
                ],
            }
        )
    return {"count": len(identifiers), "categories": dict(sorted(categories.items())), "details": details}


def _legacy_identifier_category(
    channel: str,
    kind: str,
    value: str,
    person_id: str,
    bindings: list[dict[str, Any]],
    people: dict[str, dict[str, Any]],
) -> str:
    lower_value = value.lower()
    for suffix, category in _NON_PERSON_SUFFIXES.items():
        if lower_value.endswith(suffix):
            return category
    matches = [
        row for row in bindings
        if str(row.get("channel") or "").lower() == channel
        and str(row.get("kind") or "").lower() == kind
        and str(row.get("value") or "") == value
    ]
    if not matches:
        same_value = [
            row for row in bindings
            if str(row.get("channel") or "").lower() == channel
            and str(row.get("value") or "") == value
        ]
        if same_value:
            return "binding_kind_mismatch"
        if bool(int(people.get(person_id, {}).get("is_owner") or 0)):
            return "owner_flagged_person_without_binding"
        return "no_binding_explanation_unknown"
    active = [row for row in matches if str(row.get("status") or "") == "active"]
    if len(active) > 1:
        namespaces = {row.get("namespace") for row in active}
        return "namespace_ambiguous" if len(namespaces) > 1 else "duplicate_active_binding"
    if active:
        if not active[0].get("namespace"):
            return "binding_namespace_unknown"
        return (
            "binding_matches_person_namespace_unproven"
            if str(active[0].get("person_id")) == person_id
            else "binding_person_conflict_namespace_unproven"
        )
    statuses = {str(row.get("status") or "") for row in matches}
    if "ended" in statuses:
        return "historical_binding_only"
    if "conflict" in statuses:
        return "conflict_binding_only"
    if "withheld" in statuses:
        return "withheld_binding_only"
    return "no_active_binding"


def _audit_bindings(
    table: dict[str, Any], canonical_ids: dict[str, str]
) -> dict[str, Any]:
    rows = table["rows"]
    by_status = Counter(str(row.get("status") or "unknown") for row in rows)
    by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_key[_binding_key(row)].append(row)
    active_conflicts: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    ambiguous_reuse: list[dict[str, Any]] = []
    assignments: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("status") or "") != "active":
            continue
        person_id = str(row.get("person_id") or "")
        if str(row.get("channel") or "").lower() not in {"whatsapp", "telegram"}:
            continue
        assignments[(canonical_ids.get(person_id, person_id), str(row.get("channel")), str(row.get("namespace") or ""))].append(row)
    for key, group in by_key.items():
        active = [row for row in group if str(row.get("status") or "") == "active"]
        if len(active) > 1:
            active_conflicts.append({"key": _binding_key_detail(key), "rows": [_json_row(r) for r in active]})
        eligible = [
            row for row in group
            if str(row.get("status") or "") in {"active", "ended"}
        ]
        distinct_people = {str(row.get("person_id")) for row in eligible}
        if len(eligible) < 2 or len(distinct_people) < 2:
            continue
        periods = [_binding_period(row) for row in eligible]
        if all(period is not None for period in periods) and _periods_are_disjoint(periods):
            reused.append({"key": _binding_key_detail(key), "rows": [_json_row(r) for r in eligible]})
        else:
            ambiguous_reuse.append({"key": _binding_key_detail(key), "rows": [_json_row(r) for r in eligible]})
    assignment_details = []
    paired_people: set[str] = set()
    over_limit_groups = 0
    for (person_id, channel, namespace), group in sorted(assignments.items()):
        kinds: dict[str, set[str]] = defaultdict(set)
        for row in group:
            kinds[str(row.get("kind") or "")].add(str(row.get("value") or ""))
        if channel.lower() == "whatsapp" and kinds.get("lid") and kinds.get("phone_jid"):
            paired_people.add(person_id)
        over_limit = channel.lower() == "whatsapp" and (
            len(kinds.get("lid", set())) > 1 or len(kinds.get("phone_jid", set())) > 1
        )
        over_limit_groups += int(over_limit)
        assignment_details.append(
            {
                "canonical_person_id": person_id,
                "channel": channel,
                "namespace": namespace,
                "bindings": [_json_row(row) for row in group],
                "lid_count": len(kinds.get("lid", set())),
                "phone_jid_count": len(kinds.get("phone_jid", set())),
                "exceeds_whatsapp_one_lid_one_phone_limit": over_limit,
                "meaning": "identifier inventory only; no owner or policy authority inferred",
            }
        )
    return {
        "aggregate": {
            "total": len(rows),
            "by_status": dict(sorted(by_status.items())),
            "unknown_start": sum(int(row.get("valid_from_ms") or 0) == 0 for row in rows),
            "unverified_mapping": sum(not bool(int(row.get("mapping_verified") or 0)) for row in rows),
            "active_conflict_groups": len(active_conflicts),
            "reused_identifier_groups": len(reused),
            "time_ambiguous_reuse_groups": len(ambiguous_reuse),
            "canonical_people_with_whatsapp_lid_and_phone": len(paired_people),
            "canonical_person_account_groups_over_whatsapp_binding_limit": over_limit_groups,
        },
        "details": {
            "conflicts": active_conflicts,
            "reused_identifiers": reused + ambiguous_reuse,
            "assignments": assignment_details,
        },
    }


def _binding_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in ("channel", "kind", "namespace", "value"))


def _binding_key_detail(key: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(("channel", "kind", "namespace", "value"), key, strict=True))


def _binding_period(row: Mapping[str, Any]) -> tuple[int, int | None] | None:
    start = int(row.get("valid_from_ms") or 0)
    if start <= 0:
        return None
    end = int(row.get("valid_until_ms") or 0)
    if str(row.get("status") or "") == "ended" and end <= 0:
        return None
    return (start, end if end > 0 else None)


def _periods_are_disjoint(periods: list[tuple[int, int | None] | None]) -> bool:
    ordered = sorted(periods, key=lambda period: period[0])
    for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:]):
        if previous_end is None or next_start < previous_end:
            return False
    return True


def _audit_redirects(table: dict[str, Any], people: set[str]) -> dict[str, Any]:
    rows = table["rows"]
    active = [row for row in rows if bool(int(row.get("active") or 0))]
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in active:
        by_source[str(row.get("source_id") or "")].append(row)
    multiple = [group for group in by_source.values() if len(group) > 1]
    graph = {source: str(group[0].get("target_id") or "") for source, group in by_source.items() if len(group) == 1}
    cycles: list[list[str]] = []
    for start in graph:
        seen: dict[str, int] = {}
        current = start
        while current in graph:
            if current in seen:
                cycle = list(seen)[seen[current]:]
                signature = tuple(sorted(cycle))
                if not any(tuple(sorted(existing)) == signature for existing in cycles):
                    cycles.append(cycle)
                break
            seen[current] = len(seen)
            current = graph[current]
    canonical_ids: dict[str, str] = {}
    for person_id in people:
        current, seen = person_id, set()
        while current in graph and current not in seen:
            seen.add(current)
            current = graph[current]
        canonical_ids[person_id] = current if current not in seen else person_id
    conflicts = [
        {"source_id": source, "targets": [str(row.get("target_id") or "") for row in group]}
        for source, group in by_source.items() if len(group) > 1
    ]
    conflicts.extend({"cycle": cycle} for cycle in cycles)
    conflicts.extend(
        {
            "source_id": str(row.get("source_id") or ""),
            "target_id": str(row.get("target_id") or ""),
            "reason": "redirect_person_missing",
        }
        for row in active
        if str(row.get("source_id") or "") not in people
        or str(row.get("target_id") or "") not in people
    )
    return {
        "aggregate": {
            "total": len(rows),
            "active": len(active),
            "multiple_active_sources": len(multiple),
            "cycles": len(cycles),
            "orphan_endpoints": sum(
                str(row.get("source_id") or "") not in people
                or str(row.get("target_id") or "") not in people
                for row in active
            ),
        },
        "details": {"conflicts": conflicts},
        "canonical_ids": canonical_ids,
    }


def _audit_names(
    people: list[dict[str, Any]], aliases: list[dict[str, Any]], canonical_ids: dict[str, str]
) -> dict[str, Any]:
    names_by_person: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for person in people:
        person_id = str(person.get("id") or "")
        canonical = canonical_ids.get(person_id, person_id)
        for field in ("display_name", "preferred_name"):
            value = person.get(field)
            if isinstance(value, str) and value.strip():
                normalized = _normalize_name(value)
                if normalized:
                    names_by_person[canonical][normalized].add(value.strip())
    for alias in aliases:
        if str(alias.get("status") or "") == "retired" or bool(int(alias.get("mapping_retracted") or 0)):
            continue
        person_id = str(alias.get("contact_id") or "")
        value = alias.get("alias")
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = _normalize_name(value)
        if normalized:
            names_by_person[canonical_ids.get(person_id, person_id)][normalized].add(value.strip())

    people_by_name: dict[str, set[str]] = defaultdict(set)
    variants: list[dict[str, Any]] = []
    for person_id, name_map in names_by_person.items():
        for normalized, values in name_map.items():
            people_by_name[normalized].add(person_id)
        if len(name_map) > 1:
            variants.append(
                {"person_id": person_id, "names": sorted({value for values in name_map.values() for value in values})}
            )
    conflicts = [
        {
            "normalized_name": normalized,
            "person_ids": sorted(person_ids),
            "reason": "shared_name_is_not_identity_evidence",
        }
        for normalized, person_ids in sorted(people_by_name.items())
        if len(person_ids) > 1
    ]
    return {
        "aggregate": {
            "people_with_name_variants": len(variants),
            "name_conflict_groups": len(conflicts),
            "name_conflict_pairs": sum(
                len(item["person_ids"]) * (len(item["person_ids"]) - 1) // 2 for item in conflicts
            ),
        },
        "details": {"conflicts": conflicts, "variants": variants},
    }


def _normalize_name(value: str) -> str | None:
    try:
        return normalize_alias_value(value)
    except ValueError:
        return None


def _audit_owners(
    people: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    canonical_ids: dict[str, str],
) -> dict[str, Any]:
    flagged = [person for person in people if bool(int(person.get("is_owner") or 0))]
    detail = []
    for person in flagged:
        person_id = str(person.get("id") or "")
        canonical_id = canonical_ids.get(person_id, person_id)
        active = [
            row for row in bindings
            if canonical_ids.get(str(row.get("person_id") or ""), str(row.get("person_id") or ""))
            == canonical_id
            and str(row.get("status") or "") == "active"
        ]
        detail.append(
            {
                "person_id": person_id,
                "canonical_person_id": canonical_id,
                "display_name": person.get("display_name"),
                "active_bindings": [_json_row(row) for row in active],
                "owner_authority_source": "database_flag_only; policy was not read",
            }
        )
    return {
        "aggregate": {
            "owner_flagged_people": len(flagged),
            "owner_flagged_people_with_active_bindings": sum(bool(row["active_bindings"]) for row in detail),
            "multiple_owner_flags": max(0, len(flagged) - 1),
        },
        "details": detail,
    }


def _audit_observations(
    observations: Iterable[dict[str, Any]],
    bindings: list[dict[str, Any]],
    canonical_ids: dict[str, str],
) -> dict[str, Any]:
    records = 0
    name_records = 0
    typed_records = 0
    source_counts: Counter[str] = Counter()
    resolutions: list[dict[str, Any]] = []
    name_values: dict[tuple[Any, ...], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    candidates: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        records += 1
        record_type = _observation_record_type(observation)
        source_counts[record_type] += 1
        event_id = observation.get("event_id")
        revision = observation.get("revision")
        names = _observation_name_values(observation)
        if names:
            name_records += 1
        identifiers, parse_reason = _observation_identifiers(observation)
        if identifiers:
            typed_records += 1
        per_identifier = [
            _resolve_observed_identifier(identifier, observation, bindings, canonical_ids)
            for identifier in identifiers
        ]
        if not identifiers:
            per_identifier = [{"status": "unresolved", "reason": parse_reason or "missing_sender_identifier"}]
        resolved_people = {
            str(item["canonical_person_id"])
            for item in per_identifier
            if item["status"] == "resolved"
        }
        unresolved = [item for item in per_identifier if item["status"] != "resolved"]
        if not unresolved and len(resolved_people) == 1:
            person_id = next(iter(resolved_people))
            resolutions.append(
                {
                    "event_id": event_id,
                    "revision": revision,
                    "record_type": record_type,
                    "status": "resolved",
                    "person_id": person_id,
                    "canonical_person_id": person_id,
                    "original_person_ids": sorted(
                        {str(item["person_id"]) for item in per_identifier if item["status"] == "resolved"}
                    ),
                }
            )
        else:
            reason = (
                "identifiers_resolve_to_multiple_people"
                if len(resolved_people) > 1 and not unresolved
                else "partial_identifier_resolution"
                if resolved_people and unresolved
                else str(unresolved[0]["reason"])
            )
            resolutions.append(
                {
                    "event_id": event_id,
                    "revision": revision,
                    "record_type": record_type,
                    "status": "unresolved",
                    "reason": reason,
                    "identifiers": per_identifier,
                }
            )
        for identifier in identifiers:
            if identifier.get("namespace") is None:
                continue
            for name in names:
                normalized = _normalize_name(name)
                if normalized:
                    name_values[_observed_identifier_key(identifier)][normalized].add(name.strip())
        candidates.extend(
            _observed_pair_candidates(observation, identifiers, bindings, canonical_ids)
        )

    variant_groups = [
        {"identifier": _observed_identifier_key_detail(key), "names": sorted(values)}
        for key, values_by_name in name_values.items()
        if len(values_by_name) > 1
        for values in [sorted({item for group in values_by_name.values() for item in group})]
    ]
    unresolved_by_reason = Counter(
        str(item.get("reason") or "unknown") for item in resolutions if item["status"] == "unresolved"
    )
    resolution_by_type: dict[str, dict[str, int]] = {}
    event_people: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in resolutions:
        counts = resolution_by_type.setdefault(
            str(item["record_type"]), {"resolved": 0, "unresolved": 0}
        )
        counts[str(item["status"])] += 1
        if item.get("event_id") and item["status"] == "resolved":
            event_people[(str(item["event_id"]), str(item.get("revision") or ""))].add(
                str(item.get("person_id") or "")
            )
    unique_events = {
        (str(item.get("event_id") or ""), str(item.get("revision") or ""))
        for item in resolutions
        if item.get("event_id")
    }
    return {
        "aggregate": {
            "records": records,
            "record_types": dict(sorted(source_counts.items())),
            "resolution_by_record_type": dict(sorted(resolution_by_type.items())),
            "resolution_counts_are_records_not_independent_messages": True,
            "name_records": name_records,
            "records_with_typed_identifiers": typed_records,
            "unique_event_revisions": len(unique_events),
            "resolved": sum(item["status"] == "resolved" for item in resolutions),
            "unresolved": sum(item["status"] == "unresolved" for item in resolutions),
            "unresolved_by_reason": dict(sorted(unresolved_by_reason.items())),
            "unassigned_event_revisions": sum(
                not event_people.get(event) for event in unique_events
            ),
            "conflicting_event_revisions": sum(len(people) > 1 for people in event_people.values()),
            "name_variant_groups": len(variant_groups),
        },
        "details": {
            "resolved_observations": [item for item in resolutions if item["status"] == "resolved"],
            "unresolved_observations": [item for item in resolutions if item["status"] == "unresolved"],
            "name_variants": variant_groups,
            "evidence_candidates": candidates,
        },
    }


def _observation_record_type(observation: Mapping[str, Any]) -> str:
    if "missing_copy_fields" in observation:
        return "preserved_copy"
    if "raw_identifier" in observation and "name" in observation and "sender_raw" not in observation:
        return "name_observation"
    if "event_id" in observation:
        return "normalized_primary"
    return "other"


def _observation_name_values(observation: Mapping[str, Any]) -> list[str]:
    name = observation.get("name")
    if isinstance(name, str) and name.strip():
        return [name.strip()]
    return []


def _observation_identifiers(
    observation: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    channel = observation.get("channel")
    namespace = observation.get("account") or observation.get("namespace")
    candidates: list[Any] = []
    for key in ("raw_identifier", "sender_raw", "sender_id_raw", "participant_jid_raw", "identifier"):
        value = observation.get(key)
        if value is not None:
            candidates.append(value)
    for key in ("identifiers", "raw_identifiers"):
        value = observation.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    parsed: list[dict[str, Any]] = []
    reasons: list[str] = []
    for candidate in candidates:
        identifier, reason = _parse_identifier(candidate, channel, namespace)
        if identifier is None:
            if reason:
                reasons.append(reason)
            continue
        key = _observed_identifier_key(identifier)
        if not any(_observed_identifier_key(item) == key for item in parsed):
            parsed.append(identifier)
    return parsed, reasons[0] if reasons else None


def _parse_identifier(
    candidate: Any, default_channel: Any, default_namespace: Any
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(candidate, Mapping):
        channel = candidate.get("channel") or default_channel
        kind = candidate.get("kind")
        value = candidate.get("value") or candidate.get("identifier")
        namespace = candidate.get("namespace") or candidate.get("account") or default_namespace
    else:
        channel, namespace = default_channel, default_namespace
        kind, value = None, candidate
    if not isinstance(value, str) or not value.strip():
        return None, "missing_sender_identifier"
    value = value.strip()
    if isinstance(channel, str) and ":" in value:
        prefix, rest = value.split(":", 1)
        if prefix.lower() == channel.lower():
            value = rest.strip()
    if kind is None:
        lowered = value.lower()
        if lowered.endswith("@lid"):
            kind = "lid"
        elif lowered.endswith(("@s.whatsapp.net", "@c.us")):
            kind = "phone_jid"
        else:
            return None, "identifier_kind_unknown"
    if not isinstance(channel, str) or not channel.strip():
        return None, "channel_missing"
    if not isinstance(kind, str) or not kind.strip():
        return None, "identifier_kind_unknown"
    try:
        value = normalize_identifier_value(kind.strip().lower(), value)
    except ValueError:
        return None, "identifier_value_invalid"
    if not isinstance(namespace, str) or not namespace.strip():
        namespace = None
    return {
        "channel": channel.strip().lower(),
        "kind": kind.strip().lower(),
        "namespace": namespace.strip().lower() if namespace else None,
        "value": value,
    }, None


def _observed_identifier_key(identifier: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(identifier.get(key) for key in ("channel", "kind", "namespace", "value"))


def _observed_identifier_key_detail(key: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(("channel", "kind", "namespace", "value"), key, strict=True))


def _resolve_observed_identifier(
    identifier: Mapping[str, Any],
    observation: Mapping[str, Any],
    bindings: list[dict[str, Any]],
    canonical_ids: dict[str, str],
) -> dict[str, Any]:
    if identifier.get("namespace") is None:
        return {"status": "unresolved", "reason": "account_namespace_missing", "identifier": dict(identifier)}
    if str(identifier["channel"]) == "whatsapp":
        for suffix, reason in _NON_PERSON_SUFFIXES.items():
            if str(identifier["value"]).lower().endswith(suffix):
                return {"status": "unresolved", "reason": reason, "identifier": dict(identifier)}
    occurred_ms = observation.get("occurred_ms")
    exact_time = (
        isinstance(occurred_ms, int)
        and not isinstance(occurred_ms, bool)
        and occurred_ms > 0
        and str(observation.get("time_certainty") or "") in {"native", "provider_timestamp"}
    )
    if not exact_time:
        return {"status": "unresolved", "reason": "time_unknown", "identifier": dict(identifier)}
    key = _observed_identifier_key(identifier)
    matches = [row for row in bindings if _binding_key(row) == key]
    unknown_start_people = {
        canonical_ids.get(str(row.get("person_id") or ""), str(row.get("person_id") or ""))
        for row in matches
        if str(row.get("status") or "") in {"active", "ended"}
        and int(row.get("valid_from_ms") or 0) <= 0
    }
    if any(
        str(row.get("status") or "") == "ended"
        and int(row.get("valid_from_ms") or 0) > 0
        and int(row.get("valid_until_ms") or 0) <= 0
        and int(occurred_ms) >= int(row.get("valid_from_ms") or 0)
        for row in matches
    ):
        return {"status": "unresolved", "reason": "binding_end_unknown", "identifier": dict(identifier)}
    eligible = [
        row for row in matches
        if str(row.get("status") or "") in {"active", "ended"}
        and int(row.get("valid_from_ms") or 0) > 0
        and int(occurred_ms) >= int(row.get("valid_from_ms") or 0)
        and (
            str(row.get("status") or "") == "active"
            or int(row.get("valid_until_ms") or 0) > 0
        )
        and (
            int(row.get("valid_until_ms") or 0) == 0
            or int(occurred_ms) < int(row.get("valid_until_ms") or 0)
        )
    ]
    eligible_people = {
        canonical_ids.get(str(row.get("person_id") or ""), str(row.get("person_id") or ""))
        for row in eligible
    }
    if len(eligible_people) == 1:
        eligible_persons = sorted({str(row.get("person_id") or "") for row in eligible})
        eligible_person = eligible_persons[0]
        eligible_canonical = next(iter(eligible_people))
        if unknown_start_people - {eligible_canonical}:
            return {
                "status": "unresolved",
                "reason": "binding_start_conflict",
                "identifier": dict(identifier),
            }
        return {
            "status": "resolved",
            "person_id": eligible_person,
            "canonical_person_id": eligible_canonical,
            "original_person_ids": eligible_persons,
            "binding_id": eligible[0].get("binding_id"),
            "identifier": dict(identifier),
        }
    if len(eligible_people) > 1:
        return {"status": "unresolved", "reason": "binding_conflict", "identifier": dict(identifier)}
    if not matches:
        same_value = [
            row for row in bindings
            if str(row.get("channel") or "").lower() == str(identifier["channel"])
            and str(row.get("value") or "") == str(identifier["value"])
        ]
        reason = "binding_kind_mismatch" if same_value else "no_binding"
    elif any(
        str(row.get("status") or "") in {"active", "ended"}
        and int(row.get("valid_from_ms") or 0) == 0
        for row in matches
    ):
        reason = "binding_start_unknown"
    elif any(
        str(row.get("status") or "") == "ended"
        and int(row.get("valid_from_ms") or 0) > 0
        and int(row.get("valid_until_ms") or 0) <= 0
        for row in matches
    ):
        reason = "binding_end_unknown"
    elif any(str(row.get("status") or "") in {"conflict", "withheld"} for row in matches):
        reason = "binding_not_authoritative"
    else:
        reason = "outside_proven_period"
    return {"status": "unresolved", "reason": reason, "identifier": dict(identifier)}


def _observed_pair_candidates(
    observation: Mapping[str, Any],
    identifiers: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    canonical_ids: dict[str, str],
) -> list[dict[str, Any]]:
    if not _has_native_locator(observation):
        return []
    lids = [item for item in identifiers if item["kind"] == "lid"]
    phones = [item for item in identifiers if item["kind"] == "phone_jid"]
    if len(lids) != 1 or len(phones) != 1 or lids[0]["channel"] != phones[0]["channel"]:
        return []
    if lids[0]["namespace"] is None or lids[0]["namespace"] != phones[0]["namespace"]:
        return []
    lid_resolution = _resolve_observed_identifier(lids[0], observation, bindings, canonical_ids)
    phone_resolution = _resolve_observed_identifier(phones[0], observation, bindings, canonical_ids)
    if lid_resolution["status"] != "resolved" or phone_resolution["status"] != "resolved":
        return []
    if lid_resolution["canonical_person_id"] == phone_resolution["canonical_person_id"]:
        return []
    return [
        {
            "candidate_type": "coobserved_identifiers_bound_to_different_people",
            "event_id": observation.get("event_id"),
            "source_id": observation.get("source_id"),
            "locator": observation.get("locator"),
            "phone_person_id": phone_resolution["person_id"],
            "lid_person_id": lid_resolution["person_id"],
            "canonical_phone_person_id": phone_resolution["canonical_person_id"],
            "canonical_lid_person_id": lid_resolution["canonical_person_id"],
            "phone_identifier": phones[0],
            "lid_identifier": lids[0],
            "decision": "owner_review_required; no merge performed",
        }
    ]


def _has_native_locator(observation: Mapping[str, Any]) -> bool:
    if str(observation.get("provenance_class") or "") != "native":
        return False
    locator = observation.get("locator")
    has_locator = isinstance(locator, Mapping) and bool(locator)
    source_hash = observation.get("source_hash")
    valid_hash = (
        isinstance(source_hash, str)
        and len(source_hash) == 64
        and all(char in "0123456789abcdefABCDEF" for char in source_hash)
    )
    return has_locator and valid_hash
