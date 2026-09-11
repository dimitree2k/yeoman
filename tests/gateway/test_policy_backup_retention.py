"""Policy snapshots live under the shared backup root and are pruned there."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yeoman_gateway.policy.admin.audit import PolicyAuditStore
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig


def test_write_backup_lands_in_shared_backup_root(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    save_policy(PolicyConfig(), path)
    store = PolicyAuditStore(path)

    ref = store.write_backup("abc123", PolicyConfig())

    assert ref.startswith("backups/policy/")
    assert ref.endswith("-policy-abc123.json")
    assert (tmp_path / ref).is_file()
    assert not (tmp_path / "policy" / "audit" / "backups").exists()
    assert store.load_backup(ref).model_dump() == PolicyConfig().model_dump()


def test_write_backup_reuses_identical_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    save_policy(PolicyConfig(), path)
    store = PolicyAuditStore(path)

    first = store.write_backup("first", PolicyConfig())
    second = store.write_backup("second", PolicyConfig())

    assert first == second
    assert len(list((tmp_path / "backups" / "policy").iterdir())) == 1


def test_load_backup_reads_pre_consolidation_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    save_policy(PolicyConfig(), path)
    legacy_dir = tmp_path / "policy" / "audit" / "backups"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "deadbeef.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    store = PolicyAuditStore(path)

    ref = "backups/deadbeef.json"
    assert store.load_backup(ref).model_dump() == PolicyConfig().model_dump()


def test_write_backup_prunes_snapshots_older_than_retention(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    save_policy(PolicyConfig(), path)
    directory = tmp_path / "backups" / "policy"
    directory.mkdir(parents=True)
    stale = directory / "2026-01-01T120000+0100-policy-old.json"
    stale.write_text("{}", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(stale, (old, old))

    PolicyAuditStore(path).write_backup("new-change", PolicyConfig())

    assert not stale.exists()
