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
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

DIR_MODE = 0o700
OPEN_FILE_MODE = 0o600
CLOSED_FILE_MODE = 0o444
_MONTH_STEM = re.compile(r"^\d{4}-\d{2}$")


def ensure_private_dir(path: Path) -> None:
    if not path.is_dir():
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)


def append_line(path: Path, line: str, *, mode: int = OPEN_FILE_MODE) -> None:
    """Append one line and fsync. Raises ``OSError``; callers decide how to degrade."""
    if "\n" in line or "\r" in line:
        raise ValueError("raw archive lines must not contain newlines")
    ensure_private_dir(path.parent)
    data = (line + "\n").encode("utf-8")
    while True:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino != os.stat(path).st_ino:
                continue  # replaced while we waited; reopen the current file
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            return
        finally:
            os.close(fd)


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
