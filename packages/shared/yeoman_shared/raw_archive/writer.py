"""Append-only writer for the raw message archive (V1 spec §4.0, R7).

The writer never raises into message handling. A line that cannot reach its month file goes
to the spool (``data/raw-spool/``). When even the spool fails, it stays in a bounded
in-memory queue. Both drain, oldest first, on the next append. A crash between "appended"
and "spool file removed" can duplicate a line; that is harmless because readers deduplicate.
The writer owns the spool, so removing a drained spool file is not a guard violation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil  # noqa: F401 - used by Task 5 media storage
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.paths import raw_root, spool_root
from yeoman_shared.raw_archive.records import (
    CLOSED_FILE_MODE,
    OPEN_FILE_MODE,
    append_line,
    dumps,
    ensure_private_dir,
)
from yeoman_shared.utils.helpers import get_run_path

logger = logging.getLogger(__name__)

ARCHIVE_VERSION = 1
STATUS_FILE = "raw-archive.json"
START_FILE = "START"
MAX_MEMORY_PENDING = 10_000
DEFAULT_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RawEvent:
    """One native event exactly as the adapter received or sent it."""

    channel: str
    kind: str
    direction: str
    native: Mapping[str, Any]
    native_id: str = ""
    chat_id: str = ""
    account: str = ""
    correlation_id: str = ""
    provenance: str = "native"
    media: Mapping[str, Any] | None = None
    received_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RawArchiveStatus:
    state: str
    spooled: int
    pending_in_memory: int
    last_error: str
    updated_ms: int


def month_of(ms: int) -> str:
    """UTC month bucket of a millisecond timestamp, e.g. ``2026-10``."""
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m")


def safe_channel(channel: str) -> str:
    cleaned = "".join(ch for ch in channel.strip().lower() if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise ValueError("raw archive channel must not be empty")
    return cleaned


def media_kind_from_mime(mime: str | None) -> str:
    major = (mime or "").split("/", 1)[0].lower()
    return {"image": "image", "video": "video", "audio": "audio"}.get(major, "document")


def read_start_ms(root: Path) -> int | None:
    """The moment the live writer first ran for this archive, or ``None``."""
    try:
        data = json.loads((root / START_FILE).read_text(encoding="utf-8"))
        return int(data["started_ms"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_suffix(suffix: str) -> str:
    cleaned = "".join(ch for ch in suffix.lower().lstrip(".") if ch.isalnum())[:8]
    return f".{cleaned}" if cleaned else ".bin"


class RawArchive:
    """Thread-safe, append-only raw archive writer."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        spool: Path | None = None,
        status_path: Path | None = None,
        clock: Callable[[], int] | None = None,
        media_enabled: bool = True,
        max_video_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
    ) -> None:
        self.root = Path(root) if root is not None else raw_root()
        self.spool = Path(spool) if spool is not None else spool_root()
        self.status_path = (
            Path(status_path) if status_path is not None else get_run_path() / STATUS_FILE
        )
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._media_enabled = bool(media_enabled)
        self._max_video_bytes = max(0, int(max_video_bytes))
        self._lock = threading.Lock()
        self._pending: list[tuple[str, int, str]] = []
        self._last_error = ""
        self._published = ""
        self._write_start_marker()

    # -- public API -------------------------------------------------------------------

    def append(self, event: RawEvent) -> bool:
        """Archive one event. ``True`` when it reached its month file, ``False`` when deferred."""
        received_ms = int(event.received_ms if event.received_ms is not None else self._clock())
        channel = safe_channel(event.channel)
        line = dumps(self._record(event, channel=channel, received_ms=received_ms))
        with self._lock:
            self._drain_locked()
            if self._has_backlog_locked():
                # Keep order: never write past older, still-undelivered lines.
                self._defer_locked(channel, received_ms, line)
                self._publish_status_locked()
                return False
            try:
                append_line(self._month_file(channel, received_ms), line)
            except OSError as exc:
                self._note_error(exc)
                self._defer_locked(channel, received_ms, line)
                self._publish_status_locked()
                return False
            self._publish_status_locked()
            return True

    def drain_spool(self) -> int:
        with self._lock:
            moved = self._drain_locked()
            self._publish_status_locked()
            return moved

    def status(self) -> RawArchiveStatus:
        with self._lock:
            return self._status_locked()

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _record(event: RawEvent, *, channel: str, received_ms: int) -> dict[str, Any]:
        return {
            "archive_version": ARCHIVE_VERSION,
            "received_ms": received_ms,
            "channel": channel,
            "kind": str(event.kind),
            "direction": str(event.direction),
            "native_id": str(event.native_id),
            "chat_id": str(event.chat_id),
            "account": str(event.account),
            "correlation_id": str(event.correlation_id),
            "provenance": str(event.provenance),
            "media": dict(event.media) if event.media is not None else None,
            "native": dict(event.native),
        }

    def _month_file(self, channel: str, received_ms: int) -> Path:
        return self.root / channel / f"{month_of(received_ms)}.jsonl"

    def _note_error(self, exc: BaseException) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.error("raw archive write failed: %s", type(exc).__name__)

    def _write_start_marker(self) -> None:
        marker = self.root / START_FILE
        if marker.exists():
            return
        try:
            ensure_private_dir(self.root)
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, CLOSED_FILE_MODE)
        except FileExistsError:
            return
        except OSError as exc:
            self._note_error(exc)
            return
        try:
            payload = dumps({"archive_version": ARCHIVE_VERSION, "started_ms": self._clock()})
            os.write(fd, (payload + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _spooled_files(self) -> list[Path]:
        if not self.spool.is_dir():
            return []
        return sorted(self.spool.glob("*.json"))

    def _has_backlog_locked(self) -> bool:
        return bool(self._pending) or bool(self._spooled_files())

    def _defer_locked(self, channel: str, received_ms: int, line: str) -> None:
        if self._pending:
            # Memory already holds older lines; spooling now would reorder them.
            self._remember_locked(channel, received_ms, line)
            return
        try:
            ensure_private_dir(self.spool)
            name = f"{received_ms:013d}-{uuid.uuid4().hex}.json"
            temporary = self.spool / f".{name}.tmp"
            temporary.write_text(
                json.dumps({"channel": channel, "received_ms": received_ms, "line": line}),
                encoding="utf-8",
            )
            os.chmod(temporary, OPEN_FILE_MODE)
            os.replace(temporary, self.spool / name)
        except OSError as exc:
            self._note_error(exc)
            self._remember_locked(channel, received_ms, line)

    def _remember_locked(self, channel: str, received_ms: int, line: str) -> None:
        if len(self._pending) >= MAX_MEMORY_PENDING:
            logger.error("raw archive in-memory queue full; a line could not be kept")
            return
        self._pending.append((channel, received_ms, line))

    def _drain_locked(self) -> int:
        moved = 0
        for item in self._spooled_files():
            try:
                record = json.loads(item.read_text(encoding="utf-8"))
                target = self._month_file(str(record["channel"]), int(record["received_ms"]))
                line = str(record["line"])
            except (ValueError, KeyError, TypeError):
                os.replace(item, item.with_name(item.name + ".corrupt"))
                continue
            except OSError as exc:
                self._note_error(exc)
                return moved
            try:
                append_line(target, line)
            except OSError as exc:
                self._note_error(exc)
                return moved
            item.unlink()
            moved += 1
        while self._pending:
            channel, received_ms, line = self._pending[0]
            try:
                append_line(self._month_file(channel, received_ms), line)
            except OSError as exc:
                self._note_error(exc)
                break
            self._pending.pop(0)
            moved += 1
        return moved

    def _status_locked(self) -> RawArchiveStatus:
        spooled = len(self._spooled_files())
        degraded = spooled > 0 or bool(self._pending)
        return RawArchiveStatus(
            state="degraded" if degraded else "ok",
            spooled=spooled,
            pending_in_memory=len(self._pending),
            last_error=self._last_error if degraded else "",
            updated_ms=self._clock(),
        )

    def _publish_status_locked(self) -> None:
        status = self._status_locked()
        key = f"{status.state}:{status.spooled}:{status.pending_in_memory}"
        if key == self._published:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.status_path.with_name(self.status_path.name + ".tmp")
            temporary.write_text(json.dumps(asdict(status)), encoding="utf-8")
            os.replace(temporary, self.status_path)
            self._published = key
        except OSError:
            logger.error("raw archive status file could not be written")


async def append_async(archive: RawArchive | None, event: RawEvent) -> None:
    """Archive from async code without blocking the loop. Never raises."""
    if archive is None:
        return
    try:
        await asyncio.to_thread(archive.append, event)
    except Exception as exc:  # noqa: BLE001 - the archive must never break message handling
        logger.error("raw archive append crashed kind=%s error=%s", event.kind, type(exc).__name__)
