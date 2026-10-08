"""Owner-only content erasure from the raw archive, with stable slots and an audit (D11).

This is the only code allowed to delete inside ``data/raw/``. It is reachable from the
owner's CLI (``yeoman raw purge``) and never from chat commands, models or agent tools.
The AUDIT line is written before the files change and records hashes, not content. Files
are rewritten through a temporary file and an atomic rename while holding the same
``flock`` the writer uses, so no concurrent append is lost.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import (
    OPEN_FILE_MODE,
    PURGE_DISPOSITION_LOCK,
    TOMBSTONE,
    _fsync_directory,
    append_protected,
    archive_files,
    derived_from_disposed,
    dumps,
    file_digest,
    iter_records,
    line_sha256,
    lock_file,
    owner_mutation_guard,
    record_capture_ms,
    record_correlations,
    record_identities,
    record_parts,
)
from yeoman_shared.raw_archive.verify import AUDIT, latest_manifest, record_closed
from yeoman_shared.raw_archive.writer import (
    SPOOL_REGISTRY_FILE,
    safe_channel,
    stored_media_relative,
    try_lock_media_purge,
)

PURGE_PUBLICATIONS = ".purge-publications.jsonl"

def _channel_files(root: Path, channel: str) -> list[Path]:
    files = archive_files(root, channel)
    for sub in ("backfill", "derived", "owner"):
        files.extend(archive_files(root, sub))
    return sorted(files)


@dataclass(frozen=True, slots=True)
class PurgeSelector:
    channel: str
    chat_id: str | None = None
    native_id: str | None = None
    before_ms: int | None = None

    def validate(self) -> None:
        if not self.channel.strip():
            raise ValueError("purge needs a channel")
        if not (self.chat_id or self.native_id):
            raise ValueError("purge needs a chat or a message id; whole-channel purge is refused")

    def matches(self, record: dict[str, Any]) -> bool:
        if record == TOMBSTONE:
            return False
        if record.get("channel") != safe_channel(self.channel):
            return False
        if self.chat_id and record.get("chat_id") != self.chat_id:
            return False
        if self.native_id and self.native_id not in record_identities(record):
            return False
        if self.before_ms is not None and record_capture_ms(record) >= self.before_ms:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PurgeResult:
    files: tuple[str, ...]
    removed_lines: int
    removed_sha256: tuple[str, ...]
    media_removed: tuple[str, ...]
    disposition_recorded: bool = False


def _media_path(record: dict[str, Any]) -> str | None:
    return stored_media_relative(record.get("media"))


@dataclass(frozen=True, slots=True)
class _PurgePredicate:
    selector: PurgeSelector
    chat_id: str | None
    identities: frozenset[str]
    correlations: frozenset[str]

    def matches(self, record: dict[str, Any]) -> bool:
        if record == TOMBSTONE:
            return False
        parts = record_parts(record)
        if parts != [record]:
            return any(self.matches(part) for part in parts)
        if record.get("channel") != safe_channel(self.selector.channel):
            return False
        if self.chat_id is not None and str(record.get("chat_id") or "") != self.chat_id:
            return False
        if (
            self.selector.before_ms is not None
            and record_capture_ms(record) >= self.selector.before_ms
            and not derived_from_disposed(record, self.identities)
        ):
            return False
        if self.selector.native_id is None:
            return self.selector.chat_id is not None
        ids = record_identities(record)
        if ids.intersection(self.identities):
            return True
        return bool(record_correlations(record).intersection(self.correlations))


def _build_predicate(root: Path, selector: PurgeSelector) -> _PurgePredicate:
    matched: list[dict[str, Any]] = []
    for path in _channel_files(root, safe_channel(selector.channel)):
        for _, record, _ in iter_records(path):
            if record is not None:
                matched.extend(part for part in record_parts(record) if selector.matches(part))
    chats = {str(record.get("chat_id") or "") for record in matched}
    if selector.native_id is not None and selector.chat_id is None:
        if len(chats) > 1:
            raise ValueError("message purge spans multiple chats; specify --chat")
        if len(chats) != 1 or not next(iter(chats)):
            raise ValueError("message purge has no resolvable chat; specify --chat")
    chat_id = selector.chat_id or (next(iter(chats)) if chats else None)
    identities = {selector.native_id} if selector.native_id else set()
    correlations: set[str] = set()
    for record in matched:
        identities.update(record_identities(record))
        correlations.update(record_correlations(record))
    return _PurgePredicate(selector, chat_id, frozenset(identities), frozenset(correlations))


def plan_purge(root: Path, selector: PurgeSelector) -> PurgeResult:
    """Dry run: what a purge with this selector would remove."""
    selector.validate()
    predicate = _build_predicate(root, selector)
    files: list[str] = []
    removed: list[str] = []
    removed_media: set[str] = set()
    kept_media: set[str] = set()
    for path in _channel_files(root, safe_channel(selector.channel)):
        hit = False
        for _, record, line in iter_records(path):
            media = _media_path(record) if record else None
            if record is not None and predicate.matches(record):
                hit = True
                removed.append(line_sha256(line))
                if media:
                    removed_media.add(media)
            elif media:
                kept_media.add(media)
        if hit:
            files.append(path.relative_to(root).as_posix())
    return PurgeResult(
        tuple(files), len(removed), tuple(removed), tuple(sorted(removed_media - kept_media))
    )


def _lock_file(path: Path) -> int:
    return lock_file(path)


def _lock_files(root: Path, files: tuple[str, ...]) -> list[tuple[str, Path, int]]:
    locked: list[tuple[str, Path, int]] = []
    try:
        for relative in sorted(files):
            path = root / relative
            locked.append((relative, path, _lock_file(path)))
    except BaseException:
        for _, _, lock_fd in reversed(locked):
            os.close(lock_fd)
        raise
    return locked


def _snapshot_locked(
    locked: list[tuple[str, Path, int]],
    predicate: _PurgePredicate,
    media_removed: tuple[str, ...],
) -> PurgeResult:
    files: list[str] = []
    removed: list[str] = []
    for relative, path, _ in locked:
        hit = False
        for _, record, line in iter_records(path):
            if record is not None and predicate.matches(record):
                hit = True
                removed.append(line_sha256(line))
        if hit:
            files.append(relative)
    return PurgeResult(tuple(files), len(removed), tuple(removed), media_removed)


def _archive_files_strict(root: Path) -> list[Path] | None:
    """Enumerate every line file without pathlib glob's suppressed scan errors."""
    paths: list[Path] = []
    try:
        with os.scandir(root) as directories:
            for directory in directories:
                if directory.name == "media":
                    continue
                if directory.is_symlink():
                    return None
                if not directory.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(directory.path) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".jsonl"):
                            continue
                        if not entry.is_file(follow_symlinks=True):
                            return None
                        paths.append(Path(entry.path))
    except OSError:
        return None
    return sorted(paths)


def _spool_media_references(root: Path) -> set[str] | None:
    registry = root / SPOOL_REGISTRY_FILE
    try:
        lines = registry.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeError:
        return None
    spools: set[Path] = set()
    try:
        for line in lines:
            entry = json.loads(line)
            spool = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(spool, str) or "\x00" in spool or not Path(spool).is_absolute():
                return None
            spools.add(Path(spool))
    except json.JSONDecodeError, TypeError, ValueError:
        return None
    if not spools:
        return None
    spools.add(root.parent / "raw-spool")

    references: set[str] = set()
    for spool in spools:
        try:
            with os.scandir(spool) as entries:
                items = sorted(entries, key=lambda entry: entry.name)
        except FileNotFoundError:
            continue
        except OSError, ValueError:
            return None
        for entry in items:
            if entry.name.endswith(".corrupt"):
                return None
            if not entry.name.endswith(".json"):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                return None
            try:
                envelope = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                if not isinstance(envelope, dict) or not isinstance(envelope.get("line"), str):
                    return None
                record = json.loads(envelope["line"])
                if not isinstance(record, dict):
                    return None
            except OSError, UnicodeError, json.JSONDecodeError, TypeError:
                return None
            media = _media_path(record)
            if media:
                references.add(media)
    return references


def _unreferenced_media_locked(
    root: Path,
    locked: list[tuple[str, Path, int]],
    predicate: _PurgePredicate,
) -> tuple[str, ...]:
    candidates: set[str] = set()
    selected_paths = {path for _, path, _ in locked}
    for _, path, _ in locked:
        try:
            for _, record, _ in iter_records(path):
                if record is not None and predicate.matches(record):
                    media = _media_path(record)
                    if media:
                        candidates.add(media)
        except OSError:
            return ()
    if not candidates:
        return ()

    paths = _archive_files_strict(root)
    if paths is None:
        return ()
    try:
        for path in paths:
            for _, record, _ in iter_records(path):
                if record is None:
                    return ()
                media = _media_path(record)
                if media in candidates and not (
                    path in selected_paths and predicate.matches(record)
                ):
                    candidates.discard(media)
        spool_references = _spool_media_references(root)
    except OSError:
        return ()
    if spool_references is None:
        return ()
    return tuple(sorted(candidates - spool_references))


