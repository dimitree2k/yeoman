"""Read-only inventory of preserved Yeoman sources.

This module records structural metadata and locators only. Source bytes remain the
authority; message, policy, identity and credential values are never copied into
record metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import get_operational_store_path

_MONTHS = tuple(f"2026-{month:02d}" for month in range(1, 11))
_RESTRICTED_CLASSES = {"operational", "curated_state", "identity", "restricted", "unknown"}
_EXCLUDED_PARTS = {"secrets", "auth", "whatsapp-auth", ".ssh"}
_TIME_KEYS = {
    "original_time",
    "occurred_at",
    "occurred_at_ms",
    "occurred_ms",
    "timestamp",
    "timestamp_ms",
    "timestampms",
    "messagetimestamp",
    "sent_at",
    "event_time",
}
_CREATION_KEYS = {
    "creation_time",
    "created_at",
    "created_at_ms",
    "created_ms",
    "ingest_time",
    "ingest_time_ms",
    "ingested_at",
    "stored_at",
    "stored_at_ms",
    "storedatms",
}
_CHAT_KEYS = {"chat", "chat_id", "chatid", "chat_jid", "chatjid", "group_id", "remotejid"}
_ID_KEYS = {
    "nativeid",
    "native_id",
    "message_id",
    "messageid",
    "source_message_id",
    "sourcemessageid",
}
_TYPE_KEYS = {"record_type", "event_type", "eventtype", "kind", "type"}
_KEY_ORDER = (
    "original_time", "occurred_at", "occurred_at_ms", "occurred_ms", "timestamp", "timestamp_ms",
    "timestampms", "messagetimestamp", "sent_at", "event_time", "creation_time", "created_at",
    "created_at_ms", "created_ms", "ingest_time", "ingest_time_ms", "ingested_at", "stored_at",
    "stored_at_ms", "storedatms",
    "chat_id", "chatid", "chat_jid", "chatjid", "remotejid", "group_id", "chat", "nativeid", "native_id",
    "message_id", "messageid",
    "source_message_id", "sourcemessageid", "record_type", "event_type", "eventtype", "kind", "type",
)
_RECORD_LIST_KEYS = {"records", "events", "effects", "items", "messages", "entries", "rows"}
_TABLES_WITH_RECORD_METADATA = {"events", "effects", "thread_messages", "inbound_messages", "memory2_nodes"}
_STATIC_DB_MARKERS = (".bak", ".v1-", ".pre-", "walsafe", "before-")
_LIVE_DB_NAMES = {
    "processing.db",
    "reply_context.db",
    "archive.db",
    "chat_registry.db",
    "knowledge.db",
    "memory.db",
    "contacts.db",
    "relay.db",
    "speakups.db",
    "a2a-research.db",
}


def _ops_path(name: str, *parts: str) -> str:
    return (get_operational_store_path(name, data_dir=Path("data")) / Path(*parts)).as_posix()

# Safe operational path references verified from the effective runtime config.
# The config itself is intentionally never read or copied by this inventory.
_CONFIGURED_REFERENCES = (
    ("memory.dbPath", "data/memory/memory.db"),
    ("memory.wal.stateDir", "data/memory/session-state"),
    ("knowledge.dbPath", "data/knowledge/knowledge.db"),
    ("processing.dbPath", _ops_path("processing")),
    ("media.incoming.whatsapp", "var/media/incoming/whatsapp"),
    ("media.outgoing.whatsapp", "var/media/outgoing/whatsapp"),
)

_EXPECTED_SOURCES = (
    (_ops_path("bridge_references"), "tree", "native", False, "bridge TTL source"),
    (_ops_path("processing"), "live_sqlite", "normalized", False, "canonical journal"),
    ("data/processing/*.bak", "file", "normalized", False, "static processing database variants"),
    ("data/inbound/reply_context.db", "live_sqlite", "normalized", False, "reply context"),
    ("data/inbound/*.jsonl", "file", "normalized", False, "session JSONL pattern"),
    ("data/inbound/archive.db", "live_sqlite", "normalized", False, "empty archive candidate"),
    ("data/memory/memory.db", "live_sqlite", "derived", False, "legacy Memory"),
    ("data/memory/*.bak", "static_sqlite_triple", "derived", False, "legacy Memory database variants"),
    ("data/memory/backups", "tree", "derived", False, "legacy Memory backups"),
    ("data/memory/session-state", "tree", "derived", False, "session state"),
    ("data/knowledge/knowledge.db", "live_sqlite", "curated_state", True, "Knowledge and owner curation"),
    ("data/knowledge/*.v1-*", "static_sqlite_triple", "curated_state", True, "Knowledge versioned database variants"),
    ("data/knowledge/migration-manifest.json", "file", "curated_state", True, "migration manifest"),
    ("data/raw", "reference_only", "native", False, "raw archive by reference"),
    ("data/raw-spool", "reference_only", "native", False, "not included in cold collection"),
    ("data/raw/media", "tree", "media", False, "raw media"),
    (_ops_path("document_cache"), "live_sqlite", "media", False, "media cache database"),
    (_ops_path("speakups"), "live_sqlite", "operational", True, "speak-up state"),
    (_ops_path("burst_state"), "file", "operational", True, "consciousness burst state"),
    (_ops_path("lull_state"), "file", "operational", True, "consciousness lull state"),
    (_ops_path("pending_approvals"), "file", "operational", True, "speak-up approvals"),
    (_ops_path("cron"), "file", "operational", True, "scheduled jobs"),
    (_ops_path("response_pauses"), "file", "operational", True, "policy response pauses"),
    (_ops_path("persona_evolution"), "tree", "operational", True, "persona evolution"),
    ("var/media", "tree", "media", False, "media cache"),
    ("data/bridge/whatsapp-outbox/quarantine", "tree", "operational", True, "outbound quarantine"),
    ("data/contacts/contacts.db", "live_sqlite", "identity", True, "legacy contacts"),
    ("data/inbound/chat_registry.db", "live_sqlite", "identity", True, "chat registry"),
    ("seen_chats.json", "file", "identity", True, "frozen legacy seen chats"),
    ("data/seen_chats.json", "file", "identity", True, "frozen legacy seen chats"),
    (_ops_path("seen_chats"), "file", "identity", True, "recently seen chats"),
    (_ops_path("policy_audit"), "tree", "operational", True, "policy audit"),
    ("policy/audit", "tree", "operational", True, "policy admin audit"),
    (_ops_path("overseer"), "tree", "operational", True, "Overseer state and runbooks"),
    ("data/a2a/relay.db", "live_sqlite", "operational", True, "A2A relay"),
    ("data/speakups.db", "live_sqlite", "operational", True, "dead duplicate speak-up store"),
    ("data/processing/a2a-research.db", "live_sqlite", "operational", True, "A2A research"),
    ("backups", "tree", "operational", True, "root config and migration backups"),
    ("config.json", "file", "operational", True, "not included in cold collection"),
    ("policy.json", "file", "operational", True, "not included in cold collection"),
)


def inspect_source(*, path: Path, source_class: str) -> dict[str, Any]:
    """Inspect a file or tree without changing it or returning payload text."""
    path = Path(path)
    relative = path.name
    if _is_excluded(path):
        return _empty_source(relative, source_class, "excluded", "authentication path")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return _empty_source(relative, source_class, "missing", "path does not exist")
    except OSError as exc:
        return _empty_source(relative, source_class, "inaccessible", type(exc).__name__)
    if stat.S_ISLNK(mode):
        return _empty_source(relative, source_class, "incomplete", "symlink not followed")
    if stat.S_ISDIR(mode):
        return _inspect_tree(path, source_class)
    if not stat.S_ISREG(mode):
        return _empty_source(relative, source_class, "incomplete", "non-regular file")
    return _inspect_file(path, source_class, relative)


def inventory_sources(*, home: Path) -> dict[str, Any]:
    """Inventory expected and discovered sources beneath a cold Yeoman home."""
    home = Path(home).expanduser()
    sources: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    claimed: set[str] = set()

    # Inspect known homogeneous trees and databases first. Manifests sort before
    # archive files so no archive member is ever read before its manifest.
    present_roots = sorted(
        (
            (relative, kind, source_class, restricted, note)
            for relative, kind, source_class, restricted, note in _EXPECTED_SOURCES
            if kind == "tree" and (home / relative).exists()
        ),
        key=lambda item: (item[0].count("/"), len(item[0])),
    )
    for relative, kind, source_class, restricted, note in present_roots:
        root = home / relative
        if any(Path(relative).is_relative_to(Path(parent)) for parent in claimed):
            continue
        descriptor = _describe_source(
            root, home, source_class=source_class, restricted=restricted, kind=kind
        )
        descriptor["note"] = note
        sources.append(descriptor)
        claimed.add(relative)
        omissions.extend(descriptor.get("omissions", []))

    for relative, kind, source_class, restricted, note in sorted(
        (item for item in _EXPECTED_SOURCES if item[1] != "tree"),
        key=lambda item: ("manifest" not in item[0].lower(), item[0]),
    ):
        if "*" in relative:
            pattern = relative.rsplit("/", 1)[-1]
            parent = home / relative.rsplit("/", 1)[0]
            matches = sorted(parent.glob(pattern)) if parent.is_dir() else []
            if not matches:
                sources.append(_missing_descriptor(relative, kind, source_class, restricted, note))
            for match in matches:
                rel = match.relative_to(home).as_posix()
                if rel in claimed:
                    continue
                descriptor = _describe_source(
                    match, home, source_class=source_class, restricted=restricted, kind=_file_kind(match)
                )
                descriptor["note"] = note
                sources.append(descriptor)
                claimed.add(rel)
                claimed.update(_companion_paths(match, home))
            continue
        path = home / relative
        rel = Path(relative).as_posix()
        if path.exists() and rel not in claimed:
            descriptor = _describe_source(
                path, home, source_class=source_class, restricted=restricted, kind=_file_kind(path, kind)
            )
            descriptor["note"] = note
            sources.append(descriptor)
            claimed.add(rel)
            claimed.update(_companion_paths(path, home))
        elif not path.exists():
            sources.append(_missing_descriptor(relative, kind, source_class, restricted, note))

    # Add anything outside the named classes. Never descend through symlinks or
    # inspect credentials. SQLite sidecars are already represented with their DB.
    extra_files: list[Path] = []
    def record_walk_error(error: OSError) -> None:
        filename = getattr(error, "filename", None)
        relative = Path(filename).relative_to(home).as_posix() if filename else "."
        omissions.append({"path": relative, "reason": type(error).__name__})

    for directory, dirs, files in os.walk(home, followlinks=False, onerror=record_walk_error):
        current = Path(directory)
        rel_dir = current.relative_to(home).as_posix() if current != home else ""
        dirs[:] = sorted(
            name
            for name in dirs
            if not _is_excluded(current / name)
            and not (current / name).is_symlink()
            and not _is_claimed((Path(rel_dir) / name).as_posix(), claimed)
        )
        for name in sorted(files):
            path = current / name
            rel = path.relative_to(home).as_posix()
            if _is_excluded(path) or _is_claimed(rel, claimed):
                continue
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                omissions.append({"path": rel, "reason": type(exc).__name__})
                continue
            if stat.S_ISLNK(mode):
                omissions.append({"path": rel, "reason": "symlink not followed"})
                continue
            if not stat.S_ISREG(mode):
                omissions.append({"path": rel, "reason": "non-regular file"})
                continue
            extra_files.append(path)

    extra_files.sort(key=lambda item: ("manifest" not in item.name.casefold(), item.relative_to(home).as_posix()))
    for path in extra_files:
        rel = path.relative_to(home).as_posix()
        if _is_claimed(rel, claimed):
            continue
        source_class, restricted = _classify(rel)
        descriptor = _describe_source(path, home, source_class=source_class, restricted=restricted, kind=_file_kind(path))
        sources.append(descriptor)
        claimed.add(rel)
        claimed.update(_companion_paths(path, home))
        omissions.extend(descriptor.get("omissions", []))

    for relative, kind, source_class, restricted, _note in _EXPECTED_SOURCES:
        if "*" in relative:
            continue
        if not (home / relative).exists() and not any(item["path"] == relative for item in sources):
            sources.append(_missing_descriptor(relative, kind, source_class, restricted, "expected source absent"))

    sources.sort(key=lambda item: item["path"])
    records = [record for source in sources for record in source.get("records", [])]
    coverage = _coverage(sources, records)
    return {
        "schema_version": 1,
        "sources": sources,
        "configured_references": _configured_references(home),
        "coverage": coverage,
        "omissions": sorted(omissions, key=lambda item: (item.get("path", ""), item.get("reason", ""))),
        "summary": {
            "source_count": len(sources),
            "present_sources": sum(source["status"] in {"present", "empty", "partial"} for source in sources),
            "missing_sources": sum(source["status"] == "missing" for source in sources),
            "file_count": sum(len(source.get("files", [])) for source in sources),
            "record_count": len(records),
            "parse_error_count": sum(len(source.get("parse_errors", [])) for source in sources),
        },
    }


def _inspect_tree(path: Path, source_class: str) -> dict[str, Any]:
    relative_root = path.name
    files: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    status = "empty"
    paths: list[Path] = []
    def record_walk_error(error: OSError) -> None:
        filename = getattr(error, "filename", None)
        relative = Path(filename).relative_to(path).as_posix() if filename else "."
        omissions.append({"path": relative, "reason": type(error).__name__})

    for directory, dirs, names in os.walk(path, followlinks=False, onerror=record_walk_error):
        current = Path(directory)
        dirs[:] = sorted(name for name in dirs if not _is_excluded(current / name) and not (current / name).is_symlink())
        for name in names:
            item = current / name
            try:
                mode = item.lstat().st_mode
            except OSError as exc:
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": type(exc).__name__})
                continue
            if stat.S_ISLNK(mode):
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": "symlink not followed"})
            elif stat.S_ISREG(mode):
                paths.append(item)
            else:
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": "non-regular file"})
    paths.sort(key=lambda item: ("manifest" not in item.name.lower(), item.relative_to(path).as_posix()))
    for item in paths:
        rel = item.relative_to(path).as_posix()
        if _is_sqlite_sidecar(item):
            detail = _file_fingerprint(item)
            files.append({"path": rel, **detail, "kind": "file"})
            continue
        result = _inspect_file(item, source_class, rel)
        if result.get("files") and path.name.casefold() == "backups":
            result["files"][0]["kind"] = _file_kind(item, "static_sqlite_triple")
        files.extend(result.get("files", []))
        records.extend(result.get("records", []))
        tables.extend(result.get("tables", []))
        parse_errors.extend(result.get("parse_errors", []))
        omissions.extend(result.get("omissions", []))
        if result.get("status") not in {"empty", "missing"}:
            status = "present"
    size = sum(item.get("size_bytes", 0) for item in files)
    return {
        "path": relative_root,
        "kind": "tree",
        "source_class": source_class,
        "restricted": _is_restricted(source_class),
        "status": status,
        "size_bytes": size,
        "sha256": None,
        "files": files,
        "tables": tables,
        "records": records,
        "parse_errors": parse_errors,
        "omissions": omissions,
        "cursor": _cursor(records),
        "metadata_completeness": _completeness(records),
        "duplicates": _duplicates(records, files),
    }


def _inspect_reference_tree(path: Path) -> dict[str, Any]:
    """Inventory raw archive files by reference; never make a second archive copy."""
    files: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    def record_walk_error(error: OSError) -> None:
        filename = getattr(error, "filename", None)
        relative = Path(filename).relative_to(path).as_posix() if filename else "."
        omissions.append({"path": relative, "reason": type(error).__name__})

    for directory, dirs, names in os.walk(path, followlinks=False, onerror=record_walk_error):
        current = Path(directory)
        dirs[:] = sorted(
            name for name in dirs if name != "media" and not _is_excluded(current / name) and not (current / name).is_symlink()
        )
        for name in sorted(names):
            item = current / name
            try:
                mode = item.lstat().st_mode
            except OSError as exc:
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": type(exc).__name__})
                continue
            if stat.S_ISLNK(mode):
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": "symlink not followed"})
            elif stat.S_ISREG(mode):
                relative = item.relative_to(path).as_posix()
                detail = _inspect_file(item, "native", relative)
                files.extend(detail.get("files", []))
                records.extend(detail.get("records", []))
                parse_errors.extend(detail.get("parse_errors", []))
                omissions.extend(detail.get("omissions", []))
            else:
                omissions.append({"path": item.relative_to(path).as_posix(), "reason": "non-regular file"})
    return {
        "path": path.name,
        "kind": "reference_only",
        "source_class": "native",
        "restricted": False,
        "status": "present" if files else "empty",
        "size_bytes": sum(item.get("size_bytes", 0) for item in files),
        "sha256": None,
        "files": files,
        "tables": [],
        "records": records,
        "parse_errors": parse_errors,
        "omissions": omissions,
        "cursor": _cursor(records),
        "metadata_completeness": _completeness(records),
        "duplicates": _duplicates(records, files),
    }


def _inspect_file(path: Path, source_class: str, locator_path: str) -> dict[str, Any]:
    try:
        fingerprint = _file_fingerprint(path)
    except OSError as exc:
        return _empty_source(locator_path, source_class, "inaccessible", type(exc).__name__)
    result: dict[str, Any] = {
        "path": locator_path,
        "source_class": source_class,
        "restricted": _is_restricted(source_class),
        "status": "present",
        "size_bytes": fingerprint["size_bytes"],
        "sha256": fingerprint["sha256"],
        "files": [{"path": locator_path, **fingerprint, "kind": _file_kind(path)}],
        "tables": [],
        "records": [],
        "parse_errors": [],
        "omissions": [],
        "cursor": _cursor([]),
        "metadata_completeness": _completeness([]),
        "duplicates": {"native_id": [], "identical_hash": []},
    }
    if _is_sqlite_file(path):
        _inspect_sqlite(path, source_class, locator_path, result)
    elif _looks_like_json(path):
        _inspect_json(path, source_class, locator_path, result)
    elif path.suffix.lower() in {".md", ".markdown"}:
        _inspect_markdown(path, source_class, locator_path, result)
    elif _is_archive(path):
        result["archive"] = _inspect_archive(path)
        result["status"] = "partial" if result["archive"].get("error") else "present"
        if result["archive"].get("error"):
            result["omissions"].append({"path": locator_path, "reason": result["archive"]["error"]})
    result["cursor"] = _cursor(result["records"])
    result["metadata_completeness"] = _completeness(result["records"])
    result["duplicates"] = _duplicates(result["records"], result["files"])
    return result


def _inspect_sqlite(path: Path, source_class: str, locator_path: str, result: dict[str, Any]) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="yeoman-inventory-") as temporary:
            copy = Path(temporary) / path.name
            shutil.copyfile(path, copy)
            # The inventory operates on the cold collection, even when a source
            # descriptor's kind is ``live_sqlite`` for a future online collect.
            # Copy visible sidecars too so inspection sees the captured SQLite
            # state without allowing SQLite to touch the evidence originals.
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{path}{suffix}")
                if sidecar.is_file() and not sidecar.is_symlink():
                    shutil.copyfile(sidecar, Path(f"{copy}{suffix}"))
            uri = f"{copy.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            try:
                schema_rows = connection.execute(
                    "SELECT type, name, sql FROM sqlite_schema "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                for object_row in schema_rows:
                    name = str(object_row["name"])
                    quoted = _quote_identifier(name)
                    try:
                        columns = [
                            {
                                "name": str(row["name"]),
                                "type": str(row["type"] or ""),
                                "not_null": bool(row["notnull"]),
                                "primary_key_order": int(row["pk"]),
                            }
                            for row in connection.execute(f"PRAGMA table_info({quoted})")
                        ]
                        count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
                    except sqlite3.Error as exc:
                        result["parse_errors"].append({"table": name, "error": type(exc).__name__})
                        continue
                    table = {"name": name, "object_type": str(object_row["type"]), "columns": columns, "row_count": count}
                    table["cursor"] = _table_cursor(connection, name, columns)
                    result["tables"].append(table)
                    result["records"].extend(
                        _sqlite_record_metadata(connection, name, columns, source_class, locator_path)
                    )
            finally:
                connection.close()
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "partial"
        result["parse_errors"].append({"error": type(exc).__name__})


def _sqlite_record_metadata(
    connection: sqlite3.Connection,
    table: str,
    columns: list[dict[str, Any]],
    source_class: str,
    locator_path: str,
) -> list[dict[str, Any]]:
    if table.casefold() not in _TABLES_WITH_RECORD_METADATA or _is_restricted(source_class):
        return []
    names = [item["name"] for item in columns]
    primary = [item["name"] for item in sorted(columns, key=lambda item: item["primary_key_order"]) if item["primary_key_order"]]
    safe_columns = set(_TIME_KEYS | _CREATION_KEYS | _CHAT_KEYS | _ID_KEYS | _TYPE_KEYS)
    selected = [name for name in names if name.casefold() in safe_columns and not _is_restricted(source_class)]
    selected = list(dict.fromkeys(primary + selected))
    quoted = _quote_identifier(table)
    try:
        projection = ", ".join(["rowid AS __inventory_rowid", *(_quote_identifier(name) for name in selected)])
        rows = connection.execute(f"SELECT {projection} FROM {quoted}")
    except sqlite3.Error:
        # WITHOUT ROWID tables need their declared key for a locator.
        if not primary:
            return []
        projection = ", ".join(_quote_identifier(name) for name in selected)
        rows = connection.execute(f"SELECT {projection} FROM {quoted}")
    output: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, 1):
        values = {key.casefold(): row[key] for key in row.keys() if key != "__inventory_rowid"}
        record = _metadata(values, source_class, {"file": locator_path, "table": table, "row": row_number})
        if primary and not _is_restricted(source_class):
            record["locator"] = {
                "file": locator_path,
                "table": table,
                "primary_key_sha256": _primary_key_digest(locator_path, table, primary, values),
            }
        else:
            rowid = row["__inventory_rowid"] if "__inventory_rowid" in row.keys() else row_number
            record["locator"] = {"file": locator_path, "table": table, "row": int(rowid)}
        output.append(record)
    return output


def _inspect_json(path: Path, source_class: str, locator_path: str, result: dict[str, Any]) -> None:
    suffix = path.suffix.lower()
    records: list[tuple[int, dict[str, Any]]] = []
    if suffix in {".jsonl", ".ndjson"}:
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        result["parse_errors"].append({"line": line_number, "error": type(exc).__name__})
                        continue
                    records.extend((line_number, item) for item in _records(payload))
        except (OSError, UnicodeDecodeError) as exc:
            result["status"] = "partial"
            result["parse_errors"].append({"error": type(exc).__name__})
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = [(1, item) for item in _records(payload)]
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            result["status"] = "partial"
            line = getattr(exc, "lineno", 1)
            result["parse_errors"].append({"line": int(line), "error": type(exc).__name__})
    result["records"] = [
        _metadata(item, source_class, {"file": locator_path, "line": line}) for line, item in records
    ] if not _is_restricted(source_class) else []
    if result["parse_errors"]:
        result["status"] = "partial"


def _inspect_markdown(path: Path, source_class: str, locator_path: str, result: dict[str, Any]) -> None:
    if _is_restricted(source_class):
        return
    time_pattern = re.compile(r"\b20\d{2}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?")
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                match = time_pattern.search(line)
                if match:
                    result["records"].append(
                        {
                            "original_time": match.group(0),
                            "creation_time": None,
                            "chat": None,
                            "record_type": "markdown_line",
                            "native_id": None,
                            "locator": {"file": locator_path, "line": line_number},
                        }
                    )
    except (OSError, UnicodeDecodeError) as exc:
        result["status"] = "partial"
        result["parse_errors"].append({"error": type(exc).__name__})


def _metadata(record: dict[str, Any], source_class: str, locator: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5 or not isinstance(value, dict):
            return
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized not in flattened and not isinstance(item, (dict, list)):
                flattened[normalized] = item
            elif isinstance(item, dict):
                visit(item, depth + 1)

    visit(record)
    restricted = _is_restricted(source_class)
    original_time = None if restricted else _first(flattened, _TIME_KEYS)
    creation_time = None if restricted else _first(flattened, _CREATION_KEYS)
    chat = None if restricted else _first(flattened, _CHAT_KEYS)
    native_id = None if restricted else _first(flattened, _ID_KEYS)
    record_type = None if restricted else _first(flattened, _TYPE_KEYS)
    return {
        "original_time": _bounded_scalar(original_time),
        "creation_time": _bounded_scalar(creation_time),
        "chat": _bounded_scalar(chat),
        "record_type": _bounded_scalar(record_type),
        "native_id": _bounded_scalar(native_id),
        "locator": locator,
    }


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key, value in payload.items():
        if str(key).casefold() in _RECORD_LIST_KEYS and isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def _first(values: dict[str, Any], names: set[str]) -> Any:
    for name in sorted(names, key=lambda item: _KEY_ORDER.index(item) if item in _KEY_ORDER else len(_KEY_ORDER)):
        if values.get(name) is not None:
            return values[name]
    return None


def _coverage(sources: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[tuple[str, str, str]] = Counter()
    month_counts: Counter[str] = Counter()
    completeness: Counter[str] = Counter()
    for source in sources:
        for record in source.get("records", []):
            for key in ("original_time", "creation_time", "chat", "record_type", "native_id"):
                if record.get(key) is not None:
                    completeness[key] += 1
            month = _month(record.get("original_time")) or "unknown"
            month_counts[month] += 1
            chat = str(record.get("chat") or "unknown")
            counts[(str(source.get("path", "")), month, chat)] += 1
    months = {
        month: {"records": int(month_counts[month]), "status": "covered" if month_counts[month] else "empty"}
        for month in _MONTHS
    }
    return {
        "months": months,
        "by_source_month_chat": [
            {"source_path": source, "month": month, "chat": chat, "records": count}
            for (source, month, chat), count in sorted(counts.items())
        ],
        "unknown_dates": int(month_counts["unknown"]),
        "metadata_completeness": dict(completeness),
    }


def _cursor(records: list[dict[str, Any]]) -> dict[str, Any]:
    def latest(key: str) -> str | int | float | bool | None:
        valid = [
            (instant, _json_scalar(record.get(key)))
            for record in records
            if (instant := _normalized_instant(record.get(key))) is not None
        ]
        return max(valid, key=lambda item: item[0])[1] if valid else None

    return {
        "record_count": len(records),
        "latest_original_time": latest("original_time"),
        "latest_creation_time": latest("creation_time"),
    }


def _table_cursor(
    connection: sqlite3.Connection, table: str, columns: list[dict[str, Any]]
) -> dict[str, Any]:
    names = {str(item["name"]).casefold(): str(item["name"]) for item in columns}
    original = next((names[key] for key in _KEY_ORDER[:10] if key in names), None)
    created = next(
        (names[key] for key in _KEY_ORDER if key in _CREATION_KEYS and key in names), None
    )
    selected = list(dict.fromkeys(name for name in (original, created) if name))
    latest: dict[str, tuple[datetime, Any] | None] = {name: None for name in selected}
    if selected:
        projection = ", ".join(_quote_identifier(name) for name in selected)
        for row in connection.execute(f"SELECT {projection} FROM {_quote_identifier(table)}"):
            for index, name in enumerate(selected):
                instant = _normalized_instant(row[index])
                if instant is not None and (latest[name] is None or instant > latest[name][0]):
                    latest[name] = (instant, row[index])
    return {
        "original_time_column": original,
        "latest_original_time": _bounded_scalar(latest[original][1]) if original and latest[original] else None,
        "creation_time_column": created,
        "latest_creation_time": _bounded_scalar(latest[created][1]) if created and latest[created] else None,
    }


def _completeness(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(record.get(key) is not None for record in records)
        for key in ("original_time", "creation_time", "chat", "record_type", "native_id")
    }


def _duplicates(records: list[dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("native_id") is not None:
            by_id[str(record["native_id"])].append(record["locator"])
    return {
        "native_id": [
            {"native_id": native_id, "count": len(locators), "locators": locators}
            for native_id, locators in sorted(by_id.items())
            if len(locators) > 1
        ],
        "identical_hash": _hash_groups(files),
    }


def _hash_groups(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hash: dict[str, list[str]] = defaultdict(list)
    for item in files:
        digest = item.get("sha256")
        if digest:
            by_hash[str(digest)].append(str(item.get("path", "")))
    return [
        {"sha256": digest, "paths": sorted(paths)}
        for digest, paths in sorted(by_hash.items())
        if len(paths) > 1
    ]


def _describe_source(
    path: Path,
    home: Path,
    *,
    source_class: str,
    restricted: bool,
    kind: str,
) -> dict[str, Any]:
    rel = path.relative_to(home).as_posix()
    inspected = (
        _inspect_reference_tree(path)
        if kind == "reference_only" and path.is_dir() and path.name == "raw"
        else inspect_source(path=path, source_class=source_class)
    )
    files = inspected.get("files", [])
    if path.is_file() and _is_sqlite_file(path):
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_file() and not sidecar.is_symlink():
                files.append({
                    "path": sidecar.name,
                    **_file_fingerprint(sidecar),
                    "kind": "file",
                })
    return {
        "source_id": "src-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16],
        "path": rel,
        "kind": kind,
        "source_class": source_class,
        "restricted": restricted or _is_restricted(source_class),
        "status": inspected["status"],
        "size_bytes": inspected.get("size_bytes", sum(item.get("size_bytes", 0) for item in files)),
        "sha256": inspected.get("sha256"),
        "files": files,
        "tables": inspected.get("tables", []),
        "records": inspected.get("records", []),
        "parse_errors": inspected.get("parse_errors", []),
        "omissions": inspected.get("omissions", []),
        "cursor": inspected.get("cursor", _cursor([])),
        "metadata_completeness": inspected.get("metadata_completeness", _completeness([])),
        "duplicates": inspected.get("duplicates", {"native_id": [], "identical_hash": []}),
    }


def _missing_descriptor(path: str, kind: str, source_class: str, restricted: bool, note: str) -> dict[str, Any]:
    return {
        "source_id": "src-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16],
        "path": path,
        "kind": kind,
        "source_class": source_class,
        "restricted": restricted,
        "status": "missing",
        "reason": note,
        "size_bytes": 0,
        "sha256": None,
        "files": [],
        "tables": [],
        "records": [],
        "parse_errors": [],
        "omissions": [{"path": path, "reason": note}],
        "cursor": _cursor([]),
        "metadata_completeness": _completeness([]),
        "duplicates": {"native_id": [], "identical_hash": []},
    }


def _empty_source(path: str, source_class: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "path": path,
        "source_class": source_class,
        "restricted": _is_restricted(source_class),
        "status": status,
        "size_bytes": 0,
        "sha256": None,
        "files": [],
        "tables": [],
        "records": [],
        "parse_errors": [],
        "omissions": [{"path": path, "reason": reason}],
        "cursor": _cursor([]),
        "metadata_completeness": _completeness([]),
        "duplicates": {"native_id": [], "identical_hash": []},
    }


def _file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _configured_references(home: Path) -> list[dict[str, Any]]:
    return [
        {
            "setting": setting,
            "path": relative,
            "status": "present" if (home / relative).exists() else "not_in_cold_copy",
            "evidence": "verified config reference; config values omitted",
        }
        for setting, relative in _CONFIGURED_REFERENCES
    ]


def _classify(relative: str) -> tuple[str, bool]:
    lower = relative.casefold()
    if lower in {"config.json", "policy.json"}:
        return "operational", True
    if "/.git/" in f"/{lower}/":
        return "operational", True
    if lower.startswith(("data/raw/media/", "data/media/", "var/media/")):
        return "media", False
    if lower.startswith("data/raw/") or lower.startswith(
        ("data/bridge/whatsapp-message-references/", "data/ops/bridge-message-references/")
    ):
        return "native", False
    if lower == _ops_path("processing"):
        return "normalized", False
    if "whatsapp-outbox/quarantine" in lower or "/audit/" in f"/{lower}/":
        return "operational", True
    if lower.startswith("data/knowledge/"):
        return "curated_state", True
    if lower.startswith("data/contacts/") or "chat_registry.db" in lower or lower.endswith(
        ("seen_chats.json", "seen-chats.json")
    ):
        return "identity", True
    if (
        lower.startswith(
            ("data/ops/", "data/consciousness/", "data/overseer/", "data/policy/", "data/cron/", "data/persona-evolution/")
        )
        or "speakups.db" in lower
        or "a2a-research.db" in lower
    ):
        return "operational", True
    if lower.startswith("data/memory/"):
        return "derived", False
    if lower.startswith("data/processing/") or lower.startswith("data/inbound/"):
        return "normalized", False
    if lower.startswith("backups/") or lower.startswith("policy/"):
        return "operational", True
    return "unknown", False


def _file_kind(path: Path, default: str | None = None) -> str:
    if default in {"reference_only", "tree", "live_sqlite", "static_sqlite_triple"} and not _is_sqlite_file(path):
        return default if default == "reference_only" else "file"
    if _is_sqlite_file(path):
        name = path.name.casefold()
        if any(marker in name for marker in _STATIC_DB_MARKERS):
            return "static_sqlite_triple"
        if default == "static_sqlite_triple":
            return "static_sqlite_triple"
        if default == "live_sqlite":
            return "live_sqlite"
        return "live_sqlite" if path.name in _LIVE_DB_NAMES else "static_sqlite_triple"
    return "file"


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _is_sqlite_sidecar(path: Path) -> bool:
    return path.name.endswith(("-wal", "-shm")) and _is_sqlite_file(Path(path.as_posix()[:-4]))


def _companion_paths(path: Path, home: Path) -> set[str]:
    if not _is_sqlite_file(path):
        return set()
    return {
        Path(f"{path}{suffix}").relative_to(home).as_posix()
        for suffix in ("-wal", "-shm")
        if Path(f"{path}{suffix}").is_file()
    }


def _is_claimed(relative: str, claimed: set[str]) -> bool:
    path = Path(relative)
    return relative in claimed or any(path.is_relative_to(Path(parent)) for parent in claimed)


def _is_excluded(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return bool(parts & _EXCLUDED_PARTS) or path.name == ".env" or path.name.startswith(".env.")


def _is_restricted(source_class: str) -> bool:
    return source_class.casefold() in _RESTRICTED_CLASSES


def _looks_like_json(path: Path) -> bool:
    if path.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
        return True
    return False


def _is_archive(path: Path) -> bool:
    return path.suffix.lower() in {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z"}


def _inspect_archive(path: Path) -> dict[str, Any]:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                return {
                    "format": "zip",
                    "members": len(archive.infolist()),
                    "paths": [item.filename for item in archive.infolist()],
                    "extracted": False,
                }
        if tarfile.is_tarfile(path):
            with tarfile.open(path, mode="r:*") as archive:
                members = archive.getmembers()
                return {
                    "format": "tar",
                    "members": len(members),
                    "paths": [item.name for item in members],
                    "extracted": False,
                }
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return {"format": "unknown", "members": None, "paths": [], "extracted": False, "error": type(exc).__name__}
    return {
        "format": "unknown",
        "members": None,
        "paths": [],
        "extracted": False,
        "error": "unsupported or opaque archive format; manifest and member data not inspected",
    }


def _normalized_instant(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            if abs(number) > 100_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=UTC)
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,13}", text):
            return _normalized_instant(int(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _month(value: Any) -> str | None:
    instant = _normalized_instant(value)
    if instant is not None:
        return instant.strftime("%Y-%m")
    if isinstance(value, str):
        match = re.fullmatch(r"(20\d{2})-(\d{2})", value.strip())
        if match and 1 <= int(match.group(2)) <= 12:
            return f"{match.group(1)}-{match.group(2)}"
    return None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _primary_key_digest(
    locator_path: str, table: str, columns: list[str], values: dict[str, Any]
) -> str:
    typed_values: list[dict[str, Any]] = []
    for column in columns:
        value = values.get(column.casefold())
        if isinstance(value, bytes):
            encoded = value.hex()
            type_name = "blob"
        elif value is None:
            encoded = None
            type_name = "null"
        elif isinstance(value, bool):
            encoded = int(value)
            type_name = "integer"
        elif isinstance(value, int):
            encoded = value
            type_name = "integer"
        elif isinstance(value, float):
            encoded = value.hex()
            type_name = "real"
        else:
            encoded = str(value)
            type_name = "text"
        typed_values.append({"column": column, "type": type_name, "value": encoded})
    canonical = json.dumps(typed_values, ensure_ascii=False, separators=(",", ":"))
    payload = f"{locator_path}\0{table}\0{canonical}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_scalar(value: Any) -> str | int | float | bool | None:
    scalar = _json_scalar(value)
    if isinstance(scalar, str) and len(scalar) > 512:
        return None
    return scalar
