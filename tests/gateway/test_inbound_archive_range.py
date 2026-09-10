from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yeoman_gateway.storage.inbound_archive import InboundArchive


def _seed_messages(archive: InboundArchive, base_ts: int, count: int) -> None:
    """Insert count messages at 60-second intervals starting at base_ts."""
    for i in range(count):
        archive.record_inbound(
            channel="whatsapp",
            chat_id="group@g.us",
            message_id=f"m-{i}",
            participant=None,
            sender_id=f"user-{i % 3}",
            text=f"message {i}",
            timestamp=base_ts + i * 60,
            sender_name=f"User{i % 3}",
        )


def test_lookup_in_range_returns_messages_in_window(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "test.db")
    now = datetime.now(UTC)
    base_ts = int(now.timestamp()) - 7200  # 2 hours ago

    _seed_messages(archive, base_ts, 10)

    since = datetime.fromtimestamp(base_ts + 120, tz=UTC)  # skip first 2
    until = datetime.fromtimestamp(base_ts + 420, tz=UTC)  # up to msg 7 (inclusive)

    rows = archive.lookup_messages_in_range("whatsapp", "group@g.us", since, until)
    # Messages at timestamps 120,180,240,300,360,420 = m-2 through m-7
    assert len(rows) == 6
    assert rows[0]["message_id"] == "m-2"
    assert rows[-1]["message_id"] == "m-7"


def test_lookup_in_range_respects_limit(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "test.db")
    now = datetime.now(UTC)
    base_ts = int(now.timestamp()) - 7200

    _seed_messages(archive, base_ts, 50)

    since = datetime.fromtimestamp(base_ts, tz=UTC)
    rows = archive.lookup_messages_in_range(
        "whatsapp", "group@g.us", since, limit=5
    )
    assert len(rows) == 5
    assert rows[0]["message_id"] == "m-0"


def test_lookup_in_range_returns_empty_for_no_matches(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "test.db")
    future = datetime.now(UTC) + timedelta(hours=1)

    rows = archive.lookup_messages_in_range("whatsapp", "group@g.us", future)
    assert rows == []


def test_lookup_in_range_defaults_until_to_now(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "test.db")
    now = datetime.now(UTC)
    base_ts = int(now.timestamp()) - 300  # 5 min ago

    _seed_messages(archive, base_ts, 5)

    since = datetime.fromtimestamp(base_ts, tz=UTC)
    rows = archive.lookup_messages_in_range("whatsapp", "group@g.us", since)
    assert len(rows) == 5


def test_lookup_in_range_caps_limit_at_300(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "test.db")
    now = datetime.now(UTC)
    base_ts = int(now.timestamp()) - 36000

    _seed_messages(archive, base_ts, 350)

    since = datetime.fromtimestamp(base_ts, tz=UTC)
    rows = archive.lookup_messages_in_range(
        "whatsapp", "group@g.us", since, limit=999
    )
    assert len(rows) == 300


def test_keep_forever_mode_never_purges(tmp_path) -> None:
    """The running gateway archives every message and deletes none of them."""
    archive = InboundArchive(db_path=tmp_path / "keep.db", retention_days=None)
    _seed_messages(archive, 0, 5)  # timestamp 0: older than any window

    assert archive.purge_older_than(days=1) == 0
    archive._maybe_purge_locked()
    archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="m-new",
        participant=None,
        sender_id="user-1",
        text="newest",
        timestamp=0,
        sender_name="User1",
    )

    rows = archive._conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
    assert rows == 6
    archive.close()


def test_timed_mode_still_purges_when_asked(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "timed.db", retention_days=30)
    _seed_messages(archive, 0, 4)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    archive._conn.execute("UPDATE inbound_messages SET created_at = ?", (old,))
    archive._conn.commit()

    assert archive.purge_older_than(days=1) == 4
    assert archive._conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0] == 0
    archive.close()


def test_zero_retention_means_keep_everything(tmp_path) -> None:
    archive = InboundArchive(db_path=tmp_path / "zero.db", retention_days=0)

    assert archive.retention_days is None
    archive.close()
