"""Deterministic, isolated rebuild of preserved conversation history."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_gateway.processing.models import EVENT_KINDS

from . import _history_adapters as adapters
from ._history import HistoricalJournal, HistoryTargetError
from ._history_records import NormalizedEvent


class HistoryRebuildError(ValueError):
    """The explicit collection cannot be reconciled without guessing."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _positive_revision(value: Any) -> int:
    revision = _int(value)
    return revision if revision is not None and revision > 0 else 1


def _key(event_id: Any, revision: Any) -> tuple[str, str]:
    return str(event_id or ""), str(revision or 1)


def _scoped(event: NormalizedEvent) -> tuple[str | None, str | None, str | None, str | None]:
    return event.channel, event.account, event.chat_id, event.native_id


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_id(event: NormalizedEvent, copy: Mapping[str, Any]) -> str:
    value = copy.get("source_id")
    return str(value) if value else event.source_id


def _copy_locator(event: NormalizedEvent, copy: Mapping[str, Any]) -> dict[str, Any]:
    locator = copy.get("locator")
    if isinstance(locator, Mapping):
        return dict(locator)
    if isinstance(event.locator, Mapping):
        return dict(event.locator)
    return {"locator": str(event.locator)}


def _copy_identity(copy: Mapping[str, Any], event: NormalizedEvent) -> tuple[str, str]:
    return _key(copy.get("event_id") or event.event_id, copy.get("revision") or event.revision)


@contextmanager
def _copied_sqlite(path: Path, sidecars: Mapping[str, Path]) -> Iterator[sqlite3.Connection]:
    """Read only verified bundle bytes; SQLite never opens its source path writable."""
    with tempfile.TemporaryDirectory(prefix="yeoman-history-aux-") as scratch:
        copied = Path(scratch) / path.name
        shutil.copyfile(path, copied)
        for suffix in ("-wal", "-shm", "-journal"):
            sibling = sidecars.get(str(path) + suffix)
            if sibling is not None:
                shutil.copyfile(sibling, Path(f"{copied}{suffix}"))
        connection = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


def _verified_auxiliary(collection: Path) -> dict[str, Any]:
    bundle, manifest = adapters._read_manifest(collection)
    result: dict[str, Any] = {
        "authorities": [],
        "curated_denials": [],
        "suppressions": [],
        "unresolved": [],
    }
    for entry in manifest["sources"]:
        if entry.get("status") != "copied":
            continue
        source_id = str(entry["source_id"])
        files = adapters._copied_files(bundle, entry)
        sqlite_files = {
            path: (relative, digest)
            for relative, path, digest in files
            if Path(relative).suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        }
        verified_paths = {str(path): path for _relative, path, _digest in files}
        sidecars = {
            f"{path}{suffix}": verified_paths[f"{path}{suffix}"]
            for path in sqlite_files
            for suffix in ("-wal", "-shm", "-journal")
            if f"{path}{suffix}" in verified_paths
        }
        for path, (relative, _digest) in sqlite_files.items():
            try:
                with _copied_sqlite(path, sidecars) as connection:
                    tables = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "event_source_authority" in tables:
                        for row in connection.execute("SELECT * FROM event_source_authority"):
                            authority = dict(row)
                            authority.update(source_id=source_id, file=relative)
                            result["authorities"].append(authority)
                    if "knowledge_statement_sources" in tables:
                        statement_status: dict[str, str] = {}
                        if "knowledge_statements" in tables:
                            statement_status = {
                                str(row["statement_id"]): str(row["status"])
                                for row in connection.execute(
                                    "SELECT statement_id,status FROM knowledge_statements"
                                )
                            }
                        for row in connection.execute("SELECT * FROM knowledge_statement_sources"):
                            values = dict(row)
                            status = str(values.get("status") or "active")
                            if status == "revoked" or statement_status.get(str(values.get("statement_id"))) == "revoked":
                                result["curated_denials"].append({
                                    "event_id": values.get("event_id"),
                                    "revision": values.get("revision"),
                                    "source_id": source_id,
                                    "file": relative,
                                    "reason": "curated_source_revoked",
                                })
                    if "memory2_fact_sources" in tables and "memory2_facts" in tables:
                        for row in connection.execute(
                            "SELECT s.source_event_id AS event_id,s.source_revision AS revision,"
                            "f.assertion_status,f.revoked_at_ms FROM memory2_fact_sources s "
                            "JOIN memory2_facts f ON f.fact_id=s.fact_id"
                        ):
                            if row["assertion_status"] == "revoked" or row["revoked_at_ms"] is not None:
                                result["curated_denials"].append({
                                    "event_id": row["event_id"],
                                    "revision": row["revision"],
                                    "source_id": source_id,
                                    "file": relative,
                                    "reason": "curated_fact_source_revoked",
                                })
            except sqlite3.Error as exc:
                raise HistoryRebuildError("verified copied SQLite metadata is unreadable") from exc

        for relative, path, _digest in files:
            if Path(relative).name != "SUPPRESSIONS":
                continue
            # The existing parser is run only against this already hash-verified copy.
            from yeoman_shared.raw_archive.verify import load_suppressions

            with tempfile.TemporaryDirectory(prefix="yeoman-history-suppressions-") as scratch:
                root = Path(scratch)
                shutil.copyfile(path, root / "SUPPRESSIONS")
                for channel, chat_id, native_id in sorted(load_suppressions(root)):
                    result["suppressions"].append({
                        "source_id": source_id,
                        "file": relative,
                        "channel": channel,
                        "chat_id": chat_id,
                        "native_id": native_id,
                    })
    return result


