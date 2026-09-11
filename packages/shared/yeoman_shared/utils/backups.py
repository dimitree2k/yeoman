"""Timestamped, retention-pruned snapshots of private runtime files.

Backups live next to the artifact they protect, grouped by category::

    ~/.yeoman/config.json  -> ~/.yeoman/backups/config/<stamp>-config.json
    ~/.yeoman/policy.json  -> ~/.yeoman/backups/policy/<stamp>-policy.json

Two properties keep the directories small:

* content deduplication — a snapshot is only written when no retained backup
  already holds the exact same bytes, so a file that keeps flip-flopping
  between two canonical forms does not grow the directory without bound;
* retention pruning — snapshots older than the retention window are removed
  on every snapshot attempt (default 30 days, see
  ``YEOMAN_BACKUP_RETENTION_DAYS``).

Timestamps follow the documented ``YYYY-MM-DDTHHMMSS±HHMM`` convention (local
time with UTC offset) and snapshots are owner-readable only (0600).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

BACKUP_DIR_NAME = "backups"
DEFAULT_RETENTION_DAYS = 30
RETENTION_ENV_VAR = "YEOMAN_BACKUP_RETENTION_DAYS"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M%S%z"


def retention_days() -> int:
    """Retention window in days; ``0`` disables pruning.

    Reads ``YEOMAN_BACKUP_RETENTION_DAYS`` and falls back to
    :data:`DEFAULT_RETENTION_DAYS` when unset or unparseable.
    """
    raw = os.environ.get(RETENTION_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else 0


def category_dir(base: Path, category: str) -> Path:
    """Return the backup directory for ``category`` under ``base`` (not created)."""
    return base / BACKUP_DIR_NAME / category


def backups_dir(base: Path, category: str) -> Path:
    """Return and create ``<base>/backups/<category>`` with owner-only mode."""
    return _ensure_dir(category_dir(base, category))


def backup_file(
    path: Path,
    *,
    category: str,
    label: str | None = None,
    retention: int | None = None,
) -> Path | None:
    """Snapshot ``path`` into ``<path.parent>/backups/<category>``.

    Args:
        path: Existing file to snapshot. A missing file is not an error.
        category: Subdirectory under ``backups/`` (``config``, ``policy``, ...).
        label: Name stem of the snapshot; defaults to ``path.stem``.
        retention: Retention window in days; defaults to :func:`retention_days`.

    Returns:
        The new snapshot path, or ``None`` when nothing was written because the
        source is missing or its content is already retained.
    """
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return write_backup_bytes(
        category_dir(path.parent, category),
        label=label or path.stem,
        data=data,
        suffix=path.suffix or ".json",
        retention=retention,
    )


def write_backup_bytes(
    directory: Path,
    *,
    label: str,
    data: bytes,
    suffix: str = ".json",
    retention: int | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Store ``data`` as a snapshot inside ``directory``.

    Returns:
        The snapshot path, or ``None`` when an identical retained snapshot
        already exists. Pruning runs regardless, so retention is enforced even
        when a write is skipped.
    """
    target_dir = _ensure_dir(directory)
    days = retention_days() if retention is None else retention
    prune_backups(target_dir, retention=days, now=now)

    if find_identical(target_dir, data) is not None:
        return None

    stamp = (now or datetime.now().astimezone()).strftime(TIMESTAMP_FORMAT)
    target = _unique_path(target_dir, f"{stamp}-{label}", suffix)
    try:
        with open(target, "wb") as handle:
            handle.write(data)
        target.chmod(0o600)
    except OSError:
        return None
    return target


def prune_backups(
    directory: Path,
    *,
    retention: int | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Delete snapshots in ``directory`` older than the retention window.

    Age is taken from the snapshot's modification time, which is its creation
    time for snapshots written by this module. ``retention <= 0`` disables
    pruning.
    """
    days = retention_days() if retention is None else retention
    if days <= 0 or not directory.is_dir():
        return []
    cutoff = ((now or datetime.now().astimezone()) - timedelta(days=days)).timestamp()
    removed: list[Path] = []
    for candidate in directory.iterdir():
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed.append(candidate)
        except OSError:
            continue
    return removed


def _ensure_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def find_identical(directory: Path, data: bytes) -> Path | None:
    """Return a snapshot in ``directory`` that holds exactly ``data``, if any."""
    digest = hashlib.sha256(data).hexdigest()
    size = len(data)
    for candidate in directory.iterdir():
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size != size:
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                return candidate
        except OSError:
            continue
    return None


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate
