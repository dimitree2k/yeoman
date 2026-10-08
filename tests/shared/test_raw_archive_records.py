"""Durable Layer 1 receipts and pinned-prefix copies, using synthetic archives."""

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from yeoman_gateway.history.convert.run import prepare_import_manifest
from yeoman_gateway.history.layer1 import Origin, backfill_line
from yeoman_shared.raw_archive import records, writer
from yeoman_shared.raw_archive.purge import PurgeSelector, purge

NOW = 1_790_000_000_000


def archive_at(base):
    return writer.RawArchive(base / "raw", spool=base / "spool",
                             status_path=base / "status", clock=lambda: NOW)


def native():
    return writer.RawEvent("whatsapp", "message", "in", {"text": "synthetic"},
                           native_id="m1", chat_id="synthetic@g.us", received_ms=NOW)


def staged_at(base, relatives):
    for relative in relatives:
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        row = (backfill_line(channel="whatsapp", kind="message", provenance="native",
                            time_certainty="native", occurred_ms=NOW, direction="in",
                            chat_id="synthetic@g.us", payload={"messageId": "m1", "text": "synthetic"},
                            origin=Origin("snapshot", "synthetic.db", "messages", "1"), original={})
               if relative.startswith("backfill/") else
               {"kind": "media_description", "channel": "whatsapp", "chat_id": "synthetic@g.us",
                "native_message_id": "m1", "text": "synthetic"})
        path.write_text(json.dumps(row) + "\n")
    return prepare_import_manifest(base)


@pytest.mark.parametrize("route", ["native", "durable", "description", "transcript", "spool",
                                   "memory", "owner", "owner_locked", "derived", "backfill"])
def test_committed_notifications_cover_every_publication_path(tmp_path, monkeypatch, route):
    archive = archive_at(tmp_path)
    root = archive.root
    callbacks, syncs = [], []
    real_fsync = os.fsync

    def fsync(fd):
        real_fsync(fd)
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path.name.endswith(".import-partial"):
            path = path.with_name(path.name.removesuffix(".import-partial"))
        syncs.append(("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file",
                      path))

    def committed(receipt):
        target = root / receipt.relative_path
        assert ("file", target) in syncs
        assert ("directory", target.parent) in syncs
        assert syncs.index(("file", target)) < len(syncs)
        assert syncs.index(("directory", target.parent)) < len(syncs)
        syncs.append(("callback", target))
        blob = target.read_bytes()[:receipt.end_offset]
        assert blob.endswith(b"\n") and blob.count(b"\n") == receipt.line_number
        callbacks.append(receipt)

    monkeypatch.setattr(os, "fsync", fsync)
    archive.set_commit_callback(committed)
    assert callbacks == []
    if route in ("native", "durable"):
        assert (archive.append if route == "native" else archive.append_durable)(native())
    elif route in ("description", "transcript"):
        assert getattr(archive, "append_media_" + route)({"channel": "whatsapp", "generated_ms": NOW})
    elif route in ("spool", "memory"):
        with monkeypatch.context() as patch:
            def unavailable(*args, **kwargs):
                raise OSError("synthetic unavailable")
            patch.setattr(archive, "_append_archive_line", unavailable)
            if route == "memory":
                patch.setattr(archive, "_spool_line_locked", lambda *args: False)
            assert archive.append(native()) is False
            assert callbacks == []
            assert archive.status().pending_in_memory == int(route == "memory")
        archive.drain_spool()
    elif route.startswith("owner"):
        row = {"attestation_version": 2, "type": "synthetic"}
        if route == "owner_locked":
            fd = records.lock_file(root / records.PURGE_DISPOSITION_LOCK, create=True)
            try:
                receipt = records.append_owner_record_locked(root, row, on_committed=committed)
            finally:
                os.close(fd)
        else:
            receipt = records.append_owner_record(root, row, on_committed=committed)
        assert callbacks == [receipt]
    else:
        relative = "backfill/synthetic.jsonl" if route == "backfill" else "derived/media-descriptions.jsonl"
        staged = tmp_path / "staged"
        manifest = staged_at(staged, [relative])
        assert records.import_backfill(root, staged, manifest, on_committed=committed)["status"] == "complete"
        before = list(callbacks)
        records.import_backfill(root, staged, manifest, on_committed=committed)
        assert callbacks == before
    assert len(callbacks) == 1
    receipt = callbacks[0]
    target = root / receipt.relative_path
    assert callbacks == [records.CommittedLine(receipt.relative_path, 1, target.stat().st_size)]
    assert syncs.index(("file", target)) < syncs.index(("callback", target))
    assert syncs.index(("directory", target.parent)) < syncs.index(("callback", target))
    assert records.enumerate_committed(root)[0].end_offset == receipt.end_offset


def test_callback_failure_does_not_duplicate_durable_append(tmp_path, caplog):
    archive = archive_at(tmp_path)

    def failed(receipt):
        raise OSError("synthetic callback failure")

    archive.set_commit_callback(failed)
    assert archive.append(native()) is True
    assert archive.status().spooled == archive.status().pending_in_memory == 0
    boundary, = records.enumerate_committed(archive.root)
    assert boundary.line_number == 1
    assert "callback" in caplog.text
    assert records.append_owner_record(archive.root, {"attestation_version": 2, "type": "synthetic"},
                                       on_committed=failed).line_number == 1
    staged = tmp_path / "staged"
    manifest = staged_at(staged, ["backfill/synthetic.jsonl", "derived/media-descriptions.jsonl"])
    assert records.import_backfill(archive.root, staged, manifest, on_committed=failed)["status"] == "complete"
    assert all(b.line_number == 1 for b in records.enumerate_committed(archive.root))