def _event_original(event: NormalizedEvent) -> bool:
    return (
        event.provenance_class == "native"
        and event.kind in EVENT_KINDS
        and event.direction in {"in", "out"}
        and event.source_authority not in {
            "derived_only",
            "legacy_candidate",
            "none_authored_outbound_context_only",
            "none_for_authored_outbound",
            "unverified_native_reference",
            "reference_metadata_only",
        }
    )


def _verified_retention_witness(event: NormalizedEvent) -> bool:
    if (
        event.channel is None
        or event.channel.casefold() != "whatsapp"
        or not event.account
        or not event.chat_id
        or not event.native_id
        or event.text_hash is None
    ):
        return False
    return any(
        copy.get("text_hash") == event.text_hash
        and copy.get("provenance_class") == "native"
        and copy.get("channel") == event.channel
        and copy.get("account") == event.account
        and copy.get("chat_id") == event.chat_id
        and copy.get("native_id") == event.native_id
        and copy.get("source_authority") in {
            "journal_event", "inbound_archive_copy", "native_envelope", "native_payload"
        }
        for copy in _unique_copies(event)
    )


def _copy_for_authority(
    event: NormalizedEvent, authority: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    expected = _key(authority.get("event_id"), authority.get("revision"))
    return next(
        (
            copy
            for copy in event.copies
            if _source_id(event, copy) == authority.get("source_id")
            and isinstance(authority.get("file"), str)
            and _copy_locator(event, copy).get("file") == authority.get("file")
            and _copy_identity(copy, event) == expected
            and str(copy.get("source_kind") or "") == "journal"
            and str(copy.get("provenance_class") or "") == "native"
        ),
        None,
    )


def _source_revocation_copy_matches(
    event: NormalizedEvent, authority: Mapping[str, Any]
) -> bool:
    return (
        authority.get("revoked_at_ms") is not None
        and _copy_for_authority(event, authority) is not None
    )


def _source_revocation_metadata_matches(
    event: NormalizedEvent, authority: Mapping[str, Any]
) -> bool:
    copy = _copy_for_authority(event, authority)
    if copy is None or not _source_revocation_copy_matches(event, authority):
        return False
    row_author = _text(authority.get("author_principal"))
    row_channel = _text(authority.get("source_channel"))
    row_chat = _text(authority.get("source_chat_id"))
    row_time = _int(authority.get("occurred_at_ms"))
    copy_account = _text(copy.get("account"))
    return bool(
        row_author
        and row_channel == event.channel
        and row_chat == event.chat_id
        and row_time not in (None, 0)
        and event.occurred_ms == row_time
        and (event.principal is None or event.principal == row_author)
        and copy_account == event.account
    )


def _combined_authority(
    event: NormalizedEvent,
    rows: list[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    matching = [row for row in rows if _copy_for_authority(event, row) is not None]
    if not matching:
        return None, None
    reasons: list[str] = []
    if not _event_original(event):
        reasons.append("not_native_original")
    identities: set[tuple[Any, ...]] = set()
    for row in matching:
        copy = _copy_for_authority(event, row)
        assert copy is not None
        row_channel = _text(row.get("source_channel"))
        row_chat = _text(row.get("source_chat_id"))
        row_author = _text(row.get("author_principal"))
        row_time = _int(row.get("occurred_at_ms"))
        if not row_author or not row_channel or not row_chat or row_time in (None, 0):
            reasons.append("incomplete_source_proof")
        if row_channel != event.channel or row_chat != event.chat_id:
            reasons.append("source_scope_conflict")
        copy_account = _text(copy.get("account"))
        if copy_account != event.account or not copy_account:
            reasons.append("unknown_or_conflicting_account")
        if event.occurred_ms is None or row_time != event.occurred_ms:
            reasons.append("source_time_conflict_or_unknown")
        if event.principal and row_author and event.principal != row_author:
            reasons.append("source_author_conflict")
        identities.add((row_author, row_channel, row_chat, row_time))
        if row.get("revoked_at_ms") is not None:
            reasons.append("source_revoked")
    if len(identities) != 1:
        reasons.append("source_provenance_conflict")
    members: set[str] | None = None
    statuses: list[str] = []
    for row in matching:
        status = str(row.get("audience_status") or "unknown")
        statuses.append(status)
        try:
            raw_members = json.loads(row.get("audience_members_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_members = None
        if status == "known" and isinstance(raw_members, list) and all(isinstance(x, str) for x in raw_members):
            members = set(raw_members) if members is None else members & set(raw_members)
        elif status == "author_only":
            author = _text(row.get("author_principal"))
            author_members = {author} if author else set()
            members = author_members if members is None else members & author_members
        else:
            members = set()
    identity = next(iter(identities), (None, None, None, None))
    audience_status = "unknown"
    if "unknown" not in statuses and members is not None:
        if "author_only" in statuses:
            audience_status = "author_only" if members else "unknown"
        elif members:
            audience_status = "known"
        else:
            audience_status = "unknown"
    result = {
        "event_id": event.event_id,
        "revision": str(event.revision),
        "author_principal": identity[0],
        "channel": identity[1],
        "chat_id": identity[2],
        "occurred_ms": identity[3],
        "audience_status": audience_status,
        "audience_members": sorted(members or ()),
        "snapshot_id": next((row.get("audience_snapshot_id") for row in matching if row.get("audience_snapshot_id")), None),
        "policy_revision": next((row.get("policy_revision") for row in matching if row.get("policy_revision") is not None), None),
        "source_proofs": matching,
    }
    return result, ",".join(sorted(set(reasons))) or None


def _read_snapshot_refs(path: Path) -> list[tuple[str, int]]:
    if not path.is_file() or path.is_symlink():
        raise HistoryRebuildError("knowledge snapshot must be an explicit regular SQLite file")
    with tempfile.TemporaryDirectory(prefix="yeoman-history-knowledge-") as scratch:
        copied = Path(scratch) / path.name
        shutil.copyfile(path, copied)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_file():
                shutil.copyfile(sidecar, Path(f"{copied}{suffix}"))
        connection = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "knowledge_statement_sources" not in tables:
                return []
            return [
                (str(row["event_id"]), int(row["revision"]))
                for row in connection.execute(
                    "SELECT DISTINCT event_id,revision FROM knowledge_statement_sources"
                )
            ]
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryRebuildError("knowledge source reference snapshot is unreadable") from exc
        finally:
            connection.close()


def _validate_target_paths(
    collection: Path, target_home: Path, bridge_package_dir: Path | None, rosters: Path | None
) -> tuple[Path, Path]:
    source_root, manifest_path = adapters._manifest_path(collection)
    source_root = source_root.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requested = Path(target_home).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if requested.is_symlink():
        raise HistoryTargetError("history target cannot be a symlink")
    target = requested.resolve(strict=False)
    for candidate in (target, target.parent):
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise HistoryTargetError("history target paths cannot contain symlinks")
    input_roots = {source_root}
    for entry in manifest.get("sources", []):
        external = entry.get("source_path") if isinstance(entry, Mapping) else None
        if isinstance(external, str) and external:
            path = Path(external).expanduser()
            if path.exists():
                input_roots.add(path.resolve(strict=True))
    for input_root in input_roots:
        if target == input_root or target.is_relative_to(input_root) or input_root.is_relative_to(target):
            raise HistoryTargetError("history target overlaps a preservation input")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise HistoryTargetError("history rebuild target must be new or empty")
    for optional in (bridge_package_dir, rosters):
        if optional is None:
            continue
        path = Path(optional).expanduser().resolve(strict=False)
        if target == path or target.is_relative_to(path) or path.is_relative_to(target):
            raise HistoryTargetError("history target overlaps an explicit input")
    return source_root, target


def _unique_copies(event: NormalizedEvent) -> tuple[Mapping[str, Any], ...]:
    if event.copies:
        return event.copies
    return ({
        "source_id": event.source_id,
        "source_hash": event.source_hash,
        "locator": event.locator,
        "source_kind": event.source_kind,
        "provenance_class": event.provenance_class,
        "source_authority": event.source_authority,
        "channel": event.channel,
        "account": event.account,
        "chat_id": event.chat_id,
        "native_id": event.native_id,
        "kind": event.kind,
        "direction": event.direction,
        "revision": event.revision,
        "text_hash": event.text_hash,
    },)


def _normalize_delete_targets(
    events: list[NormalizedEvent],
) -> tuple[set[int], list[dict[str, Any]]]:
    denied_events: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    for delete in events:
        if delete.kind != "delete":
            continue
        target = _text(delete.delete_target)
        locator = dict(delete.locator) if isinstance(delete.locator, Mapping) else {"locator": str(delete.locator)}
        if target is None:
            unresolved.append({
                "event_id": delete.event_id,
                "revision": str(delete.revision),
                "source_id": delete.source_id,
                "locator_json": _json(locator),
                "target_json": "null",
                "reason": "delete_target_missing",
            })
            continue
        if not delete.channel or not delete.account or not delete.chat_id:
            unresolved.append({
                "event_id": delete.event_id,
                "revision": str(delete.revision),
                "source_id": delete.source_id,
                "locator_json": _json(locator),
                "target_json": _json(target),
                "reason": "delete_scope_unknown",
            })
            continue
        matches = [
            event
            for event in events
            if event.kind in {"message", "edit"}
            and (event.native_id == target or event.event_id == target)
            and _scoped(event)[:3] == _scoped(delete)[:3]
        ]
        if not matches:
            unresolved.append({
                "event_id": delete.event_id,
                "revision": str(delete.revision),
                "source_id": delete.source_id,
                "locator_json": _json(locator),
                "target_json": _json(target),
                "reason": "delete_target_unresolved",
            })
            continue
        denied_events.update(id(event) for event in matches)
    return denied_events, unresolved


def rebuild_history(
    *,
    collection: Path,
    target_home: Path,
    bridge_package_dir: Path | None = None,
    rosters: Path | None = None,
) -> dict[str, Any]:
    """Build a marked history journal from one explicit, verified collection."""
    _source_root, target = _validate_target_paths(
        collection, target_home, bridge_package_dir, rosters
    )
    roster_count = 0
    if rosters is not None:
        try:
            roster_data = json.loads(Path(rosters).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoryRebuildError("roster input is unreadable") from exc
        roster_count = len(roster_data) if isinstance(roster_data, list) else int(isinstance(roster_data, dict))

    events, adapter_report = adapters.read_catalogued_events(
        collection=Path(collection), bridge_package_dir=bridge_package_dir
    )
    auxiliary = _verified_auxiliary(Path(collection))
    authority_rows = auxiliary["authorities"]
    curated_denials = auxiliary["curated_denials"]

    # A delete is scoped by channel, account and chat. Unknown scope never guesses.
    denied_by_delete, unresolved = _normalize_delete_targets(events)
    unresolved.extend(auxiliary["unresolved"])
    source_revoked_events: set[int] = set()
    source_revocation_propagation_events: set[int] = set()
    for row in authority_rows:
        if row.get("revoked_at_ms") is None:
            continue
        matches = [event for event in events if _source_revocation_copy_matches(event, row)]
        propagation_matches = [
            event for event in matches if _source_revocation_metadata_matches(event, row)
        ]
        scopes = {_scoped(event) for event in propagation_matches}
        if matches:
            source_revoked_events.update(id(event) for event in matches)
        if propagation_matches and len(scopes) == 1:
            source_revocation_propagation_events.update(id(event) for event in propagation_matches)
        if not propagation_matches or len(scopes) != 1:
            unresolved.append({
                "event_id": str(row.get("event_id") or ""),
                "revision": str(row.get("revision") or 1),
                "source_id": str(row.get("source_id") or "unknown"),
                "locator_json": _json({"file": row.get("file"), "table": "event_source_authority"}),
                "target_json": "null",
                "reason": (
                    "source_revocation_source_unresolved" if not matches else
                    "source_revocation_metadata_unresolved" if not propagation_matches else
                    "source_revocation_scope_ambiguous"
                ),
            })

    curated_denied_events: set[int] = set()
    for row in curated_denials:
        key = _key(row.get("event_id"), row.get("revision"))
        matches = [
            event for event in events
            if any(
                _copy_identity(copy, event) == key
                for copy in _unique_copies(event)
            )
        ]
        if len(matches) == 1:
            curated_denied_events.add(id(matches[0]))
        else:
            unresolved.append({
                "event_id": key[0],
                "revision": key[1],
                "source_id": str(row.get("source_id") or "unknown"),
                "locator_json": _json({"file": row.get("file"), "table": "knowledge_statement_sources"}),
                "target_json": "null",
                "reason": "curated_denial_scope_ambiguous" if matches else "curated_denial_source_unresolved",
            })

    suppression_keys = {
        (item["source_id"], item["channel"], item["chat_id"], item["native_id"])
        for item in auxiliary["suppressions"]
    }
    suppressed_scopes: set[tuple[str | None, str | None, str | None, str | None]] = set()
    local_denial_reasons: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for event in events:
        scope = _scoped(event)
        matched: list[Mapping[str, Any]] = []
        for copy in _unique_copies(event):
            if (
                _source_id(event, copy), copy.get("channel"), copy.get("chat_id"),
                copy.get("archive_native_id") or copy.get("native_id"),
            ) not in suppression_keys:
                continue
            matched.append(copy)
            local_denial_reasons[(
                id(event), _source_id(event, copy), _json(_copy_locator(event, copy))
            )].add("raw_suppression")
        if matched and all(scope):
            suppressed_scopes.add(scope)
        elif matched:
            copy = matched[0]
            unresolved.append({
                "event_id": event.event_id,
                "revision": str(event.revision),
                "source_id": _source_id(event, copy),
                "locator_json": _json(_copy_locator(event, copy)),
                "target_json": _json(copy.get("archive_native_id") or copy.get("native_id")),
                "reason": "suppression_scope_unknown",
            })

    retention_denials: set[int] = set()
    retention_reasons: dict[int, str] = {}
    for event in events:
        purged = [copy for copy in _unique_copies(event) if copy.get("payload_purged_ms") is not None]
        if not purged:
            continue
        if any(str(copy.get("channel") or "").casefold() != "whatsapp" for copy in purged):
            retention_denials.add(id(event))
            retention_reasons[id(event)] = "deliberate_retention_purge"
        elif event.text is not None and not _verified_retention_witness(event):
            retention_denials.add(id(event))
            retention_reasons[id(event)] = "retention_witness_unverified"

    denied_events = set(denied_by_delete)
    deny_reasons: dict[int, set[str]] = defaultdict(set)
    for event in events:
        event_instance = id(event)
        if event.retention_status == "erased" or event.source_authority == "denied_erased":
            deny_reasons[event_instance].add("curated_erasure")
        if event_instance in source_revoked_events:
            deny_reasons[event_instance].add("source_revoked")
        if event_instance in curated_denied_events:
            deny_reasons[event_instance].add("curated_source_revoked")
        if event_instance in retention_denials:
            deny_reasons[event_instance].add(retention_reasons.get(event_instance, "deliberate_retention_purge"))
        copies = _unique_copies(event)
        local_suppression_keys = [
            (event_instance, _source_id(event, copy), _json(_copy_locator(event, copy)))
            for copy in copies
        ]
        all_copies_suppressed_locally = bool(local_suppression_keys) and all(
            "raw_suppression" in local_denial_reasons.get(key, ())
            for key in local_suppression_keys
        )
        if _scoped(event) in suppressed_scopes or all_copies_suppressed_locally:
            deny_reasons[event_instance].add("raw_suppression")
        if event_instance in denied_by_delete:
            deny_reasons[event_instance].add("delete_tombstone")
        if deny_reasons[event_instance]:
            denied_events.add(event_instance)

    # A known denial applies to every preserved copy of the same transport event,
    # including content-conflicting and derived rows, without crossing accounts.
    denied_scopes = {
        _scoped(event)
        for event in events
        if id(event) in denied_events
        and all(_scoped(event))
        and (
            id(event) in source_revocation_propagation_events
            or any(reason != "source_revoked" for reason in deny_reasons[id(event)])
        )
    }
    for event in events:
        if all(_scoped(event)) and _scoped(event) in denied_scopes:
            denied_events.add(id(event))
            deny_reasons[id(event)].add("compatible_copy_denied")

    # Preserve every copy. Conflicting reuse of a cited ID gets a stable source-bound
    # variant; the original cited ID continues to resolve to its first preserved event.
    assigned: list[tuple[NormalizedEvent, str, bool, bool]] = []
    prior: dict[tuple[str, str], tuple[str, str | None, tuple[Any, ...]]] = {}
    primary_ids: set[str] = set()
    conflict_count = 0
    for event in sorted(events, key=lambda item: (
        item.event_id,
        _positive_revision(item.revision),
        0 if item.source_kind == "journal" and item.provenance_class == "native" else 1 if item.provenance_class == "native" else 2,
        item.source_id,
        _json(item.locator),
    )):
        source_key = _key(event.event_id, event.revision)
        scope = _scoped(event)
        previous = prior.get(source_key)
        conflict = False
        revision_variant = False
        if previous is not None and (previous[1] != event.text_hash or previous[2] != scope):
            canonical_id = "history:" + _digest([event.source_id, event.locator, event.event_id, event.revision])[:32]
            conflict = True
            conflict_count += 1
        elif previous is not None:
            canonical_id = previous[0]
        else:
            if event.event_id in primary_ids:
                canonical_id = "history:" + _digest([
                    event.source_id, event.locator, event.event_id, event.revision, "revision"
                ])[:32]
                revision_variant = True
            else:
                canonical_id = event.event_id
                primary_ids.add(event.event_id)
            prior[source_key] = (canonical_id, event.text_hash, scope)
        assigned.append((event, canonical_id, conflict, revision_variant))

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.history-", dir=target.parent))
    try:
        journal = HistoricalJournal(stage)
        event_details: list[dict[str, Any]] = []
        copies: list[dict[str, Any]] = []
        aliases: list[dict[str, Any]] = []
        source_proofs: list[dict[str, Any]] = []
        denials: list[dict[str, Any]] = []
        unresolved_refs = list(unresolved)
        name_observations: list[dict[str, Any]] = []
        appended: set[tuple[str, str]] = set()
        accepted = 0
        for event, canonical_id, conflict, revision_variant in assigned:
            revision = _positive_revision(event.revision)
            canonical_revision = str(revision)
            event_key = _key(canonical_id, canonical_revision)
            denied = id(event) in denied_events
            text_value = None if denied else event.text
            canonical_kind = event.kind if event.kind in EVENT_KINDS else "message"
            canonical_direction = event.direction if event.direction in {"in", "out"} else "in"
            placeholder = canonical_kind != event.kind or canonical_direction != event.direction
            payload = {
                "history_import": True,
                "normalization_version": event.normalization_version,
                "semantic_kind": event.kind,
                "semantic_direction": event.direction,
                "text": text_value,
                "text_hash": event.text_hash,
                "media_kind": event.media_kind,
                "media_missing": event.media_missing,
                "reply_target": event.reply_target,
                "edit_target": event.edit_target,
                "delete_target": event.delete_target,
                "occurred_ms": event.occurred_ms,
                "observed_ms": event.observed_ms,
                "time_certainty": event.time_certainty,
                "source_authority": event.source_authority,
                "provenance_class": event.provenance_class,
                "placeholder_classification": placeholder,
            }
            if event_key not in appended:
                # ProcessingStore needs an internal clock; zero is its unknown sentinel.
                # Capture time is never promoted to an event/source occurrence time.
                created_ms = event.occurred_ms if event.occurred_ms is not None else 0
                journal.store.append_event(
                    event_key="history:" + _digest([event.source_id, event.locator, canonical_id, canonical_revision]),
                    event_id=canonical_id,
                    trace_id="history:" + _digest([canonical_id, canonical_revision, "trace"])[:32],
                    payload={
                        "kind": canonical_kind,
                        "origin": "historical_rebuild",
                        "principal": event.principal or "",
                        "channel": event.channel or "",
                        "chat_id": event.chat_id or "",
                        "account": event.account or "",
                        "direction": canonical_direction,
                        "revision": revision,
                        "occurred_ms": event.occurred_ms,
                        "source_message_id": event.native_id,
                        "target_message_id": event.edit_target or event.delete_target,
                        **payload,
                    },
                    now_ms=created_ms,
                    account=event.account or "",
                    direction=canonical_direction,
                    revision=revision,
                )
                appended.add(event_key)
                accepted += 1

            normalized = event.as_dict()
            normalized["event_id"] = canonical_id
            normalized["revision"] = revision
            normalized["text"] = text_value
            normalized["denied"] = denied
            event_details.append({
                "event_id": canonical_id,
                "revision": canonical_revision,
                "normalized_json": _json(normalized),
                "semantic_kind": event.kind,
                "semantic_direction": event.direction,
                "provenance_class": event.provenance_class,
                "retention_status": event.retention_status,
                "text_hash": event.text_hash,
                "denied": int(denied),
            })
            event_copies = _unique_copies(event)
            for copy in event_copies:
                source_id = _source_id(event, copy)
                locator = _copy_locator(event, copy)
                locator_json = _json(locator)
                copy_event_id, copy_revision = _copy_identity(copy, event)
                copy_scope = (
                    copy.get("channel", event.channel),
                    copy.get("account", event.account),
                    copy.get("chat_id", event.chat_id),
                    copy.get("native_id", event.native_id),
                )
                local_reasons = local_denial_reasons.get((
                    id(event), source_id, locator_json
                ), set())
                copy_denied = denied or copy_scope in suppressed_scopes or bool(local_reasons)
                disposition = (
                    "denied" if copy_denied else
                    "conflict_variant" if conflict else
                    "logical_copy"
                )
                copy_metadata = dict(copy)
                copy_metadata.update(source_id=source_id, locator=locator, denied=copy_denied)
                copies.append({
                    "event_id": canonical_id,
                    "revision": canonical_revision,
                    "source_id": source_id,
                    "source_hash": copy.get("source_hash"),
                    "locator_json": locator_json,
                    "source_kind": str(copy.get("source_kind") or event.source_kind),
                    "semantic_kind": str(copy.get("kind") or event.kind),
                    "semantic_direction": str(copy.get("direction") or event.direction),
                    "provenance_class": str(copy.get("provenance_class") or event.provenance_class),
                    "source_authority": copy.get("source_authority", event.source_authority),
                    "channel": copy_scope[0],
                    "account": copy_scope[1],
                    "chat_id": copy_scope[2],
                    "native_id": copy_scope[3],
                    "text_hash": copy.get("text_hash") or event.text_hash,
                    "text_value": (
                        event.text
                        if not copy_denied and copy.get("text_hash") == event.text_hash
                        else None
                    ),
                    "disposition": disposition,
                    "copy_json": _json(copy_metadata),
                })
                aliases.append({
                    "source_event_id": copy_event_id,
                    "source_revision": copy_revision,
                    "canonical_event_id": canonical_id,
                    "canonical_revision": canonical_revision,
                    "source_id": source_id,
                    "locator_json": locator_json,
                    "status": "conflict_variant" if conflict else "revision_variant" if revision_variant else "resolved",
                })
            for name in event.name_observations:
                if isinstance(name, Mapping) and _text(name.get("name")):
                    name_observations.append({
                        "event_id": canonical_id,
                        "revision": canonical_revision,
                        "source_id": str(name.get("source_id") or event.source_id),
                        "locator_json": _json(
                            name.get("locator")
                            if isinstance(name.get("locator"), Mapping)
                            else event.locator
                        ),
                        "occurred_ms": name.get("occurred_ms"),
                        "observed_ms": name.get("observed_ms"),
                        "time_certainty": name.get("time_certainty") or "unknown",
                        "raw_identifier": name.get("raw_identifier"),
                        "name": name["name"],
                        "channel": event.channel,
                        "account": event.account,
                        "chat_id": event.chat_id,
                        "provenance_class": name.get("provenance_class") or event.provenance_class,
                    })

        # Apply independent, source-bound authority only after all copies and aliases exist.
        for event, canonical_id, conflict, _revision_variant in assigned:
            proof, reason = _combined_authority(event, authority_rows)
            if proof is None:
                continue
            proof_id = _key(event.event_id, event.revision)
            source_revoked_for_copy = any(
                _key(row.get("event_id"), row.get("revision")) == proof_id
                and _source_revocation_copy_matches(event, row)
                for row in authority_rows
            )
            revoked = source_revoked_for_copy or id(event) in denied_events
            if conflict:
                reason = ",".join(filter(None, (reason, "conflicting_source_variant")))
            if revoked:
                reason = ",".join(filter(None, (reason, "source_revoked_or_denied")))
            eligible = reason is None and not revoked
            for row in proof["source_proofs"]:
                copy = _copy_for_authority(event, row)
                assert copy is not None
                proof_ids = {
                    (canonical_id, str(_positive_revision(event.revision))),
                    _key(row.get("event_id"), row.get("revision")),
                }
                for proof_event_id, proof_revision in proof_ids:
                    source_proofs.append({
                        "event_id": proof_event_id,
                        "revision": proof_revision,
                        "source_id": row["source_id"],
                        "locator_json": _json(_copy_locator(event, copy)),
                        "author_principal": proof["author_principal"],
                        "channel": proof["channel"],
                        "chat_id": proof["chat_id"],
                        "occurred_ms": proof["occurred_ms"],
                        "audience_status": proof["audience_status"],
                        "audience_members_json": _json(proof["audience_members"]),
                        "snapshot_id": proof["snapshot_id"],
                        "policy_revision": str(proof["policy_revision"]) if proof["policy_revision"] is not None else None,
                        "revoked_at_ms": row.get("revoked_at_ms"),
                        "revoking_event_id": row.get("revoking_event_id"),
                        "eligible": int(eligible),
                        "denial_reason": reason,
                    })
            if eligible:
                source = SourceRef(
                    event_id=canonical_id,
                    revision=_positive_revision(event.revision),
                    channel=str(proof["channel"]),
                    chat_id=str(proof["chat_id"]),
                    author_principal=str(proof["author_principal"]),
                    occurred_at_ms=int(proof["occurred_ms"]),
                )
                audience = (
                    EvidenceAudience.known(frozenset(proof["audience_members"]), snapshot_id=proof["snapshot_id"], policy_revision=proof["policy_revision"])
                    if proof["audience_status"] == "known"
                    else EvidenceAudience.author_only(snapshot_id=proof["snapshot_id"], policy_revision=proof["policy_revision"])
                    if proof["audience_status"] == "author_only"
                    else EvidenceAudience.unknown()
                )
                journal.store.upsert_event_source_authority(
                    source=source,
                    audience=audience,
                    now_ms=int(proof["occurred_ms"]),
                )

        # Re-run delete and retention denials against assigned IDs for report/lineage.
        for event in events:
            event_reasons = deny_reasons.get(id(event), set())
            for copy in _unique_copies(event):
                source_id = _source_id(event, copy)
                locator_json = _json(_copy_locator(event, copy))
                reasons = event_reasons or local_denial_reasons.get(
                    (id(event), source_id, locator_json), set()
                )
                for reason in reasons:
                    denials.append({
                        "event_id": event.event_id,
                        "revision": str(event.revision),
                        "source_id": source_id,
                        "locator_json": locator_json,
                        "channel": copy.get("channel", event.channel),
                        "account": copy.get("account", event.account),
                        "chat_id": copy.get("chat_id", event.chat_id),
                        "native_id": copy.get("native_id", event.native_id),
                        "reason": reason,
                    })
        for row in curated_denials:
            denials.append({
                "event_id": str(row.get("event_id") or "") or None,
                "revision": str(row.get("revision") or 1),
                "source_id": str(row.get("source_id") or "unknown"),
                "locator_json": _json({"file": row.get("file"), "table": "knowledge_statement_sources"}),
                "channel": None,
                "account": None,
                "chat_id": None,
                "native_id": None,
                "reason": row["reason"],
            })
        report = {
            "schema": "history-rebuild-report-v1",
            "complete": True,
            "collection_complete": bool(adapter_report.get("complete_manifest")),
            "collection_verdict": adapter_report.get("collection_verdict"),
            "adapter_report": adapter_report,
            "event_count": accepted,
            "copy_count": len(copies),
            "conflict_count": conflict_count,
            "denial_count": len(denials),
            "source_proof_count": len(source_proofs),
            "unresolved_count": len(unresolved_refs),
            "roster_input_count": roster_count,
            "roster_input_applied": False,
        }
        journal.write_rebuild_records(
            event_details=event_details,
            copies=copies,
            aliases=aliases,
            source_proofs=source_proofs,
            denials=denials,
            unresolved_refs=unresolved_refs,
            name_observations=name_observations,
            report=report,
        )
        journal.close()
        with HistoricalJournal(stage, create=False) as staged:
            if not staged.rebuild_state().get("complete"):
                raise HistoryRebuildError("history rebuild did not reach its completion marker")
            check = staged.store._conn.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise HistoryRebuildError("rebuilt history journal failed integrity check")
        os.replace(stage, target)
        return report
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_history(
    *, target_home: Path, knowledge_snapshot: Path | None = None
) -> dict[str, Any]:
    """Check target integrity and exact statement source ID/revision closure."""
    try:
        with HistoricalJournal(target_home, create=False) as journal:
            state = journal.rebuild_state()
            rows = journal.store._conn.execute(
                "SELECT event_id,revision FROM events"
            ).fetchall()
            resolved = {(str(row["event_id"]), int(row["revision"])) for row in rows}
            aliases = journal.store._conn.execute(
                "SELECT source_event_id,source_revision,canonical_event_id,canonical_revision "
                "FROM history_event_aliases"
            ).fetchall()
            alias_map = {
                (str(row["source_event_id"]), _positive_revision(row["source_revision"])):
                (str(row["canonical_event_id"]), _positive_revision(row["canonical_revision"]))
                for row in aliases
            }
            references = _read_snapshot_refs(knowledge_snapshot) if knowledge_snapshot is not None else []
            missing = [
                {"event_id": event_id, "revision": revision}
                for event_id, revision in references
                if (event_id, revision) not in resolved
                and alias_map.get((event_id, revision)) not in resolved
            ]
            unresolved_deletes = int(journal.store._conn.execute(
                "SELECT COUNT(*) FROM history_unresolved_refs WHERE reason LIKE 'delete_%'"
            ).fetchone()[0])
            integrity = str(journal.store._conn.execute("PRAGMA quick_check").fetchone()[0])
            report = {
                **state,
                "integrity": integrity,
                "event_count": len(resolved),
                "statement_source_count": len(references),
                "resolved_source_count": len(references) - len(missing),
                "missing_source_refs": missing,
                "unresolved_delete_count": unresolved_deletes,
            }
            report["complete"] = bool(state.get("complete")) and not missing and integrity == "ok"
            return report
    except (HistoryTargetError, sqlite3.Error) as exc:
        raise HistoryRebuildError("history target failed verification") from exc
