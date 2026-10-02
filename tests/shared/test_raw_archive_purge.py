"""S30: owner purge removes only the selected lines and leaves an audit record."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from yeoman_shared.raw_archive.purge import PurgeSelector, plan_purge, purge
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.verify import AUDIT, latest_manifest, verify_archive
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

SEPT = 1_790_000_000_000
OCT = 1_791_500_000_000


def _setup(tmp_path: Path) -> tuple[Path, RawArchive]:
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "raw-spool",
        status_path=tmp_path / "run" / "raw-archive.json",
        clock=lambda: OCT,
    )
    return tmp_path / "raw", archive


def _message(
    archive: RawArchive, message_id: str, chat: str, ms: int, media: dict | None = None
) -> None:
    archive.append(
        RawEvent(
            channel="whatsapp",
            kind="message",
            direction="in",
            native={
                "type": "message",
                "payload": {
                    "messageId": message_id,
                    "chatJid": chat,
                    "text": f"secret-{message_id}",
                },
            },
            native_id=f"evt-{message_id}",
            chat_id=chat,
            received_ms=ms,
            media=media,
        )
    )


def _ids(root: Path) -> list[str]:
    return [
        r["native"]["payload"]["messageId"]
        for p in archive_files(root)
        for _, r, _ in iter_records(p)
        if r
    ]


def test_selector_requires_a_chat_or_a_message() -> None:
    with pytest.raises(ValueError):
        PurgeSelector(channel="whatsapp").validate()


def test_plan_is_a_dry_run(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    _message(archive, "m1", "c1", OCT)
    plan = plan_purge(root, PurgeSelector(channel="whatsapp", native_id="m1"))
    assert plan.removed_lines == 1
    assert _ids(root) == ["m1"]
    assert not (root / AUDIT).exists()


def test_purge_by_message_id_removes_only_that_line_and_audits(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    _message(archive, "m1", "c1", OCT)
    _message(archive, "m2", "c1", OCT)
    result = purge(
        root, PurgeSelector(channel="whatsapp", native_id="m1"), operator="dm", now_ms=OCT
    )
    assert result.removed_lines == 1
    assert _ids(root) == ["m2"]
    [audit] = [r for _, r, _ in iter_records(root / AUDIT) if r]
    assert audit["operator"] == "dm" and audit["removed_lines"] == 1
    assert audit["files"] == ["whatsapp/2026-10.jsonl"]
    assert "secret-m1" not in (root / AUDIT).read_text()  # scope and hashes, never content
    assert stat.S_IMODE((root / AUDIT).stat().st_mode) == 0o444


def test_purge_of_a_sealed_month_updates_the_manifest_and_verify_stays_green(
    tmp_path: Path,
) -> None:
    root, archive = _setup(tmp_path)
    _message(archive, "m1", "c1", SEPT)
    _message(archive, "m2", "c1", SEPT)
    run = tmp_path / "run"
    assert verify_archive(root, run_dir=run, now_ms=OCT).ok
    purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"),
        operator="dm",
        now_ms=OCT + 1,
    )
    sealed = root / "whatsapp" / "2026-09.jsonl"
    assert stat.S_IMODE(sealed.stat().st_mode) == 0o444
    assert latest_manifest(root)["whatsapp/2026-09.jsonl"]["lines"] == 1
    assert verify_archive(root, run_dir=run, now_ms=OCT + 2).ok


def test_purge_by_chat_and_before_keeps_newer_lines(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    _message(archive, "old", "c1", SEPT)
    _message(archive, "new", "c1", OCT)
    _message(archive, "other", "c2", SEPT)
    purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", before_ms=OCT),
        operator="dm",
        now_ms=OCT,
    )
    assert sorted(_ids(root)) == ["new", "other"]


def test_media_is_removed_only_when_no_kept_line_references_it(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "m1", "c1", OCT, media=meta)
    _message(archive, "m2", "c1", OCT, media=meta)
    purge(root, PurgeSelector(channel="whatsapp", native_id="m1"), operator="dm", now_ms=OCT)
    assert (root / meta["path"]).exists()
    result = purge(
        root, PurgeSelector(channel="whatsapp", native_id="m2"), operator="dm", now_ms=OCT + 1
    )
    assert result.media_removed == (meta["path"],)
    assert not (root / meta["path"]).exists()


def test_purge_does_not_follow_media_paths_outside_archive(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    _message(
        archive,
        "m1",
        "c1",
        OCT,
        media={"stored": True, "path": "../outside.bin"},
    )
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="m1"), operator="dm")
    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert outside.read_bytes() == b"keep"


def test_purge_audits_only_the_locked_snapshot_and_preserves_late_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import yeoman_shared.raw_archive.purge as purge_module
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    _message(archive, "before", "c1", OCT)
    append_started = threading.Event()
    append_finished = threading.Event()
    real_append_line = writer_module.append_line
    real_append_protected = purge_module.append_protected
    late_writer: threading.Thread | None = None

    def tracked_append_line(path: Path, line: str, **kwargs: object) -> None:
        if threading.current_thread().name == "late-archive-writer":
            append_started.set()
        real_append_line(path, line, **kwargs)

    def append_late_message() -> None:
        try:
            _message(archive, "late", "c1", OCT)
        finally:
            append_finished.set()

    def audited_append(path: Path, line: str) -> None:
        nonlocal late_writer
        real_append_protected(path, line)
        if path == root / AUDIT:
            late_writer = threading.Thread(
                name="late-archive-writer", target=append_late_message, daemon=True
            )
            late_writer.start()
            assert append_started.wait(timeout=2)
            late_writer.join(timeout=1)

    monkeypatch.setattr(writer_module, "append_line", tracked_append_line)
    monkeypatch.setattr(purge_module, "append_protected", audited_append)
    result = purge(root, PurgeSelector(channel="whatsapp", chat_id="c1"), operator="dm")

    assert late_writer is not None
    late_writer.join(timeout=5)
    assert append_finished.is_set()
    assert result.removed_lines == 1
    [audit] = [r for _, r, _ in iter_records(root / AUDIT) if r]
    assert audit["removed_lines"] == result.removed_lines
    assert audit["removed_sha256"] == list(result.removed_sha256)
    assert _ids(root) == ["late"]


def test_purge_includes_matches_arriving_before_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.purge as purge_module

    root, archive = _setup(tmp_path)
    _message(archive, "other-chat", "c2", OCT)
    real_lock_file = purge_module._lock_file
    appended = False

    def append_before_lock(path: Path) -> int:
        nonlocal appended
        if not appended:
            appended = True
            _message(archive, "arrived-before-lock", "c1", OCT)
        return real_lock_file(path)

    monkeypatch.setattr(purge_module, "_lock_file", append_before_lock)
    result = purge(root, PurgeSelector(channel="whatsapp", chat_id="c1"), operator="dm")

    assert appended
    assert result.removed_lines == 1
    [audit] = [r for _, r, _ in iter_records(root / AUDIT) if r]
    assert audit["removed_lines"] == 1
    assert audit["removed_sha256"] == list(result.removed_sha256)
    assert _ids(root) == ["other-chat"]


def test_purge_keeps_media_referenced_from_another_channel_and_month(
    tmp_path: Path,
) -> None:
    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    archive.append(
        RawEvent(
            channel="telegram",
            kind="media",
            direction="in",
            native={"file_id": "kept"},
            native_id="kept",
            chat_id="elsewhere",
            received_ms=SEPT,
            media=meta,
        )
    )

    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.media_removed == ()
    assert (root / meta["path"]).exists()


def test_purge_keeps_media_referenced_by_raw_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    real_append_line = writer_module.append_line

    def fail_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_archive_append)
    assert (
        archive.append(
            RawEvent(
                channel="telegram",
                kind="media",
                direction="in",
                native={"file_id": "spooled"},
                native_id="spooled",
                chat_id="elsewhere",
                received_ms=SEPT,
                media=meta,
            )
        )
        is False
    )
    assert list(archive.spool.glob("*.json"))

    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.media_removed == ()
    assert (root / meta["path"]).exists()


def test_purge_keeps_media_while_referencing_event_is_pending_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)

    def unavailable(path: Path, line: str, **kwargs: object) -> None:
        raise OSError("injected storage failure")

    monkeypatch.setattr(writer_module, "append_line", unavailable)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    assert (
        archive.append(
            RawEvent(
                channel="telegram",
                kind="media",
                direction="in",
                native={"file_id": "pending"},
                native_id="pending",
                chat_id="elsewhere",
                received_ms=SEPT,
                media=meta,
            )
        )
        is False
    )
    assert archive.status().pending_in_memory == 1

    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert _ids(root) == []
    assert (root / meta["path"]).exists()

    monkeypatch.undo()
    assert archive.drain_spool() == 1
    result = purge(root, PurgeSelector(channel="telegram", native_id="pending"), operator="dm")
    assert result.media_removed == (meta["path"],)
    assert not (root / meta["path"]).exists()


def test_atomic_media_append_keeps_media_referenced_by_spool_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    selected_media = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=selected_media)

    real_append_line = writer_module.append_line

    def fail_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_archive_append)
    assert (
        archive.append_with_media(
            RawEvent(
                channel="whatsapp",
                kind="media",
                direction="in",
                native={"file_id": "spooled"},
                native_id="spooled",
                chat_id="elsewhere",
                received_ms=OCT,
            ),
            src,
            kind="image",
        )
        is False
    )
    [spool_path] = archive.spool.glob("*.json")

    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert (root / selected_media["path"]).exists()
    assert selected_media["path"] in spool_path.read_text(encoding="utf-8")

    monkeypatch.undo()
    assert archive.drain_spool() == 1
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="spooled"), operator="dm")
    assert result.media_removed == (selected_media["path"],)
    assert not (root / selected_media["path"]).exists()


def _append_atomic_media_in_child(
    root: str,
    spool: str,
    status_path: str,
    source: str,
) -> None:
    child = RawArchive(
        Path(root), spool=Path(spool), status_path=Path(status_path), clock=lambda: OCT
    )
    child.append_with_media(
        RawEvent(
            channel="whatsapp",
            kind="media",
            direction="in",
            native={"file_id": "late"},
            native_id="late",
            chat_id="c2",
            received_ms=OCT,
        ),
        Path(source),
        kind="image",
    )


def test_purge_retains_media_during_cross_process_store_to_append_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multiprocessing

    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    context = multiprocessing.get_context("fork")
    append_reached = context.Event()
    allow_append = context.Event()
    real_append_line = writer_module.append_line

    def pause_before_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if multiprocessing.current_process().name == "raw-media-writer" and path.suffix == ".jsonl":
            append_reached.set()
            if not allow_append.wait(timeout=10):
                raise TimeoutError("test did not release the media writer")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", pause_before_archive_append)
    child = context.Process(
        name="raw-media-writer",
        target=_append_atomic_media_in_child,
        args=(
            str(root),
            str(archive.spool),
            str(archive.status_path),
            str(src),
        ),
    )
    child.start()
    try:
        assert append_reached.wait(timeout=5), "child did not reach the store/append boundary"
        result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")
        assert result.media_removed == ()
        assert (root / meta["path"]).exists()
    finally:
        allow_append.set()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    assert child.exitcode == 0
    records = [
        record
        for archive_path in archive_files(root)
        for _, record, _ in iter_records(archive_path)
        if record is not None
    ]
    assert [record["native_id"] for record in records] == ["late"]
    assert (root / meta["path"]).exists()


def test_append_of_stale_media_metadata_is_marked_unstored(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    archive.append(
        RawEvent(
            channel="telegram",
            kind="media",
            direction="in",
            native={"file_id": "stale"},
            native_id="stale",
            chat_id="elsewhere",
            received_ms=SEPT,
            media=meta,
        )
    )

    [record] = [
        record
        for path in archive_files(root)
        for _, record, _ in iter_records(path)
        if record and record["native_id"] == "stale"
    ]
    assert record["native"] == {"file_id": "stale"}
    assert record["media"]["stored"] is False
    assert record["media"]["path"] is None
    assert record["media"]["reason"] == "purged_before_append"


def test_purge_retains_media_when_reference_directory_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.purge as purge_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    archive.append(
        RawEvent(
            channel="telegram",
            kind="media",
            direction="in",
            native={"file_id": "kept"},
            native_id="kept",
            chat_id="elsewhere",
            received_ms=SEPT,
            media=meta,
        )
    )
    real_scandir = purge_module.os.scandir

    def deny_reference_directory(path):
        if Path(path) == root / "telegram":
            raise PermissionError("injected unreadable reference directory")
        return real_scandir(path)

    monkeypatch.setattr(purge_module.os, "scandir", deny_reference_directory)
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert (root / meta["path"]).exists()


def _purge_custom_spool_in_child(root: str, output: object) -> None:
    result = purge(
        Path(root), PurgeSelector(channel="whatsapp", native_id="selected"), operator="child"
    )
    output.put((result.removed_lines, result.media_removed))


def test_purge_discovers_custom_spool_from_persistent_registry_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import multiprocessing

    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    custom_spool = tmp_path / "custom" / "retry-queue"
    custom = RawArchive(
        root,
        spool=custom_spool,
        status_path=tmp_path / "run" / "custom-status.json",
        clock=lambda: OCT,
    )
    real_append_line = writer_module.append_line

    def fail_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_archive_append)
    assert (
        custom.append(
            RawEvent(
                channel="whatsapp",
                kind="media",
                direction="in",
                native={"file_id": "custom-spooled"},
                native_id="custom-spooled",
                chat_id="other",
                received_ms=OCT,
                media=meta,
            )
        )
        is False
    )
    [spool_line] = custom_spool.glob("*.json")
    assert meta["path"] in spool_line.read_text(encoding="utf-8")

    monkeypatch.undo()
    context = multiprocessing.get_context("fork")
    output = context.Queue()
    child = context.Process(target=_purge_custom_spool_in_child, args=(str(root), output))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5)
        pytest.fail("custom-spool purge child did not finish")
    assert child.exitcode == 0
    assert output.get(timeout=2) == (1, ())
    assert (root / meta["path"]).exists()


def test_purge_retains_media_when_spool_registry_is_malformed(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    registry = root / ".raw-spools.jsonl"
    registry.chmod(0o600)
    registry.write_text("{broken registry entry\n", encoding="utf-8")

    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert not _ids(root)
    assert (root / meta["path"]).exists()


def test_purge_releases_media_guard_when_file_lock_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.purge as purge_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    monkeypatch.setattr(
        purge_module,
        "_lock_file",
        lambda path: (_ for _ in ()).throw(PermissionError("injected archive lock error")),
    )

    with pytest.raises(PermissionError, match="injected archive lock error"):
        purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    monkeypatch.undo()
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")
    assert result.media_removed == (meta["path"],)
    assert not (root / meta["path"]).exists()


def test_purge_retains_media_when_spool_registry_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    registry = root / ".raw-spools.jsonl"
    real_read_text = Path.read_text

    def fail_registry(path: Path, *args: object, **kwargs: object) -> str:
        if path == registry:
            raise PermissionError("injected spool registry read error")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_registry)
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert (root / meta["path"]).exists()


def test_spool_registry_failure_keeps_nonmedia_spooling_and_media_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"img")
    meta = archive.store_media("whatsapp", src, kind="image")
    _message(archive, "selected", "c1", OCT, media=meta)
    custom_spool = tmp_path / "custom" / "retry-queue"

    def fail_registry(path: Path, line: str) -> None:
        raise OSError("injected registry write failure")

    monkeypatch.setattr(writer_module, "append_protected", fail_registry)
    custom = RawArchive(
        root,
        spool=custom_spool,
        status_path=tmp_path / "run" / "custom-status.json",
        clock=lambda: OCT,
    )
    real_append_line = writer_module.append_line

    def fail_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_archive_append)
    assert (
        custom.append(
            RawEvent(
                channel="whatsapp",
                kind="message",
                direction="in",
                native={"text": "nonmedia"},
                native_id="plain",
                chat_id="other",
                received_ms=OCT,
            )
        )
        is False
    )
    assert len(list(custom_spool.glob("*.json"))) == 1

    assert (
        custom.append(
            RawEvent(
                channel="whatsapp",
                kind="media",
                direction="in",
                native={"file_id": "not-durable"},
                native_id="memory-only",
                chat_id="other",
                received_ms=OCT,
                media=meta,
            )
        )
        is False
    )
    assert custom.status().pending_in_memory == 1
    spool_records = [json.loads(path.read_text()) for path in custom_spool.glob("*.json")]
    assert all(
        json.loads(record["line"]).get("native_id") != "memory-only" for record in spool_records
    )


def test_purge_scans_legacy_default_spool_when_registry_has_only_custom_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, legacy = _setup(tmp_path)
    src = tmp_path / "legacy.jpg"
    src.write_bytes(b"img")
    meta = legacy.store_media("whatsapp", src, kind="image")
    _message(legacy, "selected", "c1", OCT, media=meta)

    real_append_line = writer_module.append_line

    def fail_archive_append(path: Path, line: str, **kwargs: object) -> None:
        if path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_archive_append)
    assert (
        legacy.append(
            RawEvent(
                channel="whatsapp",
                kind="media",
                direction="in",
                native={"file_id": "legacy-pending"},
                native_id="legacy-pending",
                chat_id="c2",
                received_ms=OCT,
                media=meta,
            )
        )
        is False
    )
    [spool_file] = legacy.spool.glob("*.json")
    assert meta["path"] in spool_file.read_text(encoding="utf-8")

    # Model the persisted pre-registry state before a new writer uses a custom spool.
    registry = root / ".raw-spools.jsonl"
    registry.unlink()
    custom_spool = tmp_path / "configured-spool"
    custom = RawArchive(
        root,
        spool=custom_spool,
        status_path=tmp_path / "run" / "custom-status.json",
        clock=lambda: OCT,
    )
    [registration] = registry.read_text(encoding="utf-8").splitlines()
    assert json.loads(registration)["path"] == str(custom.spool)

    monkeypatch.undo()
    result = purge(root, PurgeSelector(channel="whatsapp", native_id="selected"), operator="dm")

    assert result.removed_lines == 1
    assert result.media_removed == ()
    assert (root / meta["path"]).exists()
    assert legacy.drain_spool() == 1
    [kept] = [
        record
        for path in archive_files(root)
        for _, record, _ in iter_records(path)
        if record and record["native_id"] == "legacy-pending"
    ]
    assert kept["media"]["stored"] is True
    assert (root / kept["media"]["path"]).is_file()
