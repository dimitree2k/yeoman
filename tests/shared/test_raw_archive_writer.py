"""V1 spec §4.0: append-only JSONL, UTC month files, spool on failure (S26)."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_shared.raw_archive import writer as writer_module
from yeoman_shared.raw_archive.records import append_line, file_digest, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent, month_of, read_start_ms

NOW = 1_790_000_000_000  # 2026-09-21 UTC


def _archive(tmp_path: Path, clock: int = NOW) -> RawArchive:
    return RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "raw-spool",
        status_path=tmp_path / "run" / "raw-archive.json",
        clock=lambda: clock,
    )


def _event(text: str = "hi", **overrides: object) -> RawEvent:
    values: dict[str, object] = {
        "channel": "whatsapp",
        "kind": "message",
        "direction": "in",
        "native": {"type": "message", "payload": {"text": text}},
        "native_id": "evt-1",
        "chat_id": "chat@g.us",
        "account": "acc",
    }
    values.update(overrides)
    return RawEvent(**values)  # type: ignore[arg-type]


def _lines(path: Path) -> list[dict]:
    return [record for _, record, _ in iter_records(path) if record is not None]


def _media_description(message_id: str = "m1", *, generated_ms: int = NOW) -> dict:
    return {
        "derived_version": 1,
        "kind": "media_description",
        "channel": "whatsapp",
        "chat_id": "chat@g.us",
        "native_message_id": message_id,
        "mode": "description",
        "generator": "model",
        "generated_ms": generated_ms,
        "text": f"description-{message_id}",
    }


def test_media_description_appends_to_fixed_derived_file(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    record = _media_description()
    assert archive.append_media_description(record) is True
    target = tmp_path / "raw" / "derived" / "media-descriptions.jsonl"
    assert target.is_file()
    assert _lines(target) == [record]
    assert not (tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl").exists()
    with pytest.raises(TypeError):
        archive.append_media_description(record, "elsewhere.jsonl")  # type: ignore[call-arg]


def test_media_description_spool_retains_destination_and_fifo_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    real_append = writer_module.append_line

    def fail_derived(path: Path, line: str, **kwargs: object) -> bool:
        if path.name == "media-descriptions.jsonl":
            raise OSError("injected derived failure")
        return real_append(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_derived)
    assert archive.append_media_description(_media_description("m1")) is False
    assert archive.append_media_description(_media_description("m2")) is False
    [first, second] = sorted(archive.spool.glob("*.json"))
    envelopes = [json.loads(path.read_text()) for path in (first, second)]
    assert [entry["destination"] for entry in envelopes] == [
        "derived/media-descriptions.jsonl",
        "derived/media-descriptions.jsonl",
    ]
    monkeypatch.setattr(writer_module, "append_line", real_append)
    assert archive.drain_spool() == 2
    target = tmp_path / "raw" / "derived" / "media-descriptions.jsonl"
    assert [record["native_message_id"] for record in _lines(target)] == ["m1", "m2"]


def test_media_description_full_queue_raises_without_evicting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_module, "MAX_MEMORY_PENDING", 1)
    archive = _archive(tmp_path)
    archive.spool.write_text("not a directory", encoding="utf-8")
    real_append = writer_module.append_line

    def fail_all(path: Path, line: str, **kwargs: object) -> bool:
        raise OSError("injected storage failure")

    monkeypatch.setattr(writer_module, "append_line", fail_all)
    assert archive.append_media_description(_media_description("m1")) is False
    before = list(archive._pending)
    with pytest.raises(writer_module.RawArchiveCapacityError):
        archive.append_media_description(_media_description("m2"))
    assert archive._pending == before
    assert json.loads(archive._pending[0][2])["native_message_id"] == "m1"
    monkeypatch.setattr(writer_module, "append_line", real_append)


def test_legacy_monthly_spool_entry_still_drains(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    line = json.dumps({"channel": "whatsapp", "received_ms": NOW, "native_id": "legacy"})
    archive.spool.mkdir()
    (archive.spool / f"{NOW:013d}-legacy.json").write_text(
        json.dumps({"channel": "whatsapp", "received_ms": NOW, "line": line}),
        encoding="utf-8",
    )
    assert archive.drain_spool() == 1
    assert _lines(tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl")[0]["native_id"] == "legacy"


def test_append_writes_one_record_with_all_fields(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    assert archive.append(_event()) is True
    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    [record] = _lines(month_file)
    assert record["archive_version"] == 1
    assert record["received_ms"] == NOW
    assert record["channel"] == "whatsapp"
    assert record["kind"] == "message"
    assert record["direction"] == "in"
    assert record["native_id"] == "evt-1"
    assert record["chat_id"] == "chat@g.us"
    assert record["provenance"] == "native"
    assert record["native"]["payload"]["text"] == "hi"
    assert month_file.read_text(encoding="utf-8").count("\n") == 1


def test_month_bucket_uses_utc_boundaries(tmp_path: Path) -> None:
    last_ms = int(datetime(2026, 10, 31, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)
    archive = _archive(tmp_path)
    archive.append(_event(received_ms=last_ms))
    archive.append(_event(received_ms=last_ms + 1))
    assert (tmp_path / "raw" / "whatsapp" / "2026-10.jsonl").is_file()
    assert (tmp_path / "raw" / "whatsapp" / "2026-11.jsonl").is_file()


def test_files_and_directories_are_private(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.append(_event())
    root = tmp_path / "raw"
    month_file = root / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "whatsapp").stat().st_mode) == 0o700
    assert stat.S_IMODE(month_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "START").stat().st_mode) == 0o444


def test_start_marker_is_written_once(tmp_path: Path) -> None:
    _archive(tmp_path, clock=NOW)
    _archive(tmp_path, clock=NOW + 5_000)
    assert read_start_ms(tmp_path / "raw") == NOW


def test_non_json_values_are_serialized(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    native = {"date": datetime(2026, 10, 1, tzinfo=UTC), "blob": b"\x00\x01", "tags": {"a"}}
    assert archive.append(_event(native=native)) is True
    [record] = _lines(tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl")
    assert record["native"]["date"] == "2026-10-01T00:00:00+00:00"
    assert record["native"]["blob"] == {"__b64__": "AAE="}
    assert record["native"]["tags"] == ["a"]


def test_concurrent_appends_produce_parseable_lines(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(lambda i: archive.append(_event(text=f"m{i}", native_id=f"e{i}")), range(400))
        )
    records = _lines(tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl")
    assert len(records) == 400
    assert {r["native_id"] for r in records} == {f"e{i}" for i in range(400)}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_write_failure_spools_then_drains_in_order(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    root = tmp_path / "raw"
    os.chmod(root, 0o500)  # channel directory can no longer be created
    try:
        assert archive.append(_event(text="first", native_id="e1")) is False
        status = json.loads((tmp_path / "run" / "raw-archive.json").read_text())
        assert status["state"] == "degraded"
        assert status["spooled"] == 1
    finally:
        os.chmod(root, 0o700)
    assert archive.append(_event(text="second", native_id="e2")) is True
    records = _lines(root / "whatsapp" / f"{month_of(NOW)}.jsonl")
    assert [r["native_id"] for r in records] == ["e1", "e2"]
    assert list((tmp_path / "raw-spool").glob("*.json")) == []
    assert archive.status().state == "ok"
    status = json.loads((tmp_path / "run" / "raw-archive.json").read_text())
    assert status["state"] == "ok"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_spool_failure_falls_back_to_memory(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    (tmp_path / "raw-spool").write_text("not a directory")
    root = tmp_path / "raw"
    os.chmod(root, 0o500)
    try:
        assert archive.append(_event(native_id="e1")) is False
        assert archive.status().pending_in_memory == 1
    finally:
        os.chmod(root, 0o700)
    assert archive.append(_event(native_id="e2")) is True
    records = _lines(root / "whatsapp" / f"{month_of(NOW)}.jsonl")
    assert [r["native_id"] for r in records] == ["e1", "e2"]
    assert archive.status().pending_in_memory == 0


@pytest.mark.parametrize("contents", [b"{not json", b"\xff"])
def test_corrupt_spool_file_is_set_aside_not_deleted(tmp_path: Path, contents: bytes) -> None:
    spool = tmp_path / "raw-spool"
    spool.mkdir()
    (spool / "0001-bad.json").write_bytes(contents)
    archive = _archive(tmp_path)
    assert archive.append(_event(native_id="e2")) is True
    assert (spool / "0001-bad.json.corrupt").is_file()
    assert list(spool.glob("*.json")) == []
    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert [record["native_id"] for record in _lines(month_file)] == ["e2"]


def test_append_line_follows_a_replaced_file(tmp_path: Path) -> None:
    path = tmp_path / "f.jsonl"
    append_line(path, '{"a":1}')
    replacement = tmp_path / "f.tmp"
    replacement.write_text('{"b":2}\n')
    os.replace(replacement, path)
    append_line(path, '{"c":3}')
    assert path.read_text().splitlines() == ['{"b":2}', '{"c":3}']
    digest, lines, size = file_digest(path)
    assert lines == 2 and size == len('{"b":2}\n{"c":3}\n') and len(digest) == 64


def test_newlines_inside_a_line_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        append_line(tmp_path / "f.jsonl", "a\nb")


def test_partial_write_is_separated_before_spooled_record_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    real_write = os.write
    partial = b""
    calls = 0

    def fail_after_partial(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls, partial
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == month_file:
            calls += 1
            if calls == 1:
                partial = bytes(data[:17])
                return real_write(fd, partial)
            if calls == 2:
                raise OSError("injected short-write failure")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_after_partial)
    assert archive.append(_event(native_id="e1")) is False
    assert archive.append(_event(native_id="e2")) is True

    rows = list(iter_records(month_file))
    assert rows[0][1] is None
    assert [record["native_id"] for _, record, _ in rows if record is not None] == ["e1", "e2"]
    assert month_file.read_bytes().startswith(partial + b"\n")


def test_spool_unlink_failure_keeps_entry_and_defers_current_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected month failure")
        real_append_line(path, line, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("yeoman_shared.raw_archive.writer.append_line", fail_month)
        assert archive.append(_event(native_id="e1")) is False

    [old_entry] = list(archive.spool.glob("*.json"))
    real_unlink = Path.unlink
    failed = False

    def fail_old_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path == old_entry and not failed:
            failed = True
            raise PermissionError("injected spool unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    assert archive.append(_event(native_id="e2")) is False

    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert [record["native_id"] for record in _lines(month_file)] == ["e1"]
    spool_records = [json.loads(path.read_text()) for path in archive.spool.glob("*.json")]
    assert {json.loads(record["line"])["native_id"] for record in spool_records} == {"e1", "e2"}
    assert archive.status().spooled == 2


def test_malformed_spool_line_quarantine_failure_preserves_new_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "raw-spool"
    spool.mkdir()
    malformed = spool / "0001-malformed.json"
    malformed.write_text(
        json.dumps({"channel": "whatsapp", "received_ms": NOW, "line": '{"bad":1}\n{"bad":2}'}),
        encoding="utf-8",
    )
    archive = _archive(tmp_path)
    real_replace = os.replace
    quarantines: list[Path] = []

    def fail_quarantine(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        destination_path = Path(destination)
        if destination_path.name.endswith(".corrupt"):
            quarantines.append(destination_path)
            raise PermissionError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_quarantine)
    assert archive.append(_event(native_id="e2")) is False

    assert malformed.is_file()
    assert quarantines == [malformed.with_name(malformed.name + ".corrupt")]
    spool_records = [json.loads(path.read_text()) for path in spool.glob("*.json")]
    assert any(
        json.loads(record["line"]).get("native_id") == "e2"
        for record in spool_records
        if record["line"] != '{"bad":1}\n{"bad":2}'
    )


def test_spool_file_and_new_month_name_are_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected month failure")
        real_append_line(path, line, **kwargs)

    events: list[tuple[str, Path]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def track_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        kind = "dir-fsync" if stat.S_ISDIR(mode) else "file-fsync"
        events.append((kind, Path(os.readlink(f"/proc/self/fd/{fd}"))))
        real_fsync(fd)

    def track_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        destination_path = Path(destination)
        if destination_path.parent == archive.spool:
            events.append(("spool-rename", destination_path))
        real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr("yeoman_shared.raw_archive.writer.append_line", fail_month)
        patch.setattr(os, "fsync", track_fsync)
        patch.setattr(os, "replace", track_replace)
        assert archive.append(_event(native_id="e1")) is False

    spool_rename = next(i for i, (kind, _) in enumerate(events) if kind == "spool-rename")
    temp_fsync = max(
        i
        for i, (kind, path) in enumerate(events[:spool_rename])
        if kind == "file-fsync" and path.parent == archive.spool
    )
    dir_fsync = next(
        i
        for i, (kind, path) in enumerate(events[spool_rename + 1 :], spool_rename + 1)
        if kind == "dir-fsync" and path == archive.spool
    )
    assert temp_fsync < spool_rename < dir_fsync

    fresh_archive = _archive(tmp_path / "month-check")
    month_syncs: list[tuple[str, Path]] = []

    def track_month_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        kind = "dir" if stat.S_ISDIR(mode) else "file"
        month_syncs.append((kind, Path(os.readlink(f"/proc/self/fd/{fd}"))))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_month_fsync)
    assert fresh_archive.append(_event(native_id="month")) is True
    month_file = tmp_path / "month-check" / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    file_sync = next(
        i for i, (kind, path) in enumerate(month_syncs) if kind == "file" and path == month_file
    )
    dir_sync = next(
        i
        for i, (kind, path) in enumerate(month_syncs)
        if kind == "dir" and path == month_file.parent
    )
    assert file_sync < dir_sync


@pytest.mark.asyncio
async def test_capacity_stops_intake_and_recovers_after_storage_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_module, "MAX_MEMORY_PENDING", 1)
    archive = _archive(tmp_path)
    archive.spool.write_text("not a directory", encoding="utf-8")
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(writer_module, "append_line", fail_month)
        assert archive.append(_event(text="retained", native_id="e1")) is False
        with pytest.raises(writer_module.RawArchiveCapacityError):
            archive.append(_event(text="rejected direct", native_id="e2"))
        with pytest.raises(writer_module.RawArchiveCapacityError):
            await writer_module.append_async(archive, _event(text="rejected async", native_id="e3"))

        status = archive.status()
        assert status.state == "blocked"
        assert status.spooled == 0
        assert status.pending_in_memory == 1
        assert "rejected direct" not in status.last_error
        assert "rejected async" not in status.last_error
        status_text = archive.status_path.read_text(encoding="utf-8")
        assert '"state": "blocked"' in status_text
        assert "retained" not in status_text
        assert "rejected direct" not in status_text
        assert "rejected async" not in status_text

    archive.spool.unlink()
    assert archive.append(_event(text="accepted after repair", native_id="e4")) is True
    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert [record["native_id"] for record in _lines(month_file)] == ["e1", "e4"]
    assert archive.status().state == "ok"
    assert archive.status().pending_in_memory == 0


def test_recovered_spool_persists_pending_before_accepting_new_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_module, "MAX_MEMORY_PENDING", 1)
    archive = _archive(tmp_path)
    archive.spool.write_text("not a directory", encoding="utf-8")
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    def spooled_ids() -> list[str]:
        return [
            json.loads(json.loads(path.read_text(encoding="utf-8"))["line"])["native_id"]
            for path in sorted(archive.spool.glob("*.json"))
        ]

    with monkeypatch.context() as patch:
        patch.setattr(writer_module, "append_line", fail_month)
        assert archive.append(_event(native_id="e1", received_ms=NOW - 2)) is False

        archive.spool.unlink()
        archive.spool.mkdir(mode=0o700)
        assert archive.append(_event(native_id="e2", received_ms=NOW - 1)) is False
        assert archive.status().state == "degraded"
        assert archive.status().pending_in_memory == 1
        assert spooled_ids() == ["e1"]

        assert archive.append(_event(native_id="e3", received_ms=NOW)) is False
        assert archive.status().state == "degraded"
        assert archive.status().pending_in_memory == 1
        assert spooled_ids() == ["e1", "e2"]

    assert archive.append(_event(native_id="e4", received_ms=NOW + 1)) is True
    month_file = tmp_path / "raw" / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert [record["native_id"] for record in _lines(month_file)] == ["e1", "e2", "e3", "e4"]
    assert archive.status().state == "ok"


def test_invalid_audit_blocks_archive_append_and_preserves_spool(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    audit = archive.root / "AUDIT"
    audit.write_text("not-json\n", encoding="utf-8")

    assert archive.append(_event("must stay deferred")) is False
    month_file = archive.root / "whatsapp" / f"{month_of(NOW)}.jsonl"
    assert _lines(month_file) == []
    [spooled] = archive.spool.glob("*.json")
    assert archive.drain_spool() == 0
    assert spooled.is_file()
    assert _lines(month_file) == []


def test_owner_append_receipt_is_after_fsync_and_inode_recheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yeoman_gateway.history.layer1 import write_jsonl_once
    from yeoman_shared.raw_archive import records
    from yeoman_shared.raw_archive.paths import ProtectedPathError
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge

    assert hasattr(records, "append_owner_record") and hasattr(records, "CommittedLine")
    root = tmp_path / "home" / "data" / "raw"
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    record = {"attestation_version": 2, "type": "author", "at_ms": NOW, "note": "synthetic", "by": "owner",
              "source_ref": "whatsapp/2026-09.jsonl#1", "anchor": "111",
              "channel": "whatsapp", "chat_id": "chat@g.us", "native_message_id": "erase"}
    target = root / "owner" / "attestations.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'{"partial":')
    real_fsync, real_flock = os.fsync, records.fcntl.flock
    locks, synced = [], []
    replaced = False

    def flock(fd, op):
        nonlocal replaced
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        locks.append(path.name)
        real_flock(fd, op)
        if path == target and not replaced:
            replacement = target.with_suffix(".new")
            replacement.write_bytes(b'{}\n{"partial":')
            os.replace(replacement, target)
            replaced = True

    def fsync(fd):
        real_fsync(fd)
        synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))

    monkeypatch.setattr(records.fcntl, "flock", flock)
    monkeypatch.setattr(os, "fsync", fsync)
    receipt = records.append_owner_record(root, record)
    assert receipt == records.CommittedLine("owner/attestations.jsonl", 3, target.stat().st_size)
    assert locks[:3] == [records.PURGE_DISPOSITION_LOCK, target.name, target.name]
    assert target in synced and target.parent in synced
    assert json.loads(target.read_bytes().splitlines()[-1]) == record
    inode = target.stat().st_ino
    result = purge(root, PurgeSelector(channel="whatsapp", chat_id="chat@g.us", native_id="erase"), operator="owner")
    assert result.removed_lines == 1 and target.stat().st_ino != inode
    assert json.loads(target.read_bytes().splitlines()[-1]) == records.TOMBSTONE
    record = {**record, "native_message_id": "keep"}
    receipt = records.append_owner_record(root, record)
    assert receipt == records.CommittedLine("owner/attestations.jsonl", 4, target.stat().st_size)
    disposed = {**record, "channel": "whatsapp", "chat_id": "chat@g.us", "native_message_id": "erase"}
    before = target.read_bytes()
    assert records.append_owner_record(root, disposed) is None
    assert target.read_bytes() == before
    with pytest.raises(ProtectedPathError):
        write_jsonl_once(target, [record])
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(OSError, match="fsync failed"):
        records.append_owner_record(root, record)
    monkeypatch.setattr(os, "fsync", real_fsync)
    real_stat = records.os.stat

    def stale_after_sync(path, *args, **kwargs):
        if Path(path) == target and target in synced:
            replacement = target.with_suffix(".new")
            replacement.write_bytes(before)
            os.replace(replacement, target)
            synced.clear()
        return real_stat(path, *args, **kwargs)

    synced.clear()
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(records.os, "stat", stale_after_sync)
    with pytest.raises(OSError, match="replaced"):
        records.append_owner_record(root, record)


def test_append_durable_distinguishes_spool_memory_and_recovery(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    append_line = archive._append_archive_line
    spool_line = archive._spool_line_locked

    def failed_archive(*args, **kwargs):
        raise OSError("synthetic archive unavailable")

    monkeypatch.setattr(archive, "_append_archive_line", failed_archive)
    assert archive.append_durable(_event("spooled")) is True
    assert list(archive.spool.glob("*.json"))
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args, **kwargs: False)
    assert archive.append_durable(_event("memory")) is False
    assert not archive._pending
    monkeypatch.setattr(archive, "_append_archive_line", append_line)
    monkeypatch.setattr(archive, "_spool_line_locked", spool_line)
    assert archive.append_durable(_event("recovered")) is True
    assert [r["native"]["payload"]["text"] for r in _lines(archive._month_file("whatsapp", NOW))] == [
        "spooled", "recovered"]


def test_transcript_spool_capacity_and_purge(tmp_path, monkeypatch):
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    from yeoman_shared.raw_archive.records import TOMBSTONE

    archive = _archive(tmp_path)
    record = {
        "raw_archive_version": 1, "kind": "media_transcript", "provenance": "derived_only",
        "channel": "whatsapp", "chat_id": "chat@g.us", "native_message_id": "m1",
        "generator": "asr-model", "generated_ms": NOW, "text": "synthetic transcript",
    }
    real_append = writer_module.append_line
    def fail_all(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(writer_module, "append_line", fail_all)
    assert archive.append_media_transcript(record) is True  # Durable spool, not memory.
    envelope = json.loads(next(archive.spool.glob("*.json")).read_text())
    assert envelope["destination"] == "derived/media-transcripts.jsonl"
    monkeypatch.setattr(writer_module, "append_line", real_append)
    assert archive.drain_spool() == 1
    target = archive.root / "derived/media-transcripts.jsonl"
    assert _lines(target) == [record]
    # Persist another generation before purge, then recover with a fresh writer.
    monkeypatch.setattr(writer_module, "append_line", fail_all)
    assert archive.append_media_transcript(record) is True
    monkeypatch.setattr(writer_module, "append_line", real_append)
    assert len(list(archive.spool.glob("*.json"))) == 1
    purge(archive.root, PurgeSelector(channel="whatsapp", native_id="m1"),
          operator="synthetic", now_ms=NOW + 1)
    assert _lines(target) == [TOMBSTONE]
    previous = archive
    archive = _archive(tmp_path)
    assert archive is not previous and archive.root == previous.root and archive.spool == previous.spool
    assert archive.drain_spool() == 1
    assert _lines(target) == [TOMBSTONE]
    assert "synthetic transcript" not in target.read_text()
    assert not list(archive.spool.glob("*.json"))
    assert archive.append_media_transcript(record | {"generated_ms": NOW + 2}) is True
    assert _lines(target) == [TOMBSTONE]
    archive.spool.rmdir()
    archive.spool.write_text("not a directory")
    monkeypatch.setattr(writer_module, "MAX_MEMORY_PENDING", 1)
    monkeypatch.setattr(writer_module, "append_line", fail_all)
    assert archive.append_media_transcript(record | {"native_message_id": "m2"}) is False
    before = list(archive._pending)
    with pytest.raises(writer_module.RawArchiveCapacityError):
        archive.append_media_transcript(record | {"native_message_id": "m3"})
    assert archive._pending == before
