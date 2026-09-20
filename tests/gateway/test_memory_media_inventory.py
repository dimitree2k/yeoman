"""Phase 2 / Task 5: read-only media growth report.

The report measures growth so that retention can be a later, explicit decision.  It opens
no media content, parses no document, runs no OCR, recomputes no hash, deletes no file, and
introduces neither a daemon, a purge flag nor retention configuration.  It reads stored
event metadata plus the filesystem's own size information.

Offline and synthetic: temporary directories and an injected stat function.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from yeoman_gateway.cli.memory_commands import (
    MEDIA_AGE_BUCKETS,
    MEDIA_STATES,
    collect_media_references,
    media_age_bucket,
    media_growth_rows,
)

NOW = 1_700_000_000_000
DAY = 86_400_000


def _reference(kind: str, path: str, *, age_days: int) -> dict[str, object]:
    return {
        "kind": kind,
        "path": path,
        "occurred_ms": NOW - age_days * DAY,
    }


def test_age_buckets_cover_the_configured_ranges() -> None:
    assert [label for _limit, label in MEDIA_AGE_BUCKETS] == ["0-7d", "8-30d", "31-90d", ">90d"]
    assert media_age_bucket(0) == "0-7d"
    assert media_age_bucket(7 * DAY) == "0-7d"
    assert media_age_bucket(8 * DAY) == "8-30d"
    assert media_age_bucket(31 * DAY) == "31-90d"
    assert media_age_bucket(400 * DAY) == ">90d"


def test_report_groups_by_kind_age_state_and_bytes() -> None:
    sizes = {
        "/media/a.jpg": 100,
        "/media/b.jpg": 200,
        "/media/c.pdf": 50,
    }
    references = [
        _reference("image", "/media/a.jpg", age_days=1),
        _reference("image", "/media/b.jpg", age_days=10),
        _reference("document", "/media/c.pdf", age_days=200),
        _reference("image", "/media/gone.jpg", age_days=1),
    ]

    rows = media_growth_rows(
        references, now_ms=NOW, stat_size=lambda path: sizes.get(path)
    )

    assert [(row.media_kind, row.age_bucket, row.state) for row in rows] == [
        ("document", ">90d", "present"),
        ("image", "0-7d", "missing"),
        ("image", "0-7d", "present"),
        ("image", "8-30d", "present"),
    ]
    by_key = {(row.media_kind, row.age_bucket, row.state): row for row in rows}
    assert by_key[("image", "0-7d", "missing")].count == 1
    assert by_key[("image", "0-7d", "missing")].bytes == 0
    assert by_key[("image", "0-7d", "present")].count == 1
    assert by_key[("image", "0-7d", "present")].bytes == 100
    assert by_key[("image", "8-30d", "present")].bytes == 200
    assert by_key[("document", ">90d", "present")].bytes == 50


def test_missing_files_are_reported_not_deleted(tmp_path: Path) -> None:
    real = tmp_path / "photo.jpg"
    real.write_bytes(b"x" * 42)
    references = [
        _reference("image", str(real), age_days=2),
        _reference("image", str(tmp_path / "absent.jpg"), age_days=2),
    ]

    def stat_size(path: str) -> int | None:
        try:
            return Path(path).stat().st_size
        except OSError:
            return None

    rows = media_growth_rows(references, now_ms=NOW, stat_size=stat_size)
    states = {row.state: row for row in rows}
    assert set(states) == set(MEDIA_STATES)
    assert states["present"].bytes == 42
    assert states["missing"].count == 1
    # Nothing was removed: both the file and the report survive.
    assert real.exists()


def test_report_never_opens_media_content(tmp_path: Path) -> None:
    """The only filesystem access is a size lookup; content is never read."""
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 top secret body")
    opened: list[str] = []

    def stat_size(path: str) -> int | None:
        opened.append(path)
        try:
            return secret.stat().st_size
        except OSError:  # pragma: no cover - defensive
            return None

    rows = media_growth_rows(
        [_reference("document", str(secret), age_days=100)],
        now_ms=NOW,
        stat_size=stat_size,
    )
    assert opened == [str(secret)]
    assert rows[0].bytes == len(b"%PDF-1.4 top secret body")
    # The report cannot leak content it never read.
    assert "top secret" not in json.dumps(
        [row.__dict__ if hasattr(row, "__dict__") else str(row) for row in rows]
    )


def test_report_reads_only_stored_event_metadata(tmp_path: Path) -> None:
    database = tmp_path / "processing.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE events (event_id TEXT, kind TEXT, occurred_ms INTEGER,"
        " payload_json TEXT)"
    )
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?)",
        (
            "e1",
            "message",
            NOW - DAY,
            json.dumps({"text": "private body", "media": {"kind": "image", "path": "/m/a.jpg"}}),
        ),
    )
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?)",
        ("e2", "message", NOW, json.dumps({"text": "no media here"})),
    )
    connection.commit()
    connection.close()

    references = collect_media_references(database)
    assert len(references) == 1
    assert references[0]["kind"] == "image"
    # Only metadata crosses the boundary; the message text is never returned.
    assert "private body" not in json.dumps(references)


def test_no_purge_flag_or_retention_configuration_is_introduced() -> None:
    import inspect

    from yeoman_gateway.cli import memory_commands as module

    function = module.memory_media_growth
    body = inspect.getsource(function)
    # Drop the docstring, which legitimately *says* it deletes nothing.
    docstring = function.__doc__ or ""
    body = body.replace(docstring, "")
    for forbidden in (".unlink(", "os.remove", "shutil", "rmtree", "purge", "retention"):
        assert forbidden not in body.lower()
    # The report writes nothing into the store either.
    assert "INSERT" not in body and "UPDATE" not in body and "DELETE" not in body
    # And the command exposes no purge or retention option at all.
    assert "purge" not in body.lower()


def test_report_is_deterministic_for_the_same_input() -> None:
    references = [
        _reference("audio", "/m/a.ogg", age_days=3),
        _reference("audio", "/m/b.ogg", age_days=3),
    ]
    sizes = {"/m/a.ogg": 10, "/m/b.ogg": 20}
    first = media_growth_rows(references, now_ms=NOW, stat_size=sizes.get)
    second = media_growth_rows(references, now_ms=NOW, stat_size=sizes.get)
    assert first == second
    assert first[0].count == 2
    assert first[0].bytes == 30
