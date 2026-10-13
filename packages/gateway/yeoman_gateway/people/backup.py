"""Owner-only snapshots of people.db under ~/.yeoman/backups/people/ (newest is never pruned)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from yeoman_shared.utils.backups import write_backup_bytes
from yeoman_shared.utils.helpers import get_data_path


def people_backup_dir() -> Path:
    return get_data_path() / "backups" / "people"


def snapshot_people_db(db_path: Path, *, label: str = "people", now: datetime | None = None,
                       directory: Path | None = None) -> Path | None:
    """Snapshot a consistent copy; None when the db is missing or the content is already retained."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        data = conn.serialize()
    finally:
        conn.close()
    return write_backup_bytes(directory or people_backup_dir(), label=label, data=data,
                              suffix=".db", now=now, keep_newest=1)
