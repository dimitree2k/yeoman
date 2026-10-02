"""Owner-only physical deletion from the raw archive, with an audit record (D11).

This is the only code allowed to delete inside ``data/raw/``. It is reachable from the
owner's CLI (``yeoman raw purge``) and never from chat commands, models or agent tools.
The AUDIT line is written before the files change and records hashes, not content. Files
are rewritten through a temporary file and an atomic rename while holding the same
``flock`` the writer uses, so no concurrent append is lost.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import (
    OPEN_FILE_MODE,
    append_protected,
    archive_files,
    dumps,
    iter_records,
    line_sha256,
)
from yeoman_shared.raw_archive.verify import AUDIT, latest_manifest, record_closed
from yeoman_shared.raw_archive.writer import (
    SPOOL_REGISTRY_FILE,
    safe_channel,
    stored_media_relative,
    try_lock_media_purge,
)


def _identities(record: dict[str, Any]) -> set[str]:
    ids = {str(record.get("native_id") or "")}
    native = record.get("native")
    if isinstance(native, dict):
        payload = native.get("payload")
        if isinstance(payload, dict):
            ids.add(str(payload.get("messageId") or ""))
        for key in ("message_id", "source_message_id"):
            ids.add(str(native.get(key) or ""))
    ids.discard("")
    return ids


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
        if record.get("channel") != safe_channel(self.channel):
            return False
        if self.chat_id and record.get("chat_id") != self.chat_id:
            return False
        if self.native_id and self.native_id not in _identities(record):
            return False
        if self.before_ms is not None and int(record.get("received_ms") or 0) >= self.before_ms:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PurgeResult:
    files: tuple[str, ...]
    removed_lines: int
    removed_sha256: tuple[str, ...]
    media_removed: tuple[str, ...]


def _media_path(record: dict[str, Any]) -> str | None:
    return stored_media_relative(record.get("media"))


def plan_purge(root: Path, selector: PurgeSelector) -> PurgeResult:
    """Dry run: what a purge with this selector would remove."""
    selector.validate()
    files: list[str] = []
    removed: list[str] = []
    removed_media: set[str] = set()
    kept_media: set[str] = set()
    for path in archive_files(root, safe_channel(selector.channel)):
        hit = False
        for _, record, line in iter_records(path):
            media = _media_path(record) if record else None
            if record is not None and selector.matches(record):
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
    while True:
        lock_fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.fstat(lock_fd).st_ino == os.stat(path).st_ino:
                return lock_fd
        except BaseException:
            os.close(lock_fd)
            raise
        os.close(lock_fd)


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
    selector: PurgeSelector,
    media_removed: tuple[str, ...],
) -> PurgeResult:
    files: list[str] = []
    removed: list[str] = []
    for relative, path, _ in locked:
        hit = False
        for _, record, line in iter_records(path):
            if record is not None and selector.matches(record):
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
    selector: PurgeSelector,
) -> tuple[str, ...]:
    candidates: set[str] = set()
    selected_paths = {path for _, path, _ in locked}
    for _, path, _ in locked:
        try:
            for _, record, _ in iter_records(path):
                if record is not None and selector.matches(record):
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
                    path in selected_paths and selector.matches(record)
                ):
                    candidates.discard(media)
        spool_references = _spool_media_references(root)
    except OSError:
        return ()
    if spool_references is None:
        return ()
    return tuple(sorted(candidates - spool_references))


def _rewrite_without(path: Path, selector: PurgeSelector, lock_fd: int) -> None:
    mode = os.fstat(lock_fd).st_mode & 0o777
    temporary = path.with_name(f".{path.name}.purge-tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for _, record, line in iter_records(path):
            if record is not None and selector.matches(record):
                continue
            handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode or OPEN_FILE_MODE)
    os.replace(temporary, path)


def purge(
    root: Path, selector: PurgeSelector, *, operator: str, now_ms: int | None = None
) -> PurgeResult:
    """Remove selected lines and only media proven unreferenced under the writer guard."""
    selector.validate()
    files = tuple(
        path.relative_to(root).as_posix()
        for path in archive_files(root, safe_channel(selector.channel))
    )
    if not files:
        return PurgeResult((), 0, (), ())

    # ponytail: root-wide media lock serializes channels; shard if needed, while busy scans retain media.
    # Writers hold it from publication through the archive/spool write.
    media_guard_fd = try_lock_media_purge(root)
    locked: list[tuple[str, Path, int]] = []
    try:
        locked = _lock_files(root, files)
        snapshot = _snapshot_locked(locked, selector, ())
        if snapshot.removed_lines == 0:
            return snapshot
        media_removed = (
            _unreferenced_media_locked(root, locked, selector) if media_guard_fd is not None else ()
        )
        plan = PurgeResult(
            snapshot.files,
            snapshot.removed_lines,
            snapshot.removed_sha256,
            media_removed,
        )
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        append_protected(
            root / AUDIT,
            dumps(
                {
                    "ts_ms": now,
                    "operator": operator,
                    "selector": asdict(selector),
                    "files": list(plan.files),
                    "removed_lines": plan.removed_lines,
                    "removed_sha256": list(plan.removed_sha256),
                    "media_removed": list(plan.media_removed),
                }
            ),
        )
        sealed = latest_manifest(root)
        for relative, path, lock_fd in locked:
            if relative not in plan.files:
                continue
            _rewrite_without(path, selector, lock_fd)
            if relative in sealed:
                record_closed(root, path, now_ms=now, note="purged")
        for relative in plan.media_removed:
            (root / relative).unlink(missing_ok=True)
        return plan
    finally:
        for _, _, lock_fd in reversed(locked):
            os.close(lock_fd)
        if media_guard_fd is not None:
            os.close(media_guard_fd)
