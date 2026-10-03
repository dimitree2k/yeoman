"""Deterministic, isolated rebuild of preserved conversation history."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter, defaultdict
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


_SEMANTIC_COLUMNS = {
    "canonical_events": (
        "event_id", "event_key", "trace_id", "kind", "origin", "channel", "account", "chat_id",
        "principal", "direction", "revision", "audience_ref", "source_message_id",
        "target_message_id", "thread_id", "turn_id", "occurred_ms", "created_ms", "payload_hash",
        "payload_json", "payload_purged_ms",
    ),
    "event_source_authority": (
        "event_id", "revision", "author_principal", "source_channel", "source_chat_id",
        "occurred_at_ms", "audience_status", "audience_members_json", "audience_snapshot_id",
        "policy_revision", "revoked_at_ms", "revoking_event_id",
    ),
    "event_details": (
        "event_id", "revision", "normalized_json", "semantic_kind", "semantic_direction",
        "provenance_class", "retention_status", "text_hash", "denied",
    ),
    "event_copies": (
        "event_id", "revision", "source_id", "source_hash", "locator_json", "source_kind",
        "semantic_kind", "semantic_direction", "provenance_class", "source_authority", "channel",
        "account", "chat_id", "native_id", "text_hash", "text_value", "disposition", "copy_json",
    ),
    "event_aliases": (
        "source_event_id", "source_revision", "canonical_event_id", "canonical_revision",
        "source_id", "locator_json", "status",
    ),
    "source_proofs": (
        "event_id", "revision", "source_id", "locator_json", "author_principal", "channel",
        "chat_id", "occurred_ms", "audience_status", "audience_members_json", "snapshot_id",
        "policy_revision", "revoked_at_ms", "revoking_event_id", "eligible", "denial_reason",
    ),
    "denials": (
        "event_id", "revision", "source_id", "locator_json", "channel", "account", "chat_id",
        "native_id", "reason",
    ),
    "unresolved_refs": (
        "event_id", "revision", "source_id", "locator_json", "target_json", "reason",
    ),
    "name_observations": (
        "event_id", "revision", "source_id", "locator_json", "occurred_ms", "observed_ms",
        "time_certainty", "raw_identifier", "name", "channel", "account", "chat_id",
        "provenance_class",
    ),
}


def _source_software_receipt() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[4]
    paths = (
        Path(__file__).resolve(),
        Path(adapters.__file__).resolve(),
        Path(__file__).resolve().with_name("_history_records.py"),
    )
    implementation_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }
    head: str | None = None
    dirty: bool | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            head = result.stdout.strip() or None
            status = subprocess.run(
                [
                    "git", "-C", str(root), "status", "--porcelain", "--untracked-files=no",
                    "--", *(str(path.relative_to(root)) for path in paths),
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {
        "normalization_version": 1,
        "source_head": head,
        "normalizer_worktree_dirty": dirty,
        "implementation_sha256": implementation_hashes,
    }


def _decoder_receipt(bridge_package_dir: Path | None) -> dict[str, Any]:
    script = str(getattr(adapters, "_DECODER_SCRIPT", "")).encode("utf-8")
    receipt: dict[str, Any] = {
        "capability": "whatsapp_web_message_info_decode",
        "available": False,
        "version": None,
        "package_sha256": None,
        "module_sha256": None,
        "decoder_script_sha256": hashlib.sha256(script).hexdigest(),
    }
    if bridge_package_dir is None:
        receipt["status"] = "not_supplied"
        return receipt
    package = Path(bridge_package_dir).expanduser()
    package_json = package / "node_modules" / "@whiskeysockets" / "baileys" / "package.json"
    decoder_module = package / "node_modules" / "@whiskeysockets" / "baileys" / "WAProto" / "index.js"
    if not package_json.is_file() or not decoder_module.is_file():
        receipt["status"] = "unavailable_offline"
        return receipt
    try:
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or not isinstance(metadata.get("version"), str):
            raise ValueError("local decoder package version is missing")
        receipt.update(
            available=True,
            status="available_offline",
            version=metadata["version"],
            package_sha256=hashlib.sha256(package_json.read_bytes()).hexdigest(),
            module_sha256=hashlib.sha256(decoder_module.read_bytes()).hexdigest(),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        receipt["status"] = "unavailable_offline"
    return receipt


def _input_receipt(collection: Path) -> dict[str, Any]:
    _bundle, manifest = adapters._read_manifest(collection)
    statuses = Counter(str(entry.get("status") or "missing") for entry in manifest["sources"])
    accepted_statuses = {"copied", "reference_only", "excluded"}
    missing = [
        str(entry["source_id"])
        for entry in manifest["sources"]
        if entry.get("status") not in accepted_statuses
    ]
    file_hashes: list[dict[str, str]] = []
    for entry in manifest["sources"]:
        source_id = str(entry["source_id"])
        copied = entry.get("copied_files")
        if isinstance(copied, Mapping):
            for relative, details in copied.items():
                if isinstance(details, Mapping) and isinstance(details.get("copied_sha256"), str):
                    file_hashes.append({
                        "source_id": source_id,
                        "file": str(relative),
                        "sha256": str(details["copied_sha256"]),
                    })
        reference_stats = entry.get("reference_file_stats")
        if isinstance(reference_stats, Mapping):
            for relative, details in reference_stats.items():
                if isinstance(details, Mapping) and isinstance(details.get("sha256"), str):
                    file_hashes.append({
                        "source_id": source_id,
                        "file": str(relative),
                        "sha256": str(details["sha256"]),
                    })
    file_hashes.sort(key=_json)
    complete = manifest.get("complete") is True and not missing
    material = {
        "manifest_version": manifest.get("source_bundle_manifest_version"),
        "declared_complete": manifest.get("complete") is True,
        "descriptor_count": len(manifest["sources"]),
        "status_counts": dict(sorted(statuses.items())),
        "missing_source_ids": sorted(missing),
        "source_hashes": file_hashes,
    }
    return {**material, "input_complete": complete, "sha256": _digest(material)}


def _query_semantic_table(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> list[dict[str, Any]]:
    available = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if not available:
        return []
    selected = tuple(column for column in columns if column in available)
    names = ",".join(selected)
    rows = connection.execute(f"SELECT {names} FROM {table}").fetchall()
    return sorted(
        ({column: row[column] for column in selected} for row in rows),
        key=_json,
    )


def _semantic_receipt(
    records: dict[str, list[dict[str, Any]]],
    *,
    input_receipt: Mapping[str, Any],
    software_receipt: Mapping[str, Any],
    decoder_receipt: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    conflict_count: int,
) -> dict[str, Any]:
    hashes = {name: _digest(_semantic_rows(rows)) for name, rows in records.items()}
    counts = {name: len(rows) for name, rows in records.items()}
    diagnostics = _semantic_projection({
        key: adapter_report.get(key)
        for key in (
            "collection_verdict", "parsed_count", "unresolved_count", "denial_evidence_count",
            "omitted_counts", "unsupported_tables", "unresolved", "sources",
        )
    })
    receipt_counts = {**counts, "conflicts": conflict_count}
    input_sha256 = input_receipt.get("sha256")
    normalization = dict(software_receipt)
    decoder = dict(decoder_receipt)
    material = {
        "normalization_version": 1,
        "counts": receipt_counts,
        "hashes": hashes,
        "input_sha256": input_sha256,
        "normalization": normalization,
        "decoder": decoder,
        "diagnostics_sha256": _digest(diagnostics),
        "conflict_count": conflict_count,
    }
    return {
        "schema": "history-semantic-receipt-v1",
        "counts": receipt_counts,
        "hashes": hashes,
        "input_sha256": input_sha256,
        "normalization": normalization,
        "decoder": decoder,
        "diagnostics_sha256": material["diagnostics_sha256"],
        "sha256": _digest(material),
    }


def _semantic_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        for column in ("normalized_json", "copy_json"):
            value = normalized.get(column)
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                normalized[column] = _semantic_projection(decoded)
        result.append(_semantic_projection(normalized))
    return sorted(result, key=_json)


def _input_receipt_valid(receipt: Mapping[str, Any]) -> bool:
    keys = (
        "manifest_version", "declared_complete", "descriptor_count", "status_counts",
        "missing_source_ids", "source_hashes",
    )
    material = {key: receipt.get(key) for key in keys}
    expected_complete = receipt.get("declared_complete") is True and not receipt.get(
        "missing_source_ids"
    )
    return (
        receipt.get("input_complete") is expected_complete
        and receipt.get("sha256") == _digest(material)
    )


def _semantic_projection(value: Any) -> Any:
    """Remove acquisition and byte-container details from semantic hashes."""
    excluded = {
        "acquisition_started_ms",
        "acquisition_finished_ms",
        "collection_finished_ms",
        "source_mtime_ns",
        "mtime_ns",
        "path",
        "source_path",
        "target_home",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_projection(item)
            for key, item in value.items()
            if str(key) not in excluded
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_projection(item) for item in value]
    return value


def _semantic_records_from_target(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    tables = {
        "canonical_events": "events",
        "event_source_authority": "event_source_authority",
        "event_details": "history_event_details",
        "event_copies": "history_event_copies",
        "event_aliases": "history_event_aliases",
        "source_proofs": "history_source_proofs",
        "denials": "history_denials",
        "unresolved_refs": "history_unresolved_refs",
        "name_observations": "history_name_observations",
    }
    return {
        name: _query_semantic_table(connection, table, _SEMANTIC_COLUMNS[name])
        for name, table in tables.items()
    }


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


def _curated_event_index(
    events: list[NormalizedEvent],
) -> dict[tuple[str, str], list[NormalizedEvent]]:
    indexed: dict[tuple[str, str], list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        identities = {_copy_identity(copy, event) for copy in _unique_copies(event)}
        for identity in identities:
            indexed[identity].append(event)
    return indexed


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


def _authority_candidate_index(
    events: list[NormalizedEvent],
    authority_rows: list[Mapping[str, Any]],
) -> tuple[list[list[NormalizedEvent]], dict[int, list[Mapping[str, Any]]]]:
    events_by_source_identity: dict[tuple[str, str, str, str], list[NormalizedEvent]] = defaultdict(list)
    for event in events:
        seen: set[tuple[str, str, str, str]] = set()
        for copy in event.copies:
            if (
                str(copy.get("source_kind") or "") != "journal"
                or str(copy.get("provenance_class") or "") != "native"
            ):
                continue
            source = _source_id(event, copy)
            file = _copy_locator(event, copy).get("file")
            if not isinstance(file, str):
                continue
            event_id, revision = _copy_identity(copy, event)
            key = (event_id, revision, source, file)
            if key not in seen:
                events_by_source_identity[key].append(event)
                seen.add(key)

    events_by_authority_row: list[list[NormalizedEvent]] = []
    authorities_by_event: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in authority_rows:
        file = row.get("file")
        key = (*_key(row.get("event_id"), row.get("revision")), row.get("source_id"), file)
        candidates = (
            events_by_source_identity.get(key, ())
            if isinstance(file, str) and isinstance(row.get("source_id"), str)
            else ()
        )
        matches = [event for event in candidates if _copy_for_authority(event, row) is not None]
        events_by_authority_row.append(matches)
        for event in matches:
            authorities_by_event[id(event)].append(row)
    return events_by_authority_row, authorities_by_event


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


def _read_snapshot_refs(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise HistoryRebuildError("knowledge snapshot must be an explicit regular SQLite file")
    with tempfile.TemporaryDirectory(prefix="yeoman-history-knowledge-") as scratch:
        copied = Path(scratch) / path.name
        shutil.copyfile(path, copied)
        copied_hashes = {path.name: hashlib.sha256(copied.read_bytes()).hexdigest()}
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_file():
                if sidecar.is_symlink():
                    raise HistoryRebuildError("knowledge snapshot sidecars cannot be symlinks")
                copied_sidecar = Path(f"{copied}{suffix}")
                shutil.copyfile(sidecar, copied_sidecar)
                copied_hashes[copied_sidecar.name] = hashlib.sha256(
                    copied_sidecar.read_bytes()
                ).hexdigest()
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
                return [], {
                    "present": True,
                    "source_row_count": 0,
                    "distinct_reference_count": 0,
                    "copied_sha256": dict(sorted(copied_hashes.items())),
                }
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_statement_sources)"
                )
            }
            if not {"event_id", "revision"}.issubset(columns):
                raise HistoryRebuildError("knowledge snapshot source-reference schema is incomplete")
            selected = ["event_id", "revision"]
            if "statement_id" in columns:
                selected.insert(0, "statement_id")
            rows = connection.execute(
                f"SELECT {','.join(selected)} FROM knowledge_statement_sources"
            ).fetchall()
            grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
            invalid: list[dict[str, Any]] = []
            for row in rows:
                event_id = row["event_id"]
                revision = _int(row["revision"])
                statement_id = str(row["statement_id"]) if "statement_id" in columns else ""
                if not isinstance(event_id, str) or not event_id.strip() or revision is None or revision < 1:
                    invalid.append({
                        "event_id": event_id if isinstance(event_id, str) else None,
                        "revision": revision,
                        "statement_id": statement_id or None,
                        "reason": "invalid_source_reference",
                    })
                    continue
                grouped[(event_id, revision)].append(statement_id)
            references = [
                {
                    "event_id": event_id,
                    "revision": revision,
                    "statement_ids": sorted(value for value in statement_ids if value),
                    "source_row_count": len(statement_ids),
                }
                for (event_id, revision), statement_ids in sorted(grouped.items())
            ]
            references.extend(invalid)
            return references, {
                "present": True,
                "source_row_count": len(rows),
                "distinct_reference_count": len(grouped) + len(invalid),
                "copied_sha256": dict(sorted(copied_hashes.items())),
            }
        except (sqlite3.Error, TypeError, ValueError) as exc:
            if isinstance(exc, HistoryRebuildError):
                raise
            raise HistoryRebuildError("knowledge source reference snapshot is unreadable") from exc
        finally:
            connection.close()


def _original_source_status(
    connection: sqlite3.Connection,
    canonical: tuple[str, int],
    source_copies: list[dict[str, Any]],
) -> tuple[str, str | None]:
    detail = connection.execute(
        "SELECT normalized_json FROM history_event_details WHERE event_id=? AND revision=?",
        (canonical[0], str(canonical[1])),
    ).fetchone()
    if detail is None:
        return "invalid", "canonical_detail_missing"
    try:
        normalized = json.loads(str(detail["normalized_json"]))
    except (TypeError, json.JSONDecodeError):
        return "invalid", "canonical_detail_invalid"
    if not isinstance(normalized, dict):
        return "invalid", "canonical_detail_invalid"
    if (
        normalized.get("provenance_class") != "native"
        or normalized.get("kind") not in EVENT_KINDS
        or normalized.get("direction") not in {"in", "out"}
        or normalized.get("source_authority") in {
            "derived_only",
            "legacy_candidate",
            "none_authored_outbound_context_only",
            "none_for_authored_outbound",
            "unverified_native_reference",
            "reference_metadata_only",
        }
    ):
        return "invalid", "source_is_derived_or_unverified"
    for copy in source_copies:
        locator_json = copy.get("locator_json")
        try:
            locator = json.loads(str(locator_json))
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            copy.get("provenance_class") == "native"
            and copy.get("source_authority") not in {
                "derived_only",
                "legacy_candidate",
                "none_authored_outbound_context_only",
                "none_for_authored_outbound",
                "unverified_native_reference",
                "reference_metadata_only",
            }
            and isinstance(copy.get("source_hash"), str)
            and len(str(copy["source_hash"])) == 64
            and isinstance(locator, dict)
            and locator
        ):
            return "valid", None
    return "invalid", "original_source_copy_unavailable"


def _resolve_snapshot_refs(
    connection: sqlite3.Connection,
    references: list[dict[str, Any]],
    *,
    invalid_snapshot_refs: list[dict[str, Any]],
    adapter_unresolved_count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_rows = connection.execute("SELECT event_id,revision FROM events").fetchall()
    canonical_events = {
        (str(row["event_id"]), int(row["revision"])) for row in event_rows
    }
    alias_rows = connection.execute(
        "SELECT source_event_id,source_revision,canonical_event_id,canonical_revision,source_id,locator_json "
        "FROM history_event_aliases"
    ).fetchall()
    aliases: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in alias_rows:
        aliases[(str(row["source_event_id"]), _positive_revision(row["source_revision"]))].append(
            dict(row)
        )
    unresolved: list[dict[str, Any]] = list(invalid_snapshot_refs)
    source_results: list[dict[str, Any]] = []
    valid = invalid = unresolved_original = structurally_resolved = 0
    for reference in references:
        event_id, revision = reference.get("event_id"), reference.get("revision")
        if not isinstance(event_id, str) or not isinstance(revision, int) or revision < 1:
            continue
        key = (event_id, revision)
        candidates = aliases.get(key, [])
        targets = {
            (str(row["canonical_event_id"]), _positive_revision(row["canonical_revision"]))
            for row in candidates
        }
        if len(targets) > 1:
            unresolved.append({
                "event_id": event_id,
                "revision": revision,
                "statement_ids": reference.get("statement_ids", []),
                "reason": "ambiguous_source_alias",
            })
            source_results.append({
                "event_id": event_id,
                "revision": revision,
                "statement_ids": reference.get("statement_ids", []),
                "structural_status": "ambiguous",
                "original_source_status": "unresolved",
                "reason": "ambiguous_source_alias",
            })
            unresolved_original += 1
            continue
        if targets:
            canonical = next(iter(targets))
            source_copies = [
                dict(row)
                for row in connection.execute(
                    "SELECT event_id,revision,source_id,source_hash,locator_json,provenance_class,source_authority "
                    "FROM history_event_copies WHERE event_id=? AND revision=?",
                    (canonical[0], str(canonical[1])),
                )
                if any(
                    row["source_id"] == alias["source_id"]
                    and row["locator_json"] == alias["locator_json"]
                    for alias in candidates
                )
            ]
        elif key in canonical_events:
            canonical = key
            source_copies = [
                dict(row)
                for row in connection.execute(
                    "SELECT event_id,revision,source_id,source_hash,locator_json,provenance_class,source_authority "
                    "FROM history_event_copies WHERE event_id=? AND revision=?",
                    (canonical[0], str(canonical[1])),
                )
            ]
        else:
            unresolved.append({
                "event_id": event_id,
                "revision": revision,
                "statement_ids": reference.get("statement_ids", []),
                "reason": "source_reference_unresolved",
            })
            source_results.append({
                "event_id": event_id,
                "revision": revision,
                "statement_ids": reference.get("statement_ids", []),
                "structural_status": "unresolved",
                "original_source_status": "unresolved",
                "reason": "source_reference_unresolved",
            })
            unresolved_original += 1
            continue
        structurally_resolved += 1
        status, reason = _original_source_status(connection, canonical, source_copies)
        if status == "valid":
            valid += 1
        else:
            invalid += 1
        source_results.append({
            "event_id": event_id,
            "revision": revision,
            "statement_ids": reference.get("statement_ids", []),
            "structural_status": "resolved",
            "canonical_event_id": canonical[0],
            "canonical_revision": canonical[1],
            "original_source_status": status,
            "reason": reason,
        })
    unresolved_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,revision,source_id,locator_json,target_json,reason "
            "FROM history_unresolved_refs ORDER BY event_id,revision,source_id,locator_json,reason"
        )
    ]
    unresolved.extend({
        "event_id": row["event_id"],
        "revision": _positive_revision(row["revision"]),
        "reason": str(row["reason"]),
    } for row in unresolved_rows)
    closure = {
        "status": "closed" if not unresolved and adapter_unresolved_count == 0 else "incomplete",
        "reference_count": len(references) + len(invalid_snapshot_refs),
        "structurally_resolved_count": structurally_resolved,
        "unresolved_count": len(unresolved),
        "adapter_unresolved_count": adapter_unresolved_count,
        "unresolved": unresolved,
        "references": source_results,
    }
    original = {
        "status": "not_assessed" if not references and not invalid_snapshot_refs else (
            "valid" if invalid == 0 and unresolved_original == 0 else "invalid"
        ),
        "valid_count": valid,
        "invalid_count": invalid,
        "unresolved_count": unresolved_original + len(invalid_snapshot_refs),
    }
    return closure, original


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
    by_native_id: dict[str, list[int]] = defaultdict(list)
    by_event_id: dict[str, list[int]] = defaultdict(list)
    for index, event in enumerate(events):
        if event.native_id is not None:
            by_native_id[event.native_id].append(index)
        by_event_id[event.event_id].append(index)
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
        candidate_indexes = set(by_native_id.get(target, ()))
        candidate_indexes.update(by_event_id.get(target, ()))
        matches = [
            events[index]
            for index in sorted(candidate_indexes)
            if events[index].kind in {"message", "edit"}
            and _scoped(events[index])[:3] == _scoped(delete)[:3]
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
    authority_events_by_row, authorities_by_event = _authority_candidate_index(
        events, authority_rows
    )
    curated_events_by_identity = _curated_event_index(events)

    # A delete is scoped by channel, account and chat. Unknown scope never guesses.
    denied_by_delete, unresolved = _normalize_delete_targets(events)
    unresolved.extend(auxiliary["unresolved"])
    source_revoked_events: set[int] = set()
    source_revocation_propagation_events: set[int] = set()
    for row, candidates in zip(authority_rows, authority_events_by_row):
        if row.get("revoked_at_ms") is None:
            continue
        matches = [
            event for event in candidates if _source_revocation_copy_matches(event, row)
        ]
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
        matches = curated_events_by_identity.get(key, [])
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
            event_authorities = authorities_by_event.get(id(event), [])
            proof, reason = _combined_authority(event, event_authorities)
            if proof is None:
                continue
            proof_id = _key(event.event_id, event.revision)
            source_revoked_for_copy = any(
                _key(row.get("event_id"), row.get("revision")) == proof_id
                and _source_revocation_copy_matches(event, row)
                for row in event_authorities
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
        input_receipt = _input_receipt(Path(collection))
        software_receipt = _source_software_receipt()
        decoder_receipt = _decoder_receipt(bridge_package_dir)
        report = {
            "schema": "history-rebuild-report-v1",
            "complete": True,
            "build_complete": True,
            "collection_complete": bool(adapter_report.get("complete_manifest")),
            "collection_verdict": adapter_report.get("collection_verdict"),
            "adapter_report": adapter_report,
            "input_complete": input_receipt["input_complete"],
            "input_receipt": input_receipt,
            "software_receipt": software_receipt,
            "decoder_receipt": decoder_receipt,
            "event_count": accepted,
            "copy_count": len(copies),
            "conflict_count": conflict_count,
            "denial_count": len(denials),
            "source_proof_count": len(source_proofs),
            "unresolved_count": len(unresolved_refs),
            "roster_input_count": roster_count,
            "roster_input_applied": False,
        }
        def finalize_report(
            connection: sqlite3.Connection, current_report: dict[str, Any]
        ) -> dict[str, Any]:
            semantic_records = _semantic_records_from_target(connection)
            semantic_receipt = _semantic_receipt(
                semantic_records,
                input_receipt=input_receipt,
                software_receipt=software_receipt,
                decoder_receipt=decoder_receipt,
                adapter_report=adapter_report,
                conflict_count=conflict_count,
            )
            return {**current_report, "semantic_receipt": semantic_receipt}

        report = journal.write_rebuild_records(
            event_details=event_details,
            copies=copies,
            aliases=aliases,
            source_proofs=source_proofs,
            denials=denials,
            unresolved_refs=unresolved_refs,
            name_observations=name_observations,
            report=report,
            finalize_report=finalize_report,
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
    """Verify a marked target, its semantic receipt, and supplied source references."""
    try:
        with HistoricalJournal(target_home, create=False) as journal:
            state = journal.rebuild_state()
            rows = journal.store._conn.execute(
                "SELECT event_id,revision FROM events"
            ).fetchall()
            resolved = {(str(row["event_id"]), int(row["revision"])) for row in rows}
            references, snapshot_receipt = (
                _read_snapshot_refs(knowledge_snapshot)
                if knowledge_snapshot is not None
                else ([], {"present": False, "source_row_count": 0, "distinct_reference_count": 0})
            )
            invalid_snapshot_refs = [
                reference for reference in references if reference.get("reason")
            ]
            valid_references = [
                reference for reference in references if not reference.get("reason")
            ]
            adapter_report = state.get("adapter_report")
            if not isinstance(adapter_report, dict):
                adapter_report = {}
            closure, original_validity = _resolve_snapshot_refs(
                journal.store._conn,
                valid_references,
                invalid_snapshot_refs=invalid_snapshot_refs,
                adapter_unresolved_count=int(adapter_report.get("unresolved_count") or 0),
            )
            input_receipt = state.get("input_receipt")
            if not isinstance(input_receipt, dict):
                input_receipt = {"input_complete": False, "sha256": None}
            input_receipt_valid = _input_receipt_valid(input_receipt)
            software_receipt = _source_software_receipt()
            software_receipt_valid = state.get("software_receipt") == software_receipt
            decoder_receipt = state.get("decoder_receipt")
            if not isinstance(decoder_receipt, dict):
                decoder_receipt = {"status": "missing_build_receipt"}
            expected_semantic = state.get("semantic_receipt")
            actual_semantic = _semantic_receipt(
                _semantic_records_from_target(journal.store._conn),
                input_receipt=input_receipt,
                software_receipt=software_receipt,
                decoder_receipt=decoder_receipt,
                adapter_report=adapter_report,
                conflict_count=int(state.get("conflict_count") or 0),
            )
            expected_hashes = (
                expected_semantic.get("hashes")
                if isinstance(expected_semantic, dict)
                else None
            )
            mismatched_tables = sorted(
                name
                for name in set(expected_hashes or {}) | set(actual_semantic["hashes"])
                if (expected_hashes or {}).get(name) != actual_semantic["hashes"].get(name)
            )
            semantic_valid = (
                isinstance(expected_semantic, dict)
                and expected_semantic.get("sha256") == actual_semantic["sha256"]
                and expected_semantic.get("counts") == actual_semantic["counts"]
                and expected_semantic.get("input_sha256") == actual_semantic["input_sha256"]
                and expected_semantic.get("normalization") == actual_semantic["normalization"]
                and expected_semantic.get("decoder") == actual_semantic["decoder"]
                and expected_semantic.get("diagnostics_sha256") == actual_semantic["diagnostics_sha256"]
                and software_receipt_valid
                and not mismatched_tables
            )
            actual_semantic.update(
                expected_sha256=(expected_semantic.get("sha256") if isinstance(expected_semantic, dict) else None),
                matches_target=semantic_valid,
                mismatched_tables=mismatched_tables,
            )
            unresolved_deletes = int(journal.store._conn.execute(
                "SELECT COUNT(*) FROM history_unresolved_refs WHERE reason LIKE 'delete_%'"
            ).fetchone()[0])
            integrity = str(journal.store._conn.execute("PRAGMA quick_check").fetchone()[0])
            build_complete = bool(state.get("build_complete", state.get("complete")))
            input_complete = bool(input_receipt.get("input_complete"))
            original_status = str(original_validity.get("status"))
            references_valid = original_status in {"valid", "not_assessed"}
            complete = (
                build_complete
                and input_complete
                and input_receipt_valid
                and software_receipt_valid
                and integrity == "ok"
                and semantic_valid
                and closure["status"] == "closed"
                and references_valid
            )
            report = {
                "schema": "history-verification-report-v1",
                "build_complete": build_complete,
                "input_complete": input_complete,
                "input_receipt_valid": input_receipt_valid,
                "input_receipt": input_receipt,
                "software_receipt_valid": software_receipt_valid,
                "semantic_receipt": actual_semantic,
                "semantic_receipt_valid": semantic_valid,
                "structural_reference_closure": {
                    **closure,
                    "snapshot_supplied": knowledge_snapshot is not None,
                    "snapshot": snapshot_receipt,
                },
                "original_source_validity": original_validity,
                "disclosure_permission": {
                    "status": "not_assessed",
                    "reason": "Verification does not evaluate current recipient authorization.",
                },
                "integrity": integrity,
                "event_count": len(resolved),
                "statement_source_count": snapshot_receipt.get("source_row_count", 0),
                "distinct_source_reference_count": snapshot_receipt.get("distinct_reference_count", 0),
                "resolved_source_count": closure["structurally_resolved_count"],
                "missing_source_refs": closure["unresolved"],
                "unresolved_delete_count": unresolved_deletes,
                "complete": complete,
            }
            return report
    except (HistoryTargetError, sqlite3.Error) as exc:
        raise HistoryRebuildError("history target failed verification") from exc