def _stage_rewrite(path: Path, predicate: _PurgePredicate, lock_fd: int) -> Path:
    mode = os.fstat(lock_fd).st_mode & 0o777
    temporary = path.with_name(f".{path.name}.purge-tmp")
    temporary.unlink(missing_ok=True)  # Only called after any authorized pending stage was recovered.
    with path.open("rb") as source, temporary.open("wb") as handle:
        for raw in source:
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                record = None
            if isinstance(record, dict) and predicate.matches(record):
                replacement: dict[str, Any] = TOMBSTONE
                payload = record.get("payload")
                segments = payload.get("segments") if isinstance(payload, dict) else None
                if isinstance(segments, list):
                    parts = iter(record_parts(record))
                    kept = []
                    for segment in segments:
                        if isinstance(segment, dict) and segment != TOMBSTONE:
                            kept.append(TOMBSTONE if predicate.matches(next(parts)) else segment)
                        else:
                            kept.append(segment)
                    if any(isinstance(segment, dict) and segment != TOMBSTONE and segment for segment in kept):
                        # The original/raw batch duplicates removed content. Retain only the envelope
                        # and independent surviving segments, without inherited text/media/hash.
                        replacement = {key: record[key] for key in (
                            "backfill_version", "channel", "kind", "chat_id", "occurred_ms",
                            "time_certainty", "direction", "provenance", "skip_reason") if key in record}
                        replacement["received_ms"] = record_capture_ms(record)
                        clean_payload = {"segments": kept}
                        for key in ("chatJid", "fromAssistant", "messageId"):
                            if key in payload and (key != "messageId" or any(
                                    isinstance(segment, dict) and segment.get("messageId") == payload[key]
                                    for segment in kept)):
                                clean_payload[key] = payload[key]
                        replacement["payload"] = clean_payload
                ending = b"\r\n" if raw.endswith(b"\r\n") else b"\n" if raw.endswith(b"\n") else b""
                handle.write(dumps(replacement).encode("utf-8") + ending)
            else:
                handle.write(raw)
        os.fchmod(handle.fileno(), mode or OPEN_FILE_MODE)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    return temporary


