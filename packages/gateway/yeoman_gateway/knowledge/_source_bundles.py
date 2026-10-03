"""Lossless, versioned acquisition of explicitly named historical sources.

Only structural metadata enters manifests. Raw archives stay reference-only, SQLite
content is never projected into rows, and credential paths are excluded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from ._snapshot import SnapshotError, _backup, _fingerprint

SOURCE_BUNDLE_MANIFEST_VERSION = 2
_KINDS = {"file", "tree", "live_sqlite", "static_sqlite_triple", "reference_only"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CREDENTIAL_PARTS = {"secrets", "credentials", "auth", "authentication", "keys", "private-keys"}
_KEY_SUFFIXES = {".pem", ".key", ".p8", ".p12", ".pfx"}


def collect_sources(*, sources: list[dict[str, Any]], target_dir: Path) -> dict[str, Any]:
    """Acquire one immutable version from explicit descriptors; never guess source paths."""
    if not isinstance(sources, list) or not sources:
        raise SnapshotError("sources_invalid", "sources must be a non-empty JSON descriptor list")
    descriptors = [_descriptor(item) for item in sources]
    ids = [item["source_id"] for item in descriptors]
    if len(ids) != len(set(ids)):
        raise SnapshotError("source_duplicate", "duplicate source ids are not allowed")
    descriptors.sort(key=lambda item: item["source_id"])

    root = _target_root(Path(target_dir).expanduser())
    plans = []
    for item in descriptors:
        try:
            plans.append(_plan(item, root))
        except SnapshotError as exc:
            if exc.code in {"source_symlink", "source_path_invalid", "source_destination_overlap"}:
                raise
            plans.append(
                {"descriptor": item, "status": "incomplete", "reason": exc.code, "files": []}
            )
        except OSError:
            plans.append(
                {"descriptor": item, "status": "incomplete", "reason": "source_unreadable", "files": []}
            )
    required = sum(
        sum(info["size_bytes"] for info in plan["files"]) + _inspection_bytes(plan)
        for plan in plans
    )
    probe = root if root.exists() else root.parent
    while not probe.exists():
        probe = probe.parent
    if shutil.disk_usage(probe).free < required:
        raise SnapshotError("capacity_insufficient", "collection target lacks required free space")

    _private_mkdir(root)
    stage_key = hashlib.sha256(
        json.dumps(
            [
                {
                    "source_id": item["source_id"],
                    "path": str(item["path"]),
                    "kind": item["kind"],
                    "source_class": item["source_class"],
                    "restricted": item["restricted"],
                    "cursor": item.get("cursor"),
                }
                for item in descriptors
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    staging = root / ".staging" / stage_key
    _private_mkdir(staging.parent)
    _private_mkdir(staging)
    manifest_path = staging / "manifest.json"

    if manifest_path.exists():
        payload = _read_json(manifest_path)
        if payload.get("staging_key") != stage_key:
            raise SnapshotError("staging_mismatch", "staged collection does not match these sources")
    else:
        payload = {
            "source_bundle_manifest_version": SOURCE_BUNDLE_MANIFEST_VERSION,
            "bundle_format": "yeoman-source-bundle",
            "staging_key": stage_key,
            "collection_started_ms": _now_ms(),
            "complete": False,
            "sources": [],
        }
        _write_json(manifest_path, payload)

    previous = {entry.get("source_id"): entry for entry in payload["sources"]}
    entries: list[dict[str, Any]] = []
    _private_mkdir(staging / "sources")
    for plan in plans:
        descriptor = plan["descriptor"]
        source_id = descriptor["source_id"]
        entry = previous.get(source_id)
        if entry and entry.get("status") == "copied" and _entry_matches(staging, entry, plan):
            entries.append(entry)
            continue
        if plan["status"] != "ready":
            entry = _not_collected_entry(plan)
            entries.append(entry)
            payload["sources"] = entries.copy()
            _write_json(manifest_path, payload)
            continue
        if plan["reference_only"]:
            entry = _reference_entry(plan)
            entries.append(entry)
            payload["sources"] = entries.copy()
            _write_json(manifest_path, payload)
            continue

        started = _now_ms()
        temporary = staging / "sources" / f".{source_id}.partial"
        destination = staging / "sources" / source_id
        _remove_tree(temporary)
        _remove_tree(destination)
        _private_mkdir(temporary)
        entry = {
            "source_id": source_id,
            "source_path": str(descriptor["path"]),
            "kind": descriptor["kind"],
            "source_class": descriptor["source_class"],
            "restricted": descriptor["restricted"],
            "status": "acquiring",
            "acquisition_started_ms": started,
            "cursor": descriptor.get("cursor"),
        }
        payload["sources"] = [*entries, entry]
        _write_json(manifest_path, payload)
        try:
            copied_files, sqlite_summary, source_components = _acquire(plan, temporary)
            _check_unchanged(plan)
        except Exception as exc:
            _remove_tree(temporary)
            entry.update(
                status="incomplete",
                reason_code=_reason(exc),
                acquisition_finished_ms=_now_ms(),
            )
            entries.append(entry)
            payload["sources"] = entries.copy()
            _write_json(manifest_path, payload)
            continue
        os.replace(temporary, destination)
        sqlite_main_file = None
        if descriptor["kind"] == "live_sqlite":
            sqlite_main_file = "data.db"
        elif descriptor["kind"] == "static_sqlite_triple":
            sqlite_main_file = descriptor["path"].name
        entry.update(
            status="copied",
            consistency={
                "live_sqlite": "sqlite_online_backup",
                "static_sqlite_triple": "static_byte_copy; coherent boundary unverified",
            }.get(descriptor["kind"], "streamed_byte_copy"),
            copied_files=copied_files,
            source_components=source_components,
            sqlite=sqlite_summary,
            sqlite_main_file=sqlite_main_file,
            file_stats=_jsonl_stats(destination, copied_files),
            source_sidecar_note=(
                "SQLite read-only WAL access may update shared-memory coordination bytes"
                if descriptor["kind"] == "live_sqlite"
                else ""
            ),
            acquisition_finished_ms=_now_ms(),
        )
        entries.append(entry)
        payload["sources"] = entries.copy()
        _write_json(manifest_path, payload)

    payload["sources"] = entries
    payload["complete"] = all(
        entry.get("status") in {"copied", "reference_only", "excluded"} for entry in entries
    )
    payload["collection_finished_ms"] = _now_ms()
    _write_json(manifest_path, payload)
    versions = root / "versions"
    _private_mkdir(versions)
    version = versions / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:12]}"
    os.replace(staging, version)
    final_manifest = version / "manifest.json"
    return {
        "complete": payload["complete"],
        "bundle_dir": str(version),
        "manifest_path": str(final_manifest),
        "source_count": len(entries),
    }


def verify_source_bundle(*, manifest: Path, restore_dir: Path | None = None) -> dict[str, Any]:
    """Verify exact bundle bytes and structural SQLite metadata on isolated copies."""
    path = Path(manifest).expanduser()
    payload = _read_json(path)
    if payload.get("source_bundle_manifest_version") != SOURCE_BUNDLE_MANIFEST_VERSION:
        raise SnapshotError("manifest_invalid", "manifest is not a source bundle v2")
    bundle = path.parent
    if restore_dir is None:
        with tempfile.TemporaryDirectory(prefix="yeoman-source-verify-") as scratch:
            return _verify_into(bundle, payload, Path(scratch) / "restore", persist=False)
    target = _target_root(Path(restore_dir).expanduser())
    if _overlap(target, bundle):
        raise SnapshotError("source_destination_overlap", "restore directory overlaps the source bundle")
    _private_mkdir(target)
    if any(target.iterdir()):
        raise SnapshotError("restore_target_not_empty", "restore directory must be empty")
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target))
    try:
        report = _verify_into(bundle, payload, staging, persist=True)
        if report["verdict"] != "ok":
            report["restored_sources"] = {}
            return report
        staged_sources = staging / "sources"
        if staged_sources.exists():
            os.replace(staged_sources, target / "sources")
            report["restored_sources"] = {
                source_id: str(target / "sources" / source_id)
                for source_id in report["restored_sources"]
            }
        return report
    finally:
        _remove_tree(staging)


def _verify_into(bundle: Path, payload: dict[str, Any], target: Path, *, persist: bool) -> dict[str, Any]:
    _private_mkdir(target)
    errors: list[str] = []
    restored: dict[str, str] = {}
    entries = payload.get("sources", [])
    if any(not isinstance(entry, dict) for entry in entries):
        raise SnapshotError("manifest_invalid", "source bundle entry is not an object")
    ids = [entry.get("source_id") for entry in entries]
    if any(not isinstance(source_id, str) or not _SAFE_ID.fullmatch(source_id) for source_id in ids):
        raise SnapshotError("manifest_invalid", "source bundle contains an invalid source id")
    if len(ids) != len(set(ids)):
        raise SnapshotError("manifest_invalid", "source bundle contains duplicate ids")
    expected_roots = {str(entry["source_id"]) for entry in entries if entry.get("status") == "copied"}
    sources_root = bundle / "sources"
    if sources_root.exists():
        _reject_symlink_components(sources_root)
        actual_roots = {path.name for path in sources_root.iterdir()}
        if actual_roots != expected_roots:
            errors.append("bundle_layout_mismatch")
    elif expected_roots:
        errors.append("bundle_layout_mismatch")

    for entry in entries:
        source_id = entry.get("source_id")
        status = entry.get("status")
        if status in {"reference_only", "excluded"}:
            continue
        if status != "copied" or not _SAFE_ID.fullmatch(str(source_id or "")):
            errors.append("incomplete_source")
            continue
        root = bundle / "sources" / str(source_id)
        output = target / "sources" / str(source_id)
        _private_mkdir(output)
        valid = True
        copied_files = entry.get("copied_files", {})
        if not isinstance(copied_files, dict):
            raise SnapshotError("manifest_invalid", "source bundle file list is invalid")
        if _bundle_files(root) != set(copied_files):
            errors.append("bundle_layout_mismatch")
            continue
        for rel, info in copied_files.items():
            if not isinstance(info, dict):
                raise SnapshotError("manifest_invalid", "source bundle file metadata is invalid")
            relative = PurePosixPath(str(rel))
            if relative.is_absolute() or ".." in relative.parts or "\\" in str(rel):
                valid = False
                break
            packed = root.joinpath(*relative.parts)
            _reject_symlink_components(packed)
            if not packed.is_file() or _fingerprint(packed) != info.get("copied_sha256"):
                valid = False
                break
            restored_file = output.joinpath(*relative.parts)
            _private_mkdir(restored_file.parent)
            _copy_file(packed, restored_file)
            if _fingerprint(restored_file) != info.get("copied_sha256"):
                valid = False
                break
        if not valid:
            errors.append("bundle_hash_mismatch")
            _remove_tree(output)
            continue
        restored[source_id] = str(output)
        expected_sqlite = entry.get("sqlite") or {}
        actual_sqlite = _inspect_sqlite_files(
            output,
            entry.get("copied_files", {}),
            main_file=entry.get("sqlite_main_file"),
        )
        if actual_sqlite != expected_sqlite:
            errors.append("sqlite_structure_mismatch")
    if not payload.get("complete"):
        errors.append("collection_incomplete")
    return {
        "verdict": "ok" if not errors else "failed",
        "errors": sorted(set(errors)),
        "restored_sources": restored if persist else {},
        "source_count": len(payload.get("sources", [])),
    }


def _descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError("sources_invalid", "each source descriptor must be an object")
    source_id = str(value.get("source_id", ""))
    if not _SAFE_ID.fullmatch(source_id) or ".." in source_id:
        raise SnapshotError("source_id_invalid", "source id must be a safe single path component")
    if not isinstance(value.get("path"), (str, os.PathLike)):
        raise SnapshotError("source_path_invalid", f"source {source_id} has no path")
    kind = str(value.get("kind", ""))
    if kind not in _KINDS:
        raise SnapshotError("source_kind_invalid", f"source {source_id} has an unsupported kind")
    if not isinstance(value.get("source_class"), str) or not isinstance(value.get("restricted"), bool):
        raise SnapshotError("source_descriptor_invalid", f"source {source_id} lacks classification")
    raw_path = Path(value["path"]).expanduser()
    if ".." in raw_path.parts:
        raise SnapshotError("source_path_invalid", "source path traversal is not allowed")
    path = raw_path.absolute()
    cursor = value.get("cursor", value.get("acquisition_cursor"))
    try:
        json.dumps(cursor)
    except (TypeError, ValueError) as exc:
        raise SnapshotError("source_cursor_invalid", "source cursor must be JSON serializable") from exc
    return {
        "source_id": source_id,
        "path": path,
        "kind": kind,
        "source_class": value["source_class"],
        "restricted": value["restricted"],
        "cursor": cursor,
    }


def _target_root(path: Path) -> Path:
    if ".." in path.parts:
        raise SnapshotError("target_path_invalid", "target path traversal is not allowed")
    absolute = path.absolute()
    _reject_symlink_components(absolute)
    resolved = absolute.resolve(strict=False)
    if _inside_protected_raw(resolved.parts):
        raise SnapshotError(
            "target_protected",
            "target cannot be inside protected raw or raw-spool storage",
        )
    if absolute.is_symlink() or (absolute.exists() and not absolute.is_dir()):
        raise SnapshotError("target_invalid", "target must be a real directory")
    return resolved


def _plan(descriptor: dict[str, Any], target: Path) -> dict[str, Any]:
    path: Path = descriptor["path"]
    _reject_symlink_components(path)
    if _overlap(path, target):
        raise SnapshotError("source_destination_overlap", f"source {descriptor['source_id']} overlaps target")
    if _credential_path(path) or descriptor["source_class"].lower() in {
        "auth",
        "authentication",
        "credential",
        "credentials",
    }:
        return {"descriptor": descriptor, "status": "excluded", "reason": "restricted_path_excluded", "files": []}
    raw = descriptor["source_class"].lower() in {"raw", "raw_archive", "raw-spool"}
    raw = raw or _contains_raw_component(path.parts)
    if descriptor["kind"] == "reference_only" or raw:
        if not path.exists():
            return {"descriptor": descriptor, "status": "incomplete", "reason": "source_missing", "files": []}
        return {"descriptor": descriptor, "status": "ready", "reference_only": True, "files": []}
    if not path.exists():
        return {"descriptor": descriptor, "status": "incomplete", "reason": "source_missing", "files": []}
    if not _readable_mode(path, directory=path.is_dir()):
        return {"descriptor": descriptor, "status": "incomplete", "reason": "source_unreadable", "files": []}
    kind = descriptor["kind"]
    files: list[dict[str, Any]] = []
    if kind == "live_sqlite":
        if not path.is_file():
            return {"descriptor": descriptor, "status": "incomplete", "reason": "source_not_file", "files": []}
        files.append(_file_info(path, path.name))
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{path}{suffix}")
            _reject_symlink_components(companion)
            if companion.exists():
                files.append(_file_info(companion, companion.name))
    elif kind == "static_sqlite_triple":
        if not path.is_file():
            return {"descriptor": descriptor, "status": "incomplete", "reason": "source_not_file", "files": []}
        files.append(_file_info(path, path.name))
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{path}{suffix}")
            _reject_symlink_components(companion)
            if companion.exists():
                files.append(_file_info(companion, companion.name))
    elif kind == "file":
        if not path.is_file():
            return {"descriptor": descriptor, "status": "incomplete", "reason": "source_not_file", "files": []}
        files.append(_file_info(path, path.name))
    elif kind == "tree":
        if not path.is_dir():
            return {"descriptor": descriptor, "status": "incomplete", "reason": "source_not_directory", "files": []}
        files = _tree_files(path)
    return {"descriptor": descriptor, "status": "ready", "reference_only": False, "files": files}


def _file_info(path: Path, relative: str) -> dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not _readable_mode(path):
        raise SnapshotError("source_unreadable", "source contains an unreadable or non-regular file")
    if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        raise SnapshotError("source_path_invalid", "source member path is unsafe")
    return {"path": path, "relative": relative, "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def _inspection_bytes(plan: dict[str, Any]) -> int:
    if plan.get("status") != "ready":
        return 0
    files = {item["relative"]: item["size_bytes"] for item in plan["files"]}
    if plan["descriptor"]["kind"] in {"live_sqlite", "static_sqlite_triple"}:
        return sum(files.values())
    size = 0
    for relative, length in files.items():
        if Path(relative).suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            size += length + files.get(f"{relative}-wal", 0) + files.get(f"{relative}-shm", 0)
    return size


def _tree_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    def inaccessible(error: OSError) -> None:
        raise SnapshotError("source_unreadable", "source tree contains an inaccessible path") from error

    for directory, dirnames, filenames in os.walk(
        root, topdown=True, onerror=inaccessible, followlinks=False
    ):
        base = Path(directory)
        for name in list(dirnames):
            child = base / name
            if _contains_raw_component((name,)):
                raise SnapshotError(
                    "raw_path_requires_reference",
                    "tree contains raw content; describe it separately as reference_only",
                )
            _reject_symlink_components(child)
            if not child.is_dir() or not _readable_mode(child, directory=True):
                raise SnapshotError("source_unreadable", "source tree contains an inaccessible directory")
            if _credential_path(child):
                dirnames.remove(name)
        for name in filenames:
            child = base / name
            _reject_symlink_components(child)
            if _credential_path(child):
                continue
            relative = child.relative_to(root).as_posix()
            if _contains_raw_component(PurePosixPath(relative).parts):
                raise SnapshotError(
                    "raw_path_requires_reference",
                    "tree contains raw content; describe it separately as reference_only",
                )
            files.append(_file_info(child, relative))
    return sorted(files, key=lambda item: item["relative"])


def _acquire(
    plan: dict[str, Any], temporary: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    descriptor = plan["descriptor"]
    kind = descriptor["kind"]
    copied: dict[str, Any] = {}
    sqlite_sources: list[tuple[str, Path]] = []
    source_components: dict[str, dict[str, Any]] = {}
    if kind == "live_sqlite":
        source = descriptor["path"]
        for info in plan["files"]:
            source_components[info["relative"]] = {
                "sha256": _fingerprint(info["path"]),
                "size_bytes": info["size_bytes"],
                "mtime_ns": info["mtime_ns"],
            }
        target = temporary / "data.db"
        _backup(source, target)
        target.chmod(0o600)
        copied["data.db"] = {
            "source_sha256": source_components[source.name]["sha256"],
            "copied_sha256": _fingerprint(target),
            "size_bytes": target.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
        }
        sqlite_sources.append(("data.db", target))
    else:
        for info in plan["files"]:
            source: Path = info["path"]
            relative = info["relative"]
            target = temporary.joinpath(*PurePosixPath(relative).parts)
            _private_mkdir(target.parent)
            source_hash = _copy_file(source, target)
            copied[relative] = {
                "source_sha256": source_hash,
                "copied_sha256": _fingerprint(target),
                "size_bytes": target.stat().st_size,
                "source_mtime_ns": info["mtime_ns"],
            }
            if kind == "static_sqlite_triple":
                source_components[relative] = {
                    "sha256": source_hash,
                    "size_bytes": info["size_bytes"],
                    "mtime_ns": info["mtime_ns"],
                }
            if kind == "static_sqlite_triple" and relative == descriptor["path"].name:
                sqlite_sources.append((relative, target))
            elif kind == "tree" and target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                sqlite_sources.append((relative, target))
            elif kind == "file" and target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                sqlite_sources.append((relative, target))
    sqlite_summary = _inspect_sqlite_sources(sqlite_sources, temporary)
    return copied, sqlite_summary, source_components


def _inspect_sqlite_sources(
    sources: list[tuple[str, Path]], scratch_dir: Path | None = None
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix=".sqlite-inspect-", dir=scratch_dir) as scratch:
        temporary = Path(scratch)
        for index, (name, path) in enumerate(sorted(sources)):
            db_copy = temporary / f"{index}-{path.name}"
            _copy_file(path, db_copy)
            for suffix in ("-wal", "-shm"):
                companion = Path(f"{path}{suffix}")
                if companion.is_file() and not companion.is_symlink():
                    _copy_file(companion, Path(f"{db_copy}{suffix}"))
            summaries[name] = _inspect_sqlite(db_copy)
    return next(iter(summaries.values())) if len(summaries) == 1 else summaries


def _inspect_sqlite_files(
    root: Path, copied_files: dict[str, Any], *, main_file: str | None = None
) -> dict[str, Any]:
    paths: dict[str, Path] = {}
    if main_file is not None:
        if not isinstance(main_file, str):
            raise SnapshotError("manifest_invalid", "SQLite main-file locator is invalid")
        relative = PurePosixPath(main_file)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in main_file
            or main_file not in copied_files
        ):
            raise SnapshotError("manifest_invalid", "SQLite main-file locator is invalid")
        paths[main_file] = root.joinpath(*relative.parts)
    for relative in copied_files:
        p = PurePosixPath(str(relative))
        if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or str(relative) == "data.db":
            paths[str(relative)] = root.joinpath(*p.parts)
    return _inspect_sqlite_sources(list(paths.items()))


def _inspect_sqlite(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            objects = connection.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            counts: dict[str, int] = {}
            schemas: dict[str, list[str]] = {}
            for name, kind in objects:
                table = str(name)
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")]
                schemas[table] = columns
                if kind == "table":
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            return {"schemas": schemas, "counts": counts}
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SnapshotError("sqlite_inspection_failed", "copied SQLite source is unreadable") from exc


def _jsonl_stats(root: Path, copied_files: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for relative in copied_files:
        if Path(relative).suffix.lower() not in {".jsonl", ".ndjson"}:
            continue
        lines = malformed = 0
        with root.joinpath(*PurePosixPath(relative).parts).open("rb") as source:
            for raw in source:
                if not raw.strip():
                    continue
                lines += 1
                try:
                    json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    malformed += 1
        result[str(relative)] = {"json_lines": lines, "malformed_json_lines": malformed}
    if len(result) == 1:
        return next(iter(result.values()))
    return result


def _not_collected_entry(plan: dict[str, Any]) -> dict[str, Any]:
    descriptor = plan["descriptor"]
    if plan["status"] == "excluded":
        token = hashlib.sha256(descriptor["source_id"].encode()).hexdigest()[:12]
        return {"source_id": f"excluded-{token}", "status": "excluded", "reason_code": plan["reason"]}
    return {
        "source_id": descriptor["source_id"],
        "source_path": str(descriptor["path"]),
        "kind": descriptor["kind"],
        "source_class": descriptor["source_class"],
        "restricted": descriptor["restricted"],
        "status": "incomplete",
        "reason_code": plan["reason"],
        "acquisition_started_ms": _now_ms(),
        "acquisition_finished_ms": _now_ms(),
        "cursor": descriptor.get("cursor"),
    }


def _reference_entry(plan: dict[str, Any]) -> dict[str, Any]:
    descriptor = plan["descriptor"]
    info = descriptor["path"].stat()
    return {
        "source_id": descriptor["source_id"],
        "source_path": str(descriptor["path"]),
        "kind": descriptor["kind"],
        "source_class": descriptor["source_class"],
        "restricted": descriptor["restricted"],
        "status": "reference_only",
        "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else None,
        "source_mtime_ns": info.st_mtime_ns,
        "content_read": False,
        "acquisition_started_ms": _now_ms(),
        "acquisition_finished_ms": _now_ms(),
        "cursor": descriptor.get("cursor"),
    }


def _entry_matches(staging: Path, entry: dict[str, Any], plan: dict[str, Any]) -> bool:
    if not entry.get("copied_files"):
        return False
    base = staging / "sources" / str(entry.get("source_id", ""))
    try:
        _reject_symlink_components(base)
        bundle_ok = all(
            not PurePosixPath(rel).is_absolute()
            and ".." not in PurePosixPath(rel).parts
            and "\\" not in rel
            and _fingerprint(base.joinpath(*PurePosixPath(rel).parts)) == info.get("copied_sha256")
            for rel, info in entry["copied_files"].items()
        )
        if not bundle_ok:
            return False
        current_files = {item["relative"]: item for item in plan["files"]}
        if plan["descriptor"]["kind"] == "live_sqlite":
            previous = entry.get("source_components", {})
            if set(previous) != set(current_files):
                return False
            return all(
                name.endswith("-shm")
                or (
                    current["size_bytes"] == previous[name].get("size_bytes")
                    and current["mtime_ns"] == previous[name].get("mtime_ns")
                    and _fingerprint(current["path"]) == previous[name].get("sha256")
                )
                for name, current in current_files.items()
            )
        previous = entry["copied_files"]
        return set(previous) == set(current_files) and all(
            current["size_bytes"] == previous[name].get("size_bytes")
            and current["mtime_ns"] == previous[name].get("source_mtime_ns")
            and _fingerprint(current["path"]) == previous[name].get("source_sha256")
            for name, current in current_files.items()
        )
    except (OSError, SnapshotError, TypeError, KeyError, AttributeError):
        return False


def _bundle_files(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        return set()
    files: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in dirnames:
            if (base / name).is_symlink():
                raise SnapshotError("source_symlink", "bundle symlinks are not allowed")
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise SnapshotError("source_symlink", "bundle contains a non-regular file")
            files.add(path.relative_to(root).as_posix())
    return files


def _check_unchanged(plan: dict[str, Any]) -> None:
    for info in plan["files"]:
        if plan["descriptor"]["kind"] == "live_sqlite" and info["relative"].endswith("-shm"):
            continue
        current = info["path"].stat()
        if current.st_size != info["size_bytes"] or current.st_mtime_ns != info["mtime_ns"]:
            raise SnapshotError("source_changed", "source changed during collection")


def _copy_file(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as input_file, target.open("xb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
            output_file.write(chunk)
        output_file.flush()
        os.fsync(output_file.fileno())
    target.chmod(0o600)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    _reject_symlink_components(path.absolute())
    if not path.is_file() or path.is_symlink():
        raise SnapshotError("manifest_missing", "source bundle manifest does not exist")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError("manifest_invalid", "source bundle manifest is invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise SnapshotError("manifest_invalid", "source bundle manifest has no source list")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _private_mkdir(path: Path) -> None:
    pending: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise SnapshotError("target_invalid", "private collection path contains a symlink")
        pending.append(current)
        if current.parent == current:
            raise SnapshotError("target_invalid", "cannot find parent for private collection path")
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise SnapshotError("target_invalid", "private collection path is not a directory")
    for directory in reversed(pending):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _private_directory(directory)
    _private_directory(path)


def _private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError("target_invalid", "private collection path is not a directory")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise SnapshotError("target_owner_invalid", "private collection directory has another owner")
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise SnapshotError("target_permissions", "cannot make collection directory private") from exc


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotError("source_symlink", "symlink source paths are not allowed")


def _overlap(source: Path, target: Path) -> bool:
    source_abs = source.absolute()
    target_abs = target.absolute()
    return source_abs == target_abs or source_abs in target_abs.parents or target_abs in source_abs.parents


def _readable_mode(path: Path, *, directory: bool = False) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    bits = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    if directory:
        bits |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    return bool(mode & bits)


def _credential_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        any(part in _CREDENTIAL_PARTS for part in parts)
        or name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519", "credentials.json", "token.json"}
        or Path(name).suffix in _KEY_SUFFIXES
    )


def _contains_raw_component(parts: Any) -> bool:
    return any(str(part).lower() in {"raw", "raw-spool"} for part in parts)


def _inside_protected_raw(parts: Any) -> bool:
    normalized = [str(part).lower() for part in parts]
    return any(
        normalized[index] == "data" and normalized[index + 1] in {"raw", "raw-spool"}
        for index in range(len(normalized) - 1)
    )


def _reason(exc: Exception) -> str:
    return exc.code if isinstance(exc, SnapshotError) else "source_unreadable"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["SOURCE_BUNDLE_MANIFEST_VERSION", "collect_sources", "verify_source_bundle"]
