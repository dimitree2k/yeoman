"""Owner-only physical deletion from the raw archive, with an audit record (D11).

This is the only code allowed to delete inside ``data/raw/``. It is reachable from the
owner's CLI (``yeoman raw purge``) and never from chat commands, models or agent tools.
The AUDIT line is written before the files change and records hashes, not content. Files
are rewritten through a temporary file and an atomic rename while holding the same
``flock`` the writer uses, so no concurrent append is lost.
"""

from __future__ import annotations

import fcntl
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
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
from yeoman_shared.raw_archive.writer import safe_channel


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
    media = record.get("media")
    if (
        not isinstance(media, dict)
        or not media.get("stored")
        or not isinstance(media.get("path"), str)
    ):
        return None
    path = PurePosixPath(media["path"])
    if path.is_absolute() or path.parts[:1] != ("media",) or ".." in path.parts:
        return None
    return path.as_posix()


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


def _rewrite_without(path: Path, selector: PurgeSelector) -> None:
    while True:
        lock_fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if os.fstat(lock_fd).st_ino != os.stat(path).st_ino:
                continue
            mode = path.stat().st_mode & 0o777
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
            return
        finally:
            os.close(lock_fd)


def purge(
    root: Path, selector: PurgeSelector, *, operator: str, now_ms: int | None = None
) -> PurgeResult:
    """Physically remove matching lines (and now-unreferenced media). Owner CLI only."""
    plan = plan_purge(root, selector)
    if plan.removed_lines == 0:
        return plan
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
    for relative in plan.files:
        path = root / relative
        _rewrite_without(path, selector)
        if relative in sealed:
            record_closed(root, path, now_ms=now, note="purged")
    for relative in plan.media_removed:
        (root / relative).unlink(missing_ok=True)
    return plan