def _manifest_digest(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    return entry.get("sha256"), entry.get("lines"), entry.get("bytes")


def _pending_publications(root: Path) -> list[dict[str, Any]]:
    """Read only owner-created, content-free publication evidence; corruption fails closed."""
    journal = root / PURGE_PUBLICATIONS
    if journal.is_symlink():
        raise OSError("invalid pending purge journal path")
    pending: dict[str, dict[str, Any]] = {}
    try:
        # ponytail: scan owner purge history; compact completed entries if it becomes large.
        for _, entry, _ in iter_records(journal):
            if (entry is None or entry.get("version") != 1
                    or not isinstance(entry.get("operation_id"), str)):
                raise OSError("invalid pending purge journal")
            operation = entry["operation_id"]
            if entry.get("state") == "pending":
                if operation in pending or not isinstance(entry.get("files"), list) or not entry["files"]:
                    raise OSError("invalid pending purge operation")
                seen: set[str] = set()
                for item in entry["files"]:
                    if not isinstance(item, dict) or not isinstance(item.get("file"), str):
                        raise OSError("invalid pending purge file")
                    relative = Path(item["file"])
                    if (relative.is_absolute() or len(relative.parts) != 2
                            or item["file"] != relative.as_posix() or item["file"] in seen
                            or any(part in {".", "..", "media"} for part in relative.parts)
                            or relative.suffix != ".jsonl"
                            or (root / relative).is_symlink() or (root / relative.parent).is_symlink()
                            or not isinstance(item.get("sealed"), bool)):
                        raise OSError("invalid pending purge destination")
                    seen.add(item["file"])
                    for key in ("before", "after"):
                        digest = item.get(key)
                        if (not isinstance(digest, list) or len(digest) != 3
                                or not isinstance(digest[0], str) or len(digest[0]) != 64
                                or any(c not in "0123456789abcdef" for c in digest[0])
                                or any(type(value) is not int or value < 0 for value in digest[1:])):
                            raise OSError("invalid pending purge digest")
                pending[operation] = entry
            elif entry.get("state") == "complete" and operation in pending:
                del pending[operation]
            else:
                raise OSError("invalid pending purge completion")
    except FileNotFoundError:
        return []
    except (UnicodeError, ValueError, TypeError) as exc:
        raise OSError("invalid pending purge journal") from exc
    return list(pending.values())


def _publish_pending(root: Path, operation: dict[str, Any], *, now_ms: int) -> None:
    """Caller holds the root and destination locks; never bless a digest outside this operation."""
    manifest = latest_manifest(root)
    for item in operation["files"]:
        path = root / item["file"]
        before, after = tuple(item["before"]), tuple(item["after"])
        current = file_digest(path)
        if current not in (before, after):
            raise OSError(f"pending purge bytes changed: {item['file']}")
        if item["sealed"] and _manifest_digest(manifest.get(item["file"], {})) not in (before, after):
            raise OSError(f"pending purge manifest changed: {item['file']}")
        if current == before:
            temporary = path.with_name(f".{path.name}.purge-tmp")
            if temporary.is_symlink() or file_digest(temporary) != after:
                raise OSError(f"pending purge stage changed: {item['file']}")
            os.replace(temporary, path)
        _fsync_directory(path.parent)
        if item["sealed"] and _manifest_digest(manifest[item["file"]]) != after:
            record_closed(root, path, now_ms=now_ms, note="purged")
    append_protected(root / PURGE_PUBLICATIONS, dumps({
        "version": 1, "operation_id": operation["operation_id"], "state": "complete",
    }))


def _recover_pending(root: Path, *, now_ms: int, destination: str | None = None) -> None:
    for operation in _pending_publications(root):
        if destination is not None and not any(item["file"] == destination for item in operation["files"]):
            continue
        locked = _lock_files(root, tuple(item["file"] for item in operation["files"]))
        try:
            _publish_pending(root, operation, now_ms=now_ms)
        finally:
            for _, _, fd in reversed(locked):
                os.close(fd)


def _purge(
    root: Path, selector: PurgeSelector, *, operator: str, now_ms: int | None = None
) -> PurgeResult:
    """Tombstone selected content and record a durable owner disposition, including zero matches."""
    selector.validate()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    effective = selector
    if selector.chat_id is not None and selector.native_id is None and selector.before_ms is None:
        from dataclasses import replace

        effective = replace(selector, before_ms=now)

    # ponytail: this root-wide lock serializes only raw appends against owner dispositions.
    disposition_fd = lock_file(root / PURGE_DISPOSITION_LOCK, create=True)
    media_guard_fd: int | None = None
    locked: list[tuple[str, Path, int]] = []
    try:
        media_guard_fd = try_lock_media_purge(root)
        _recover_pending(root, now_ms=now)
        files = tuple(
            path.relative_to(root).as_posix()
            for path in _channel_files(root, safe_channel(effective.channel))
        )
        locked = _lock_files(root, files)
        predicate = _build_predicate(root, effective)
        snapshot = _snapshot_locked(locked, predicate, ())
        media_removed = (
            _unreferenced_media_locked(root, locked, predicate)
            if media_guard_fd is not None
            else ()
        )
        plan = PurgeResult(
            snapshot.files,
            snapshot.removed_lines,
            snapshot.removed_sha256,
            media_removed,
            True,
        )

        selected_records: list[dict[str, Any]] = []
        for _, path, _ in locked:
            for _, record, _ in iter_records(path):
                if record is not None and predicate.matches(record):
                    selected_records.append(record)
        identities = set(predicate.identities)
        correlations = set(predicate.correlations)
        for record in selected_records:
            for part in record_parts(record):
                if not predicate.matches(part):
                    continue
                identities.update(record_identities(part))
                correlations.update(record_correlations(part))
        audit_record = {
            "ts_ms": now,
            "operator": operator,
            "selector": asdict(effective),
            "files": list(plan.files),
            "removed_lines": plan.removed_lines,
            "removed_sha256": list(plan.removed_sha256),
            "media_removed": list(plan.media_removed),
            "disposition": {
                "scope": "message" if effective.native_id is not None else "chat",
                "channel": safe_channel(effective.channel),
                "chat_id": predicate.chat_id,
                "before_ms": effective.before_ms,
                "message_identities": sorted(identities),
                "correlation_ids": sorted(correlations),
            },
        }
        append_protected(root / AUDIT, dumps(audit_record))
        sealed = latest_manifest(root)
        publication_files = []
        for relative, path, lock_fd in locked:
            if relative not in plan.files:
                continue
            before = file_digest(path)
            if relative in sealed and _manifest_digest(sealed[relative]) != before:
                raise OSError(f"sealed purge checksum mismatch: {relative}")
            temporary = _stage_rewrite(path, predicate, lock_fd)
            publication_files.append({"file": relative, "before": list(before),
                                      "after": list(file_digest(temporary)), "sealed": relative in sealed})
        if publication_files:
            operation = {"version": 1, "operation_id": uuid.uuid4().hex,
                         "state": "pending", "files": publication_files}
            append_protected(root / PURGE_PUBLICATIONS, dumps(operation))
            _publish_pending(root, operation, now_ms=now)
        for relative in plan.media_removed:
            (root / relative).unlink(missing_ok=True)
        return plan
    finally:
        for _, _, lock_fd in reversed(locked):
            os.close(lock_fd)
        if media_guard_fd is not None:
            os.close(media_guard_fd)
        os.close(disposition_fd)


def purge(root: Path, selector: PurgeSelector, *, operator: str, now_ms: int | None = None,
          projection_owner_fd: int | None = None) -> PurgeResult:
    selector.validate()
    with owner_mutation_guard(root, projection_owner_fd=projection_owner_fd):
        return _purge(root, selector, operator=operator, now_ms=now_ms)
