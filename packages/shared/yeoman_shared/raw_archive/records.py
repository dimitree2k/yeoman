"""Low-level, append-only line I/O shared by every raw archive component.

Appends take an exclusive ``flock`` on the file. After acquiring it, the writer checks that
the path still points at the same inode: an owner purge replaces files atomically, and a
writer that waited on the old inode must reopen instead of appending to a deleted file.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

DIR_MODE = 0o700
OPEN_FILE_MODE = 0o600
CLOSED_FILE_MODE = 0o444
PURGE_DISPOSITION_LOCK = ".purge-disposition.lock"
_MONTH_STEM = re.compile(r"^\d{4}-\d{2}$")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_private_dir(path: Path) -> None:
    if path.is_dir():
        return
    parent = path.parent
    if parent != path:
        ensure_private_dir(parent)
    try:
        path.mkdir(mode=DIR_MODE)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        _fsync_directory(parent)


def lock_file(path: Path, *, create: bool = False) -> int:
    """Take an exclusive lock, retrying if an owner purge replaced the path."""
    if create:
        ensure_private_dir(path.parent)
    while True:
        try:
            flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
            fd = os.open(path, flags, OPEN_FILE_MODE)
        except FileNotFoundError:
            if create:
                continue
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino == os.stat(path).st_ino:
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def append_line(
    path: Path,
    line: str,
    *,
    mode: int = OPEN_FILE_MODE,
    coordination_lock: Path | None = None,
    should_append: Callable[[], bool] | None = None,
) -> bool:
    """Append one line and fsync; return false when the locked owner check disposes it."""
    if "\n" in line or "\r" in line:
        raise ValueError("raw archive lines must not contain newlines")
    ensure_private_dir(path.parent)
    data = (line + "\n").encode("utf-8")
    coordinator_fd = (
        lock_file(coordination_lock, create=True) if coordination_lock is not None else None
    )
    try:
        while True:
            try:
                fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_APPEND)
                except FileNotFoundError:
                    continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_ino != os.stat(path).st_ino:
                    continue  # replaced while we waited; reopen the current file
                if should_append is not None and not should_append():
                    return False
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    if os.write(fd, b"\n") != 1:
                        raise OSError("could not separate an incomplete raw archive line")
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("raw archive append made no progress")
                    view = view[written:]
                os.fsync(fd)
                _fsync_directory(path.parent)
                return True
            finally:
                os.close(fd)
    finally:
        if coordinator_fd is not None:
            os.close(coordinator_fd)


def record_identities(record: dict[str, Any]) -> set[str]:
    channel = str(record.get("channel") or "")
    kind = str(record.get("kind") or "")
    native = record.get("native")
    if isinstance(native, dict):
        event_metadata = (
            (channel == "telegram" and kind in {"update", "media"})
            or "eventId" in native
            or record.get("provenance") == "journal"
        )
        ids = set() if event_metadata else {str(record.get("native_id") or "")}
        payload = native.get("payload")
        if isinstance(payload, dict):
            ids.add(str(payload.get("messageId") or ""))
            encrypted_edit = payload.get("encryptedEdit")
            if (
                payload.get("observationOnly") is True
                and payload.get("observationType") == "encrypted_message_edit_undecoded"
                and isinstance(encrypted_edit, dict)
                and encrypted_edit.get("kind") == "secretEncryptedMessage"
            ):
                ids.add(str(payload.get("targetMessageId") or ""))
        for key in ("message_id", "source_message_id"):
            ids.add(str(native.get(key) or ""))
        for key in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "business_message",
            "edited_business_message",
        ):
            message = native.get(key)
            if isinstance(message, dict):
                ids.add(str(message.get("message_id") or ""))
    else:
        ids = {str(record.get("native_id") or "")}
    if (channel == "telegram" and kind == "media") or record.get("provenance") == "journal":
        ids.add(str(record.get("correlation_id") or ""))
    ids.discard("")
    return ids


def append_is_disposed(audit_path: Path, record: dict[str, Any], line: str) -> bool:
    """Match a locked month append against durable owner purge dispositions."""
    channel = str(record.get("channel") or "")
    chat_id = str(record.get("chat_id") or "")
    received_ms = int(record.get("received_ms") or 0)
    identities = record_identities(record)
    correlation_id = str(record.get("correlation_id") or "")
    digest = line_sha256(line)
    try:
        for _, audit, _ in iter_records(audit_path):
            if audit is None:
                raise OSError("raw archive AUDIT contains an invalid line")
            if "disposition" not in audit:
                continue  # Historical AUDIT records predate durable dispositions.
            disposition = audit["disposition"]
            if not isinstance(disposition, dict):
                raise OSError("raw archive AUDIT disposition is invalid")
            scope = disposition.get("scope")
            disposition_channel = disposition.get("channel")
            scoped_chat = disposition.get("chat_id")
            before_ms = disposition.get("before_ms")
            message_ids = disposition.get("message_identities")
            correlation_ids = disposition.get("correlation_ids")
            removed_hashes = audit.get("removed_sha256")
            if (
                scope not in {"chat", "message"}
                or not isinstance(disposition_channel, str)
                or (scoped_chat is not None and not isinstance(scoped_chat, str))
                or (
                    before_ms is not None
                    and (not isinstance(before_ms, int) or isinstance(before_ms, bool))
                )
                or (scope == "chat" and before_ms is None)
                or not isinstance(message_ids, list)
                or any(not isinstance(value, str) for value in message_ids)
                or not isinstance(correlation_ids, list)
                or any(not isinstance(value, str) for value in correlation_ids)
                or not isinstance(removed_hashes, list)
                or any(not isinstance(value, str) for value in removed_hashes)
            ):
                raise OSError("raw archive AUDIT disposition is malformed")
            if disposition_channel != channel:
                continue
            if scoped_chat is not None and scoped_chat != chat_id:
                continue
            if scope == "message" and scoped_chat is None and chat_id:
                continue  # Do not let an unscoped numeric ID collide across chats.
            if before_ms is not None and received_ms >= before_ms:
                continue
            if scope == "chat":
                if before_ms is None:
                    raise OSError("raw archive chat disposition has no cutoff")
                return received_ms < before_ms
            if digest in removed_hashes:
                return True
            if identities.intersection(message_ids):
                return True
            if correlation_id and correlation_id in correlation_ids:
                return True
    except FileNotFoundError:
        if audit_path.is_symlink():
            raise OSError("raw archive AUDIT symlink is broken")
        return False
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise OSError("could not read raw archive purge dispositions") from exc
    return False


def append_protected(path: Path, line: str) -> None:
    """Append to a ``0444`` bookkeeping file (MANIFEST, AUDIT, SUPPRESSIONS)."""
    if path.exists():
        os.chmod(path, OPEN_FILE_MODE)
        try:
            append_line(path, line)
        finally:
            os.chmod(path, CLOSED_FILE_MODE)
    else:
        append_line(path, line, mode=CLOSED_FILE_MODE)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value, key=repr) if isinstance(value, (set, frozenset)) else list(value)
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def dumps(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default
    )


def iter_records(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str]]:
    """Yield ``(line_number, record_or_None, raw_line)``; unparseable lines yield ``None``."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                yield number, None, line
                continue
            yield number, parsed if isinstance(parsed, dict) else None, line


def file_digest(path: Path) -> tuple[str, int, int]:
    """``(sha256, line_count, byte_count)`` of a file, streamed."""
    digest = hashlib.sha256()
    lines = 0
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
    return digest.hexdigest(), lines, size


def line_sha256(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def is_month_stem(stem: str) -> bool:
    return bool(_MONTH_STEM.match(stem))


def archive_files(root: Path, channel: str | None = None) -> list[Path]:
    """Every line file (month and seed files) below *root*, sorted; media excluded."""
    if not root.is_dir():
        return []
    if channel is not None:
        directories = [root / channel]
    else:
        directories = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name != "media"]
    files: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.jsonl")))
    return files
