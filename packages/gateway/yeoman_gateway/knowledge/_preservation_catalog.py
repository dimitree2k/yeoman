"""Private, rebuildable catalog for immutable source-bundle manifests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ._preservation_inventory import _normalized_instant
from ._snapshot import SnapshotError
from ._source_bundles import (
    _collect_sources,
    _collection_lock,
    _descriptor,
    _fingerprint,
    _plan,
    _private_mkdir,
    _read_json,
    _reference_metadata,
    _reject_symlink_components,
    _remove_tree,
    _target_root,
    _write_json,
)

_CATALOG_NAME = "catalog.sqlite3"
_AUDIT_NAME = "purge-audit.jsonl"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_METADATA_FIELDS = ("original_time", "creation_time", "chat", "record_type", "native_id")


def refresh_collection(*, sources: list[dict[str, Any]], target_dir: Path) -> dict[str, Any]:
    """Acquire only changed explicit sources and incrementally index their manifests."""
    root = _target_root(Path(target_dir).expanduser())
    _private_mkdir(root)
    with _collection_lock(root):
        return _refresh_collection(sources=sources, target_dir=root)


def _refresh_collection(*, sources: list[dict[str, Any]], target_dir: Path) -> dict[str, Any]:
    """Refresh while the caller holds the per-collection lock."""
    if not isinstance(sources, list) or not sources:
        raise SnapshotError("sources_invalid", "sources must be a non-empty JSON descriptor list")
    root = _target_root(Path(target_dir).expanduser())
    _private_mkdir(root)
    catalog = root / _CATALOG_NAME
    if not catalog.exists():
        rebuild_catalog(target_dir=root)
    tombstones, pending_purges, purged_versions = _purge_state(root)
    latest = _latest_source_entries(root)
    changed: list[dict[str, Any]] = []
    reacquire: set[str] = set()
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise SnapshotError("sources_invalid", "each source descriptor must be an object")
        descriptor = _descriptor(source)
        source_id = descriptor["source_id"]
        if source_id in source_ids:
            raise SnapshotError("source_duplicate", "duplicate source ids are not allowed")
        source_ids.add(source_id)
        if any(item["source_id"] == source_id for item in pending_purges.values()):
            raise SnapshotError("purge_pending", "source purge cleanup is pending; retry purge before refresh")
        disposition = source.get("owner_disposition")
        if source_id in tombstones:
            if disposition != "reacquire_after_purge":
                raise SnapshotError("source_purged", "source is purged; explicit owner disposition is required")
            reacquire.add(source_id)
        elif disposition is not None:
            raise SnapshotError("owner_disposition_invalid", "no purge disposition is pending for this source")
        previous = latest.get(source_id)
        if source_id in reacquire or not _unchanged(descriptor, previous, root):
            changed.append(source)

    if not changed:
        return {
            "complete": all(_latest_complete(latest.get(source_id)) for source_id in source_ids),
            "refreshed": False,
            "bundle_dir": None,
            "manifest_path": None,
            "source_count": len(source_ids),
        }

    report = _collect_sources(
        sources=[{key: value for key, value in source.items() if key != "owner_disposition"} for source in changed],
        target_dir=root,
        _allow_purged_source_ids=reacquire,
    )
    manifest_path = Path(report["manifest_path"])
    payload = _read_json(manifest_path)
    acquired = {
        str(entry.get("source_id")): entry
        for entry in payload["sources"]
        if isinstance(entry, dict)
    }
    cleared: set[str] = set()
    for source_id in reacquire:
        if acquired.get(source_id, {}).get("status") in {"copied", "reference_only"}:
            _append_audit(root, source_id, "reacquire", str(os.getuid()), [])
            cleared.add(source_id)
    tombstones.difference_update(cleared)
    previous_success = _successful_source_ids(
        root, exclude=manifest_path, tombstones=tombstones, purged_versions=purged_versions
    )
    connection = _connect(root / _CATALOG_NAME)
    try:
        indexed = {
            str(row[0])
            for row in connection.execute("SELECT manifest_path FROM manifests").fetchall()
        }
    finally:
        connection.close()
    unindexed_prior = [
        path for path in _manifest_paths(root) if path != manifest_path and str(path) not in indexed
    ]
    if unindexed_prior:
        rebuild_catalog(target_dir=root)
    else:
        _index_manifest(root, manifest_path, previous_success, tombstones, purged_versions)
    latest = _latest_source_entries(root)
    return {
        "complete": all(_latest_complete(latest.get(source_id)) for source_id in source_ids),
        "refreshed": True,
        "bundle_dir": report["bundle_dir"],
        "manifest_path": report["manifest_path"],
        "source_count": len(source_ids),
    }


def rebuild_catalog(*, target_dir: Path) -> dict[str, Any]:
    """Rebuild the disposable SQLite index exclusively from finalized manifests."""
    root = _target_root(Path(target_dir).expanduser())
    _private_mkdir(root)
    catalog_path = root / _CATALOG_NAME
    _reject_symlink_components(catalog_path.absolute())
    tombstones, _, purged_versions = _purge_state(root)
    manifests = _manifest_paths(root)
    connection = _connect(catalog_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM records")
        connection.execute("DELETE FROM manifests")
        successful: set[str] = set()
        source_ids: set[str] = set()
        record_count = 0
        for manifest in manifests:
            payload = _read_json(manifest)
            if payload.get("bundle_format") != "yeoman-source-bundle":
                continue
            current = _index_payload(
                connection,
                manifest,
                payload,
                successful,
                tombstones,
                purged_versions,
            )
            source_ids.update(current[0])
            record_count += current[1]
            successful.update(current[2])
        connection.commit()
        return {
            "manifest_count": int(connection.execute("SELECT COUNT(*) FROM manifests").fetchone()[0]),
            "source_count": len(source_ids),
            "record_count": record_count,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        _secure_file(catalog_path)


def query_catalog(*, target_dir: Path, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Return exact metadata and locators without opening preserved source bytes."""
    root = _target_root(Path(target_dir).expanduser())
    _check_private_root(root)
    if not isinstance(filters, dict):
        raise SnapshotError("query_invalid", "filters must be an object")
    allowed = {
        "source_id", "chat", "record_type", "native_id", "original_after", "original_before",
        "creation_after", "creation_before", "unknown_dates",
    }
    if set(filters) - allowed:
        raise SnapshotError("query_invalid", "unsupported catalog filter")
    if "unknown_dates" in filters and not isinstance(filters["unknown_dates"], bool):
        raise SnapshotError("query_invalid", "unknown_dates must be a boolean")
    tombstones, _, purged_versions = _purge_state(root)
    catalog = root / _CATALOG_NAME
    _reject_symlink_components(catalog.absolute())
    if not catalog.is_file():
        raise SnapshotError("catalog_missing", "catalog is missing; run snapshot rebuild")
    bounds = {
        key: _bound_ms(filters[key])
        for key in ("original_after", "original_before", "creation_after", "creation_before")
        if filters.get(key) is not None
    }
    connection = sqlite3.connect(f"{catalog.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM records ORDER BY source_id, version_id, record_index"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SnapshotError("catalog_invalid", "catalog could not be read") from exc
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _public_row(row)
        if item["source_id"] in tombstones or (item["source_id"], item["version_id"]) in purged_versions:
            continue
        if any(item.get(key) != filters[key] for key in ("source_id", "chat", "record_type", "native_id") if key in filters):
            continue
        has_date_filter = bool(bounds) or "unknown_dates" in filters
        if has_date_filter and not item["metadata_record"]:
            continue
        original_ms = item.pop("_original_ms")
        creation_ms = item.pop("_creation_ms")
        if "unknown_dates" in filters and bool(filters["unknown_dates"]) != (original_ms is None):
            continue
        if not _within(original_ms, "original", bounds) or not _within(creation_ms, "creation", bounds):
            continue
        item.pop("metadata_record")
        result.append(item)
    return result


def purge_collection(
    *, target_dir: Path, source_id: str, operator: str, confirmed: bool = False
) -> dict[str, Any]:
    """Preview or purge every immutable bundle containing one source id."""
    root = _target_root(Path(target_dir).expanduser())
    if not root.is_dir():
        return _purge_collection(
            target_dir=root, source_id=source_id, operator=operator, confirmed=confirmed
        )
    _check_private_root(root)
    with _collection_lock(root):
        return _purge_collection(
            target_dir=root, source_id=source_id, operator=operator, confirmed=confirmed
        )


def _purge_collection(
    *, target_dir: Path, source_id: str, operator: str, confirmed: bool = False
) -> dict[str, Any]:
    """Preview or purge while the caller holds the per-collection lock."""
    if not _ID.fullmatch(str(source_id)) or ".." in str(source_id):
        raise SnapshotError("source_id_invalid", "source id must be a safe single path component")
    if not hasattr(os, "getuid") or str(operator) != str(os.getuid()):
        raise SnapshotError("owner_uid_required", "purge operator must be the current local owner UID")
    root = _target_root(Path(target_dir).expanduser())
    if not root.is_dir():
        raise SnapshotError("target_missing", "private collection root does not exist")
    _check_private_root(root)
    _, pending_purges, _ = _purge_state(root)
    affected: dict[str, Path] = {}
    retained_ids: set[str] = set()
    for manifest in _manifest_paths(root):
        payload = _read_json(manifest)
        entries = [entry for entry in payload.get("sources", []) if isinstance(entry, dict)]
        if any(entry.get("source_id") == source_id for entry in entries):
            affected[manifest.parent.name] = manifest.parent
            retained_ids.update(
                str(entry["source_id"])
                for entry in entries
                if isinstance(entry.get("source_id"), str) and entry.get("source_id") != source_id
            )
    for version_id, bundle in _staged_source_bundles(root, source_id).items():
        affected[version_id] = bundle
        manifest = bundle / "manifest.json"
        if manifest.is_file():
            payload = _read_json(manifest)
            retained_ids.update(
                str(entry["source_id"])
                for entry in payload["sources"]
                if isinstance(entry, dict)
                and isinstance(entry.get("source_id"), str)
                and entry.get("source_id") != source_id
            )
    pending_for_source = {
        purge_id: item
        for purge_id, item in pending_purges.items()
        if item["source_id"] == source_id
    }
    affected_versions = set(affected)
    for item in pending_for_source.values():
        affected_versions.update(item["versions"])
    affected_paths = [
        affected.get(version)
        or _version_path(root, version)
        for version in sorted(affected_versions)
    ]
    preview = {
        "source_id": source_id,
        "affected_bundles": [str(path) for path in affected_paths],
        "affected_versions": sorted(affected_versions),
        "affected_source_ids": [source_id] if affected_versions else [],
        "retained_source_ids": sorted(retained_ids),
        "requires_confirmation": not confirmed,
        "purged": False,
        "pending_cleanup": bool(pending_for_source),
    }
    if not confirmed:
        return preview

    purge_ids = list(pending_for_source)
    purge_versions_by_id = {
        purge_id: item["versions"] for purge_id, item in pending_for_source.items()
    }
    already_pending_versions = {
        version for item in pending_for_source.values() for version in item["versions"]
    }
    new_versions = sorted(affected_versions - already_pending_versions)
    if not purge_ids or new_versions:
        purge_id = uuid.uuid4().hex
        purge_ids.append(purge_id)
        versions_to_record = new_versions if pending_for_source else sorted(affected_versions)
        if not pending_for_source and not versions_to_record:
            versions_to_record = []
        purge_versions_by_id[purge_id] = versions_to_record
        _append_audit(
            root,
            source_id,
            "purge_started",
            str(os.getuid()),
            [],
            versions=versions_to_record,
            purge_id=purge_id,
        )
    try:
        for version in sorted(affected_versions):
            _purge_source_version(root, version, source_id)
    except OSError as exc:
        raise SnapshotError(
            "purge_cleanup_pending", "purge cleanup is incomplete; retry the confirmed purge"
        ) from exc
    rebuild_catalog(target_dir=root)
    for purge_id in purge_ids:
        _append_audit(
            root,
            source_id,
            "purge_completed",
            str(os.getuid()),
            [],
            versions=purge_versions_by_id[purge_id],
            purge_id=purge_id,
        )
    preview.update(requires_confirmation=False, purged=True)
    preview["pending_cleanup"] = False
    return preview


def _connect(path: Path) -> sqlite3.Connection:
    _reject_symlink_components(path.absolute())
    if path.exists():
        info = path.stat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SnapshotError("target_owner_invalid", "catalog belongs to another local owner")
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise SnapshotError("target_permissions", "cannot make catalog private") from exc
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS manifests ("
            "manifest_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS records ("
            "manifest_path TEXT NOT NULL, version_id TEXT NOT NULL, source_id TEXT NOT NULL,"
            "status TEXT NOT NULL, source_path TEXT, source_class TEXT, restricted INTEGER NOT NULL,"
            "record_index INTEGER NOT NULL, metadata_record INTEGER NOT NULL, original_time TEXT,"
            "original_ms REAL, creation_time TEXT, creation_ms REAL, chat TEXT, record_type TEXT,"
            "native_id TEXT, locator_json TEXT NOT NULL, row_count INTEGER,"
            "acquisition_started_ms INTEGER, acquisition_finished_ms INTEGER,"
            "PRIMARY KEY (manifest_path, source_id, record_index));"
            "CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_id);"
            "CREATE INDEX IF NOT EXISTS idx_records_chat_type_native ON records(chat, record_type, native_id);"
            "CREATE INDEX IF NOT EXISTS idx_records_original_time ON records(original_ms);"
            "CREATE INDEX IF NOT EXISTS idx_records_creation_time ON records(creation_ms);"
        )
        connection.commit()
        os.chmod(path, 0o600)
        return connection
    except Exception:
        connection.close()
        raise


def _index_manifest(
    root: Path,
    manifest: Path,
    previous_success: set[str],
    tombstones: set[str],
    purged_versions: set[tuple[str, str]],
) -> int:
    connection = _connect(root / _CATALOG_NAME)
    try:
        payload = _read_json(manifest)
        connection.execute("BEGIN IMMEDIATE")
        count = _index_payload(
            connection, manifest, payload, previous_success, tombstones, purged_versions
        )[1]
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        _secure_file(root / _CATALOG_NAME)


def _index_payload(
    connection: sqlite3.Connection,
    manifest: Path,
    payload: dict[str, Any],
    previous_success: set[str],
    tombstones: set[str],
    purged_versions: set[tuple[str, str]],
) -> tuple[set[str], int, set[str]]:
    manifest_key = str(manifest)
    if payload.get("bundle_format") != "yeoman-source-bundle":
        return set(), 0, set()
    connection.execute("DELETE FROM records WHERE manifest_path = ?", (manifest_key,))
    connection.execute("DELETE FROM manifests WHERE manifest_path = ?", (manifest_key,))
    digest = _fingerprint(manifest)
    connection.execute("INSERT INTO manifests VALUES (?, ?)", (manifest_key, digest))
    inserted_ids: set[str] = set()
    successful: set[str] = set()
    record_count = 0
    version_id = manifest.parent.name
    for entry in payload.get("sources", []):
        if not isinstance(entry, dict):
            continue
        source_id = str(entry.get("source_id", ""))
        if (
            not _ID.fullmatch(source_id)
            or source_id in tombstones
            or (source_id, version_id) in purged_versions
        ):
            continue
        inserted_ids.add(source_id)
        status = str(entry.get("status", "unknown"))
        if status == "incomplete" and entry.get("reason_code") == "source_missing" and source_id in previous_success:
            status = "source_gone"
        if status in {"copied", "reference_only", "excluded"}:
            successful.add(source_id)
        records = _entry_records(entry)
        for index, record in enumerate(records):
            _insert_record(connection, manifest_key, version_id, source_id, status, entry, index, record)
            record_count += 1
    return inserted_ids, record_count, successful


def _entry_records(entry: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    record_metadata = entry.get("record_metadata")
    restricted = bool(entry.get("restricted")) or entry.get("source_class") in {
        "operational", "curated_state", "identity", "restricted", "unknown",
    }
    if (
        isinstance(record_metadata, dict)
        and entry.get("status") in {"copied", "reference_only"}
        and not restricted
    ):
        records = record_metadata.get("records", [])
        if isinstance(records, list):
            result.extend(
                _safe_record(record, metadata_record=True)
                for record in records
                if isinstance(record, dict)
            )
    # Structural fallback keeps legacy manifests and restricted bundles searchable.
    for relative, info in sorted(entry.get("copied_files", {}).items()):
        safe = _safe_relative(str(relative))
        if safe is not None:
            result.append(
                {
                    "record_type": "file",
                    "locator": {"file": PurePosixPath("sources", str(entry["source_id"]), safe).as_posix()},
                    "metadata_record": False,
                }
            )
    for file_path, summary in _sqlite_summaries(entry):
        for table, count in sorted(summary.get("counts", {}).items()):
            file = _safe_relative(file_path)
            if file is None or not isinstance(table, str):
                continue
            result.append(
                {
                    "record_type": "sqlite_table",
                    "locator": {
                        "file": PurePosixPath("sources", str(entry["source_id"]), file).as_posix(),
                        "table": table,
                    },
                    "row_count": int(count),
                    "metadata_record": False,
                }
            )
    if not result:
        result.append({"record_type": "source", "locator": {}, "metadata_record": False})
    return result


def _safe_record(record: dict[str, Any], *, metadata_record: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"metadata_record": metadata_record}
    for field in _METADATA_FIELDS:
        value = record.get(field)
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, str) or (
            isinstance(value, str) and len(value) <= 512
        ):
            result[field] = value
        else:
            result[field] = None
    result["locator"] = _safe_locator(record.get("locator"))
    return result


def _safe_locator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("file", "table", "line", "row", "primary_key_sha256"):
        item = value.get(key)
        if key == "file" and isinstance(item, str) and _safe_relative(item) is not None:
            result[key] = item
        elif key == "table" and isinstance(item, str) and len(item) <= 512:
            result[key] = item
        elif key in {"line", "row"} and isinstance(item, int) and item >= 0:
            result[key] = item
        elif key == "primary_key_sha256" and isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item
    return result


def _sqlite_summaries(entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    summary = entry.get("sqlite")
    if not isinstance(summary, dict):
        return []
    if "counts" in summary and isinstance(summary.get("counts"), dict):
        main = entry.get("sqlite_main_file")
        if not isinstance(main, str):
            copied = entry.get("copied_files", {})
            candidates = [
                name for name in copied
                if str(name).endswith((".db", ".sqlite", ".sqlite3")) or str(name) == "data.db"
            ]
            main = candidates[0] if len(candidates) == 1 else None
        return [(main, summary)] if isinstance(main, str) else []
    return [
        (str(name), value)
        for name, value in summary.items()
        if isinstance(name, str) and isinstance(value, dict) and isinstance(value.get("counts"), dict)
    ]


def _insert_record(
    connection: sqlite3.Connection,
    manifest_path: str,
    version_id: str,
    source_id: str,
    status: str,
    entry: dict[str, Any],
    record_index: int,
    record: dict[str, Any],
) -> None:
    locator = _safe_locator(record.get("locator"))
    original = record.get("original_time")
    creation = record.get("creation_time")
    values = [
        manifest_path,
        version_id,
        source_id,
        status,
        str(entry.get("source_path", "")),
        str(entry.get("source_class", "")),
        int(bool(entry.get("restricted"))),
        record_index,
        int(bool(record.get("metadata_record"))),
        _scalar_text(original),
        _instant_ms(original),
        _scalar_text(creation),
        _instant_ms(creation),
        _scalar_text(record.get("chat")),
        _scalar_text(record.get("record_type")),
        _scalar_text(record.get("native_id")),
        json.dumps(locator, sort_keys=True, separators=(",", ":")),
        int(record["row_count"]) if isinstance(record.get("row_count"), int) else None,
        entry.get("acquisition_started_ms"),
        entry.get("acquisition_finished_ms"),
    ]
    connection.execute(
        "INSERT OR REPLACE INTO records VALUES (" + ",".join("?" for _ in values) + ")",
        values,
    )


def _public_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_id": str(row["source_id"]),
        "status": str(row["status"]),
        "source_path": str(row["source_path"] or ""),
        "source_class": str(row["source_class"] or ""),
        "version_id": str(row["version_id"]),
        "original_time": row["original_time"],
        "_original_ms": row["original_ms"],
        "creation_time": row["creation_time"],
        "_creation_ms": row["creation_ms"],
        "chat": row["chat"],
        "record_type": row["record_type"],
        "native_id": row["native_id"],
        "locator": json.loads(row["locator_json"]),
        "row_count": row["row_count"],
        "acquisition_started_ms": row["acquisition_started_ms"],
        "acquisition_finished_ms": row["acquisition_finished_ms"],
        "metadata_record": bool(row["metadata_record"]),
    }


def _manifest_paths(root: Path) -> list[Path]:
    versions = root / "versions"
    if not versions.exists():
        return []
    _reject_symlink_components(versions.absolute())
    if versions.is_symlink() or not versions.is_dir():
        raise SnapshotError("versions_invalid", "collection versions path is invalid")
    paths = []
    for manifest in sorted(versions.glob("*/manifest.json")):
        _reject_symlink_components(manifest.absolute())
        if manifest.is_file() and not manifest.is_symlink():
            paths.append(manifest)
    return sorted(paths, key=_manifest_order)


def _manifest_order(path: Path) -> tuple[int, int, str]:
    payload = _read_json(path)
    finished = payload.get("collection_finished_ms", payload.get("collection_started_ms", 0))
    finished_ms = int(finished) if isinstance(finished, (int, float)) else 0
    return finished_ms, path.stat().st_mtime_ns, path.parent.name


def _latest_source_entries(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for manifest in _manifest_paths(root):
        payload = _read_json(manifest)
        if payload.get("bundle_format") != "yeoman-source-bundle":
            continue
        for entry in payload["sources"]:
            if isinstance(entry, dict) and isinstance(entry.get("source_id"), str):
                latest[entry["source_id"]] = entry
    return latest


def _successful_source_ids(
    root: Path,
    *,
    exclude: Path | None = None,
    tombstones: set[str] | None = None,
    purged_versions: set[tuple[str, str]] | None = None,
) -> set[str]:
    result: set[str] = set()
    tombstones = tombstones or set()
    purged_versions = purged_versions or set()
    for manifest in _manifest_paths(root):
        if exclude is not None and manifest == exclude:
            continue
        payload = _read_json(manifest)
        for entry in payload.get("sources", []):
            if (
                isinstance(entry, dict)
                and entry.get("source_id") not in tombstones
                and (entry.get("source_id"), manifest.parent.name) not in purged_versions
                and entry.get("status") in {"copied", "reference_only", "excluded"}
            ):
                result.add(str(entry["source_id"]))
    return result


def _latest_complete(entry: dict[str, Any] | None) -> bool:
    return bool(entry and entry.get("status") in {"copied", "reference_only", "excluded"})


def _unchanged(descriptor: dict[str, Any], previous: dict[str, Any] | None, root: Path) -> bool:
    if previous is None:
        return False
    for key in ("source_id", "kind", "source_class", "restricted"):
        if descriptor[key] != previous.get(key):
            return False
    if str(descriptor["path"]) != previous.get("source_path"):
        return False
    if descriptor.get("cursor") != previous.get("cursor"):
        return False
    try:
        plan = _plan(descriptor, root)
    except (OSError, SnapshotError):
        return False
    if plan["status"] != "ready":
        return (
            previous.get("status") == "incomplete"
            and previous.get("reason_code") == plan.get("reason")
        )
    if plan.get("reference_only"):
        if previous.get("status") != "reference_only":
            return False
        _, file_stats = _reference_metadata(descriptor)
        return previous.get("reference_file_stats") == file_stats
    if previous.get("status") != "copied" or "record_metadata" not in previous:
        return False
    if descriptor["kind"] == "live_sqlite":
        old = previous.get("source_components", {})
        for item in plan["files"]:
            if item["relative"].endswith("-shm"):
                continue
            prior = old.get(item["relative"])
            if not prior or _fingerprint(item["path"]) != prior.get("sha256"):
                return False
        return True
    old_files = previous.get("copied_files", {})
    current = {item["relative"]: item for item in plan["files"]}
    if set(old_files) != set(current):
        return False
    return all(_fingerprint(item["path"]) == old_files[name].get("source_sha256") for name, item in current.items())


def _purge_state(
    root: Path,
) -> tuple[set[str], dict[str, dict[str, Any]], set[tuple[str, str]]]:
    audit = root / _AUDIT_NAME
    if not audit.exists():
        return set(), {}, set()
    _reject_symlink_components(audit.absolute())
    if audit.is_symlink() or not audit.is_file():
        raise SnapshotError("purge_audit_invalid", "purge audit path is invalid")
    info = audit.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SnapshotError("target_owner_invalid", "purge audit belongs to another local owner")
    if info.st_mode & 0o077:
        raise SnapshotError("target_permissions", "purge audit must be private")
    active: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    purged_versions: set[tuple[str, str]] = set()
    try:
        for index, line in enumerate(audit.read_text(encoding="utf-8").splitlines()):
            event = json.loads(line)
            if not isinstance(event, dict) or not _ID.fullmatch(str(event.get("source_id", ""))):
                raise ValueError("invalid audit row")
            source_id = event["source_id"]
            action = event.get("action")
            if action in {"purge_started", "purge"}:
                purge_id = event.get("purge_id")
                if action == "purge":
                    # Older audit rows stored bundle paths but no explicit version list.
                    purge_id = f"legacy-{index}"
                    bundles = event.get("bundles", [])
                    if not isinstance(bundles, list) or any(not isinstance(value, str) for value in bundles):
                        raise ValueError("invalid legacy purge bundles")
                    versions = [Path(value).name for value in bundles]
                else:
                    versions = event.get("versions")
                if not isinstance(purge_id, str) or not purge_id or len(purge_id) > 128:
                    raise ValueError("invalid purge id")
                if not isinstance(versions, list) or any(
                    not isinstance(version, str) or not _ID.fullmatch(version) or ".." in version
                    for version in versions
                ):
                    raise ValueError("invalid purge versions")
                if purge_id in pending:
                    raise ValueError("duplicate purge id")
                pending[purge_id] = {"source_id": source_id, "versions": sorted(set(versions))}
                purged_versions.update((source_id, version) for version in versions)
                active.add(source_id)
            elif action == "purge_completed":
                purge_id = event.get("purge_id")
                item = pending.get(purge_id)
                if not item or item["source_id"] != source_id:
                    raise ValueError("completion without purge start")
                versions = event.get("versions")
                if not isinstance(versions, list) or any(not isinstance(v, str) for v in versions):
                    raise ValueError("invalid purge completion versions")
                if sorted(set(versions)) != item["versions"]:
                    raise ValueError("purge completion version mismatch")
                del pending[purge_id]
            elif event.get("action") == "reacquire":
                if not any(item["source_id"] == source_id for item in pending.values()):
                    active.discard(source_id)
            else:
                raise ValueError("invalid audit action")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotError("purge_audit_invalid", "purge audit is unreadable or malformed") from exc
    return active, pending, purged_versions


def _guard_collection(root: Path, source_ids: list[str], allow_reacquire: set[str]) -> None:
    tombstones, pending, _ = _purge_state(root)
    for source_id in source_ids:
        if any(item["source_id"] == source_id for item in pending.values()):
            raise SnapshotError("purge_pending", "source purge cleanup is pending; retry purge first")
        if source_id in tombstones and source_id not in allow_reacquire:
            raise SnapshotError("source_purged", "source is purged; use the explicit refresh disposition")


def _append_audit(
    root: Path,
    source_id: str,
    action: str,
    operator_uid: str,
    bundles: list[str],
    affected_source_ids: list[str] | None = None,
    *,
    versions: list[str] | None = None,
    purge_id: str | None = None,
) -> None:
    audit = root / _AUDIT_NAME
    _reject_symlink_components(audit.absolute())
    if audit.exists() and (audit.is_symlink() or not audit.is_file()):
        raise SnapshotError("purge_audit_invalid", "purge audit path is invalid")
    created = not audit.exists()
    event = {
        "action": action,
        "source_id": source_id,
        "operator_uid": operator_uid,
        "at_ms": int(time.time() * 1000),
        "bundles": bundles,
    }
    if affected_source_ids is not None:
        event["affected_source_ids"] = affected_source_ids
    if versions is not None:
        event["versions"] = versions
    if purge_id is not None:
        event["purge_id"] = purge_id
    fd = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        dirfd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)


def _purge_source_version(root: Path, version: str, source_id: str) -> None:
    if not _ID.fullmatch(version) or ".." in version:
        raise SnapshotError("bundle_path_invalid", "refusing an invalid purged version id")
    staging = version.startswith("staging-")
    key = version.removeprefix("staging-") if staging else version
    if staging and not re.fullmatch(r"[a-f0-9]{64}", key):
        raise SnapshotError("bundle_path_invalid", "refusing an invalid staged bundle id")
    bundle_root = root / (".staging" if staging else "versions")
    versions_root = bundle_root.resolve()
    bundle = bundle_root / key
    _reject_symlink_components(bundle.absolute())
    if bundle.is_symlink() or not bundle.resolve().is_relative_to(versions_root):
        raise SnapshotError("bundle_path_invalid", "refusing to purge a bundle outside versions")
    if not bundle.exists():
        return
    if not bundle.is_dir():
        raise SnapshotError("bundle_path_invalid", "purged bundle path is not a directory")
    manifest = bundle / "manifest.json"
    _reject_symlink_components(manifest.absolute())
    if manifest.exists():
        payload = _read_json(manifest)
        entries = payload.get("sources", [])
        if not isinstance(entries, list):
            raise SnapshotError("manifest_invalid", "source bundle manifest has no source list")
    else:
        payload = {"sources": []}
        entries = []

    source_dir = bundle / "sources" / source_id
    _reject_symlink_components(source_dir.absolute())
    _remove_tree(source_dir)
    if staging:
        partial_dir = bundle / "sources" / f".{source_id}.partial"
        _reject_symlink_components(partial_dir.absolute())
        _remove_tree(partial_dir)
    remaining = [
        entry
        for entry in entries
        if not isinstance(entry, dict) or entry.get("source_id") != source_id
    ]
    if staging:
        if len(remaining) != len(entries):
            payload["sources"] = remaining
            payload["complete"] = False
            _write_json(manifest, payload)
        return
    if not remaining:
        _remove_tree(bundle)
        return
    if len(remaining) != len(entries):
        payload["sources"] = remaining
        payload["complete"] = all(
            isinstance(entry, dict)
            and entry.get("status") in {"copied", "reference_only", "excluded"}
            for entry in remaining
        )
        _write_json(manifest, payload)


def _staged_source_bundles(root: Path, source_id: str) -> dict[str, Path]:
    staging_root = root / ".staging"
    if not staging_root.exists():
        return {}
    _reject_symlink_components(staging_root.absolute())
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise SnapshotError("bundle_path_invalid", "staging path is not a directory")
    result: dict[str, Path] = {}
    for bundle in sorted(staging_root.iterdir()):
        _reject_symlink_components(bundle.absolute())
        if bundle.is_symlink() or not bundle.is_dir():
            raise SnapshotError("bundle_path_invalid", "staging contains a non-directory entry")
        manifest = bundle / "manifest.json"
        present = False
        if manifest.exists():
            payload = _read_json(manifest)
            present = any(
                isinstance(entry, dict) and entry.get("source_id") == source_id
                for entry in payload["sources"]
            )
        sources = bundle / "sources"
        if sources.exists():
            _reject_symlink_components(sources.absolute())
            present = present or (sources / source_id).exists() or (
                sources / f".{source_id}.partial"
            ).exists()
        if present:
            result[f"staging-{bundle.name}"] = bundle
    return result


def _version_path(root: Path, version: str) -> Path:
    if version.startswith("staging-"):
        return root / ".staging" / version.removeprefix("staging-")
    return root / "versions" / version


def _check_private_root(root: Path) -> None:
    _reject_symlink_components(root.absolute())
    if root.is_symlink() or not root.is_dir():
        raise SnapshotError("target_invalid", "private collection root is not a directory")
    info = root.stat()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SnapshotError("target_owner_invalid", "private collection directory has another owner")
    if info.st_mode & 0o077:
        raise SnapshotError("target_permissions", "private collection directory must use mode 0700")


def _secure_file(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        path.chmod(0o600)


def _safe_relative(value: str) -> str | None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        return None
    return path.as_posix()


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 512:
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, bool):
        return str(value)
    return None


def _instant_ms(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if abs(number) > 100_000_000_000 else number * 1000
    parsed = _normalized_instant(value)
    return parsed.timestamp() * 1000 if parsed is not None else None


def _bound_ms(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise SnapshotError("query_bound_invalid", "time bounds must be aware ISO instants or numeric epochs")
    if isinstance(value, (int, float)):
        return _instant_ms(value)  # type: ignore[return-value]
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return _instant_ms(float(text))  # type: ignore[return-value]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("query_bound_invalid", "time bounds must be aware ISO instants or numeric epochs") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotError("query_bound_invalid", "time bounds must include a timezone")
    return parsed.astimezone(UTC).timestamp() * 1000


def _within(value: float | None, kind: str, bounds: dict[str, float]) -> bool:
    after = bounds.get(f"{kind}_after")
    before = bounds.get(f"{kind}_before")
    return not ((after is not None and (value is None or value < after)) or (before is not None and (value is None or value > before)))
