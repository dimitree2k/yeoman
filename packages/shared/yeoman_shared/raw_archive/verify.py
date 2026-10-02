"""Month closing, MANIFEST, SUPPRESSIONS and the daily integrity check (V1 spec §4.0).

Closed months stay plain JSONL with mode 0444. Closing a month never deletes anything.
MANIFEST is append-only; the latest entry per file wins (an owner purge appends a new one).
A line or file count drop is a problem unless an AUDIT entry since the previous check
names the file.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import raw_root
from yeoman_shared.raw_archive.records import (
    CLOSED_FILE_MODE,
    append_protected,
    archive_files,
    dumps,
    file_digest,
    is_month_stem,
    iter_records,
)
from yeoman_shared.raw_archive.writer import STATUS_FILE, month_of
from yeoman_shared.utils.helpers import get_run_path

MANIFEST = "MANIFEST"
AUDIT = "AUDIT"
SUPPRESSIONS = "SUPPRESSIONS"
COUNTS_FILE = "raw-archive-counts.json"


@dataclass(frozen=True, slots=True)
class VerifyReport:
    ok: bool
    problems: tuple[str, ...]
    closed: tuple[str, ...]
    files_checked: int
    lines_total: int


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [record for _, record, _ in iter_records(path) if record is not None]


def latest_manifest(root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for record in _read_jsonl(root / MANIFEST):
        if isinstance(record.get("file"), str):
            entries[record["file"]] = record
    return entries


def audit_entries(root: Path) -> list[dict[str, Any]]:
    return _read_jsonl(root / AUDIT)


def record_closed(root: Path, path: Path, *, now_ms: int, note: str = "closed") -> dict[str, Any]:
    sha, lines, size = file_digest(path)
    entry = {
        "file": path.relative_to(root).as_posix(),
        "sha256": sha,
        "lines": lines,
        "bytes": size,
        "closed_ms": now_ms,
        "note": note,
    }
    append_protected(root / MANIFEST, dumps(entry))
    return entry


def close_months(root: Path, *, now_ms: int) -> list[str]:
    """Seal every month file older than the current UTC month that isn't sealed yet."""
    current = month_of(now_ms)
    sealed = latest_manifest(root)
    closed: list[str] = []
    for path in archive_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in sealed or not is_month_stem(path.stem) or path.stem >= current:
            continue
        os.chmod(path, CLOSED_FILE_MODE)
        record_closed(root, path, now_ms=now_ms)
        closed.append(relative)
    return closed


def append_suppression(
    root: Path, *, channel: str, chat_id: str, native_id: str, reason: str, now_ms: int
) -> None:
    record = {
        "ts_ms": now_ms,
        "channel": channel,
        "chat_id": chat_id,
        "native_id": native_id,
        "reason": reason,
    }
    append_protected(root / SUPPRESSIONS, dumps(record))


def load_suppressions(root: Path) -> set[tuple[str, str, str]]:
    return {
        (
            str(record.get("channel") or ""),
            str(record.get("chat_id") or ""),
            str(record.get("native_id") or ""),
        )
        for record in _read_jsonl(root / SUPPRESSIONS)
    }


def _count_media(root: Path) -> int:
    media = root / "media"
    if not media.is_dir():
        return 0
    return sum(1 for path in media.rglob("*") if path.is_file() and not path.name.startswith("."))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_counts(path: Path, counts: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(counts), encoding="utf-8")
    os.replace(temporary, path)


def verify_archive(
    root: Path | None = None, *, run_dir: Path | None = None, now_ms: int | None = None
) -> VerifyReport:
    root = root or raw_root()
    run = run_dir or get_run_path()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if not root.is_dir():
        return VerifyReport(False, ("archive_missing",), (), 0, 0)

    problems: list[str] = []
    status = _load_json(run / STATUS_FILE)
    spool = root.parent / "raw-spool"
    try:
        spool_has_backlog = next(spool.glob("*.json"), None) is not None
    except OSError:
        spool_has_backlog = True
    writer_has_backlog = (
        spool_has_backlog
        or status.get("state") in {"degraded", "blocked"}
        or int(status.get("spooled", 0)) > 0
        or int(status.get("pending_in_memory", 0)) > 0
    )
    closed = [] if writer_has_backlog else close_months(root, now_ms=now)
    manifest = latest_manifest(root)
    previous = _load_json(run / COUNTS_FILE)
    since = int(previous.get("checked_ms", 0))
    audited = {
        str(name)
        for entry in audit_entries(root)
        if int(entry.get("ts_ms", 0)) >= since
        for name in entry.get("files", [])
    }
    media_audited = any(
        entry.get("media_removed")
        for entry in audit_entries(root)
        if int(entry.get("ts_ms", 0)) >= since
    )

    current: dict[str, int] = {}
    lines_total = 0
    for path in archive_files(root):
        relative = path.relative_to(root).as_posix()
        sha, lines, _size = file_digest(path)
        current[relative] = lines
        lines_total += lines
        entry = manifest.get(relative)
        if entry is not None and (
            entry.get("sha256") != sha or int(entry.get("lines", -1)) != lines
        ):
            problems.append(f"checksum_mismatch:{relative}")
    for relative in manifest:
        if relative not in current and relative not in audited:
            problems.append(f"file_missing:{relative}")
    for relative, old in dict(previous.get("files", {})).items():
        new = current.get(relative)
        if new is None:
            if relative not in manifest and relative not in audited:
                problems.append(f"file_missing:{relative}")
        elif new < int(old) and relative not in audited:
            problems.append(f"line_count_dropped:{relative}:{old}->{new}")

    media_files = _count_media(root)
    if media_files < int(previous.get("media_files", 0)) and not media_audited:
        problems.append(f"media_count_dropped:{previous.get('media_files')}->{media_files}")

    if status.get("state") == "degraded":
        problems.append(f"writer_degraded:spooled={status.get('spooled', '?')}")
    elif status.get("state") == "blocked":
        problems.append(f"writer_blocked:pending={status.get('pending_in_memory', '?')}")

    _save_counts(
        run / COUNTS_FILE, {"checked_ms": now, "files": current, "media_files": media_files}
    )
    unique = tuple(dict.fromkeys(problems))
    return VerifyReport(not unique, unique, tuple(closed), len(current), lines_total)