def test_disposition_suppression_emits_no_notification(tmp_path):
    archive = archive_at(tmp_path)
    callbacks = []
    purge(archive.root, PurgeSelector(channel="whatsapp", chat_id="synthetic@g.us", native_id="m1"),
          operator="synthetic", now_ms=NOW + 1)
    archive.set_commit_callback(callbacks.append)
    assert archive.append(native()) is True
    assert callbacks == []
    assert records.append_owner_record(archive.root, {"attestation_version": 2, "type": "message_author",
        "message_id": "whatsapp:synthetic@g.us:m1"}, on_committed=callbacks.append) is None
    assert callbacks == []


def test_startup_enumeration_recovers_missed_notification(tmp_path):
    archive = archive_at(tmp_path)
    callbacks = []
    archive.set_commit_callback(callbacks.append)
    archive.set_commit_callback(None)
    assert archive.append(native())
    restarted = archive_at(tmp_path)
    for sub in ("backfill", "derived", "owner"):
        target = archive.root / sub / "synthetic.jsonl"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b'\nmalformed\n{"purged_version":1}\n')
    for relative in ("telegram/x.jsonl", "media/x.jsonl", "spool/x.jsonl", "AUDIT",
                     records.IMPORT_RECEIPTS, "derived/x.jsonl.import-partial", "manifest.json"):
        target = archive.root / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"excluded")
    boundaries = records.enumerate_committed(restarted.root)
    assert callbacks == []
    assert [b.relative_path for b in boundaries] == sorted([
        "whatsapp/2026-09.jsonl", "backfill/synthetic.jsonl", "derived/synthetic.jsonl", "owner/synthetic.jsonl"])
    for boundary in boundaries:
        blob = (archive.root / boundary.relative_path).read_bytes()
        assert boundary == records.SourceBoundary(boundary.relative_path, blob.count(b"\n"),
                                                   len(blob), hashlib.sha256(blob).hexdigest())


def test_committed_prefix_rejects_partial_tail(tmp_path):
    root = tmp_path / "raw"
    path = root / "whatsapp/synthetic.jsonl"
    records.append_line(path, "{}")
    before = records.enumerate_committed(root)
    with path.open("ab") as handle:
        handle.write(b"partial")
    blob = path.read_bytes()
    with pytest.raises(ValueError, match="incomplete"):
        records.enumerate_committed(root)
    assert path.read_bytes() == blob
    assert before[0].end_offset == 3


def test_partial_import_enumerates_durable_file(tmp_path, monkeypatch):
    root, staged = tmp_path / "raw", tmp_path / "staged"
    manifest = staged_at(staged, ["backfill/a.jsonl", "backfill/b.jsonl"])
    real_publish = records._publish_import_file
    callbacks = []

    def interrupted(path, *args, **kwargs):
        if path.name == "b.jsonl":
            raise OSError("synthetic interruption")
        return real_publish(path, *args, **kwargs)

    monkeypatch.setattr(records, "_publish_import_file", interrupted)
    with pytest.raises(OSError, match="interruption"):
        records.import_backfill(root, staged, manifest, on_committed=callbacks.append)
    assert records._import_receipt(root, manifest["package_digest"])["status"] == "partial"
    boundary, = records.enumerate_committed(root)
    assert boundary.relative_path == "backfill/a.jsonl"
    assert callbacks == [records.CommittedLine(boundary.relative_path, boundary.line_number, boundary.end_offset)]


def test_prefix_copy_releases_archive_lock_before_build(tmp_path, monkeypatch):
    archive = archive_at(tmp_path)
    assert archive.append(native())
    boundaries = records.enumerate_committed(archive.root)
    original = (archive.root / boundaries[0].relative_path).read_bytes()
    paused, release = Event(), Event()
    real_pread = os.pread

    def paused_read(fd, size, offset):
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == archive.root / boundaries[0].relative_path:
            paused.set()
            assert release.wait(10)
        return real_pread(fd, size, offset)

    monkeypatch.setattr(os, "pread", paused_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(records.copy_committed, archive.root, boundaries, tmp_path / "copy")
        try:
            assert paused.wait(10)
            monkeypatch.setattr(os, "pread", real_pread)
            assert pool.submit(archive.append, native()).result(timeout=10)
            # Purge replaces the inode; the retained descriptor must still copy the original.
            purge(archive.root, PurgeSelector(channel="whatsapp", native_id="m1"), operator="synthetic")
        finally:
            release.set()
        copying.result(timeout=10)
    assert (tmp_path / "copy" / boundaries[0].relative_path).read_bytes() == original


def test_prefix_copy_rejects_mutation_and_closes_descriptors(tmp_path):
    archive = archive_at(tmp_path)
    archive.append(native())
    boundaries = records.enumerate_committed(archive.root)
    target = archive.root / boundaries[0].relative_path
    blob = target.read_bytes()
    target.write_bytes(b"x" + blob[1:])
    before = len(list(Path("/proc/self/fd").iterdir()))
    with pytest.raises(ValueError, match="prefix"):
        records.copy_committed(archive.root, boundaries, tmp_path / "copy")
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    assert not (tmp_path / "copy" / boundaries[0].relative_path).exists()
    with pytest.raises(ValueError):
        records.copy_committed(archive.root, [records.SourceBoundary("../escape", 1, 1, "x")], tmp_path / "unsafe")
