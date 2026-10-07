"""Layer 1 (preserved originals): line format, write-once files, reader with stable refs."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import ProtectedPathError, is_protected
from yeoman_shared.raw_archive.records import TOMBSTONE

BACKFILL_VERSION = 1
PROVENANCE = frozenset({"native", "recovered_text", "verbatim_unverified", "derived_only"})
TIME_CERTAINTY = frozenset({"native", "provider_timestamp", "capture_time_approx", "unknown"})
SUBDIRS = ("whatsapp", "backfill", "derived", "owner")


def is_tombstone(record: Mapping[str, Any]) -> bool:
    return dict(record) == TOMBSTONE


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def row_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Origin:
    store: str
    path: str
    table: str
    row_key: str


def backfill_line(
    *,
    channel: str,
    kind: str,
    provenance: str,
    time_certainty: str,
    occurred_ms: int | None,
    direction: str | None,
    chat_id: str | None,
    payload: dict[str, Any],
    origin: Origin,
    original: Any,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    if provenance not in PROVENANCE:
        raise ValueError(f"unknown provenance: {provenance!r}")
    if time_certainty not in TIME_CERTAINTY:
        raise ValueError(f"unknown time certainty: {time_certainty!r}")
    return {
        "backfill_version": BACKFILL_VERSION,
        "channel": channel,
        "kind": kind,
        "provenance": provenance,
        "time_certainty": time_certainty,
        "occurred_ms": occurred_ms,
        "direction": direction,
        "chat_id": chat_id,
        "payload": payload,
        "skip_reason": skip_reason,
        "origin": {
            "store": origin.store,
            "path": origin.path,
            "table": origin.table,
            "row_key": origin.row_key,
            "row_sha256": row_sha256(original),
        },
        "original": original,
    }


def write_jsonl_once(path: Path, lines: Iterable[dict[str, Any]]) -> int:
    """Write a frozen Layer 1 file. Refuses overwrite; leftover .partial requires manual removal."""
    if is_protected(path):
        raise ProtectedPathError(f"refusing write to protected raw archive path: {path}")
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".partial")
    count = 0
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        for line in lines:
            out.write(canonical_json(line) + "\n")
            count += 1
        out.flush()
        os.fsync(out.fileno())
    os.link(partial, path)
    partial.unlink()
    return count


@dataclass(frozen=True)
class Layer1Line:
    ref: str
    record: dict[str, Any] | None


def layer1_files(roots: Sequence[Path]) -> list[tuple[str, Path]]:
    found: dict[str, Path] = {}
    for root in roots:
        for sub in SUBDIRS:
            folder = root / sub
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.jsonl")):
                rel = f"{sub}/{path.name}"
                if rel in found:
                    raise ValueError(f"{rel} exists in two Layer 1 roots")
                found[rel] = path
    return sorted(found.items())


def iter_layer1(roots: Sequence[Path]) -> Iterator[Layer1Line]:
    for rel, path in layer1_files(roots):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    record = None
                yield Layer1Line(f"{rel}#{number}", record if isinstance(record, dict) else None)
