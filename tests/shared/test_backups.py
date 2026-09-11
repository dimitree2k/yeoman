"""Tests for timestamped, retention-pruned runtime file backups."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeoman_shared.utils import backups


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _age(path: Path, days: int, now: datetime) -> None:
    stamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_backup_file_snapshots_into_category_dir(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    _write(config, '{"a": 1}')

    snapshot = backups.backup_file(config, category="config")

    assert snapshot is not None
    assert snapshot.parent == tmp_path / "backups" / "config"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{6}[+-]\d{4}-config\.json", snapshot.name)
    assert snapshot.read_bytes() == config.read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_backup_file_skips_content_that_is_already_retained(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    _write(config, '{"a": 1}')
    assert backups.backup_file(config, category="config") is not None

    _write(config, '{"a": 2}')
    assert backups.backup_file(config, category="config") is not None

    # Ping-pong back to the first revision: the retained snapshot is reused.
    _write(config, '{"a": 1}')
    assert backups.backup_file(config, category="config") is None
    assert len(list((tmp_path / "backups" / "config").iterdir())) == 2


def test_backup_file_missing_source_is_a_noop(tmp_path: Path) -> None:
    assert backups.backup_file(tmp_path / "missing.json", category="config") is None
    assert not (tmp_path / "backups").exists()


def test_prune_backups_removes_snapshots_older_than_retention(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).astimezone()
    directory = tmp_path / "backups" / "config"
    directory.mkdir(parents=True)
    fresh = directory / "2026-09-11T120000+0200-config.json"
    stale = directory / "2026-01-01T120000+0100-config.json"
    _write(fresh, "{}")
    _write(stale, "{}")
    _age(stale, 40, now)

    removed = backups.prune_backups(directory, retention=30, now=now)

    assert removed == [stale]
    assert fresh.is_file()
    assert not stale.exists()


def test_backup_file_prunes_expired_snapshots(tmp_path: Path) -> None:
    directory = tmp_path / "backups" / "config"
    directory.mkdir(parents=True)
    stale = directory / "2026-01-01T120000+0100-config.json"
    _write(stale, '{"old": true}')
    _age(stale, 40, datetime.now(timezone.utc).astimezone())
    config = tmp_path / "config.json"
    _write(config, '{"a": 1}')

    assert backups.backup_file(config, category="config", retention=30) is not None
    assert not stale.exists()


def test_prune_is_disabled_when_retention_is_zero(tmp_path: Path) -> None:
    directory = tmp_path / "backups" / "config"
    directory.mkdir(parents=True)
    stale = directory / "2026-01-01T120000+0100-config.json"
    _write(stale, "{}")
    _age(stale, 400, datetime.now(timezone.utc).astimezone())

    assert backups.prune_backups(directory, retention=0) == []
    assert stale.is_file()


def test_retention_days_default_env_and_invalid_values(monkeypatch) -> None:
    monkeypatch.delenv(backups.RETENTION_ENV_VAR, raising=False)
    assert backups.retention_days() == backups.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv(backups.RETENTION_ENV_VAR, "7")
    assert backups.retention_days() == 7
    monkeypatch.setenv(backups.RETENTION_ENV_VAR, "0")
    assert backups.retention_days() == 0
    monkeypatch.setenv(backups.RETENTION_ENV_VAR, "nonsense")
    assert backups.retention_days() == backups.DEFAULT_RETENTION_DAYS


def test_find_identical_returns_the_retained_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "backups" / "policy"
    stored = backups.write_backup_bytes(directory, label="policy", data=b"{}")

    assert stored is not None
    assert backups.find_identical(directory, b"{}") == stored
    assert backups.find_identical(directory, b'{"x": 1}') is None
