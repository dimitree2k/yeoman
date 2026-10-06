"""Helpers shared by the backfill converters. Sources are always opened read-only."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_ZONE = ZoneInfo("Europe/Berlin")
_BATCH_PREFIX = re.compile(r"^\s*\[group_notes_batch\]\s*")
_DESCRIPTION = re.compile(r"\s*\[image_description\]\s*", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"^\s*\[(image|video|gif|sticker|document|audio|voice message|voice|media)\]\s*",
    re.IGNORECASE,
)
_MEDIA_KIND = {
    "image": "image", "video": "video", "gif": "video", "sticker": "sticker",
    "document": "document", "audio": "audio", "voice": "audio", "voice message": "audio",
    "media": "unknown",
}


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    return row.fetchone() is not None


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def media_kind(placeholder: str) -> str:
    return _MEDIA_KIND.get(placeholder.lower(), "unknown")


@dataclass(frozen=True)
class Cleaned:
    text: str | None
    description: str | None
    placeholder: str | None
    changed: bool


def clean_text(value: Any) -> Cleaned:
    """Split Yeoman's additions off a stored text: batch prefix, image description, placeholder."""
    if value is None:
        return Cleaned(None, None, None, False)
    text = str(value)
    stripped = _BATCH_PREFIX.sub("", text, count=1)
    changed = stripped != text
    text = stripped
    description = None
    parts = _DESCRIPTION.split(text, maxsplit=1)
    if len(parts) == 2:
        text, description, changed = parts[0], parts[1].strip() or None, True
    placeholder = None
    match = _PLACEHOLDER.match(text)
    if match:
        placeholder, text, changed = match.group(1).lower(), text[match.end():], True
    return Cleaned(text.strip() or None, description, placeholder, changed)


def epoch_or_iso_to_ms(value: Any) -> tuple[int | None, str]:
    if value is None or value == "" or isinstance(value, bool):
        return None, "unknown"
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = int(float(value))
        return (number * 1000 if number < 100_000_000_000 else number), "provider_timestamp"
    try:
        moment = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None, "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_ZONE)
    return int(moment.timestamp() * 1000), "capture_time_approx"
