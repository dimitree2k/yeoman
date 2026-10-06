"""S30: owner purge removes only the selected lines and leaves an audit record."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from yeoman_shared.raw_archive.purge import PurgeSelector, plan_purge, purge
from yeoman_shared.raw_archive.records import archive_files, iter_records, line_sha256
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


def _records(root: Path) -> list[dict]:
    return [record for path in archive_files(root) for _, record, _ in iter_records(path) if record]


def _media_description(message_id: str = "m1", *, generated_ms: int = OCT) -> dict:
    return {
        "derived_version": 1,
        "kind": "media_description",
        "channel": "whatsapp",
        "chat_id": "c1",
        "native_message_id": message_id,
        "mode": "description",
        "generator": "model",
        "generated_ms": generated_ms,
        "text": f"description-{message_id}",
    }


def _pending_message(
    message_id: str, *, received_ms: int = SEPT, correlation_id: str = ""
) -> RawEvent:
    return RawEvent(
        channel="whatsapp",
        kind="message",
        direction="in",
        native={"payload": {"messageId": message_id, "text": f"body-{message_id}"}},
        native_id=f"event-{message_id}",
        chat_id="c1",
        correlation_id=correlation_id,
        received_ms=received_ms,
    )


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


def test_raw_purge_includes_matching_media_description(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    record = _media_description()
    archive.append_media_description(record)
    target = root / "derived" / "media-descriptions.jsonl"
    line = target.read_text(encoding="utf-8").rstrip("\n")
    planned = plan_purge(root, PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"))
    assert planned.removed_lines == 1
    assert planned.files == ("derived/media-descriptions.jsonl",)
    result = purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"),
        operator="dm",
        now_ms=OCT + 1,
    )
    assert result.removed_lines == 1
    assert result.removed_sha256 == (line_sha256(line),)
    assert target.read_text(encoding="utf-8") == ""
    [audit] = [record for _, record, _ in iter_records(root / AUDIT) if record]
    assert audit["files"] == ["derived/media-descriptions.jsonl"]
    assert audit["removed_sha256"] == [line_sha256(line)]


@pytest.mark.parametrize("spooled", [False, True])
def test_disposition_suppresses_direct_and_spooled_media_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spooled: bool
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    if spooled:
        real_append = writer_module.append_line

        def fail_derived(path: Path, line: str, **kwargs: object) -> bool:
            if path.name == "media-descriptions.jsonl":
                raise OSError("injected derived failure")
            return real_append(path, line, **kwargs)

        monkeypatch.setattr(writer_module, "append_line", fail_derived)
        assert archive.append_media_description(_media_description()) is False

    purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"),
        operator="dm",
        now_ms=OCT + 1,
    )
    if spooled:
        monkeypatch.setattr(writer_module, "append_line", real_append)
        assert archive.drain_spool() == 1
    else:
        assert archive.append_media_description(_media_description()) is True
    target = root / "derived" / "media-descriptions.jsonl"
    assert not target.exists() or target.read_text(encoding="utf-8") == ""


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


def test_message_purge_does_not_apply_an_implicit_chat_cutoff(tmp_path: Path) -> None:
    root, archive = _setup(tmp_path)
    _message(archive, "future-stamped", "c1", OCT + 1)

    result = purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="future-stamped"),
        operator="dm",
        now_ms=OCT,
    )

    assert result.removed_lines == 1
    [audit] = [record for _, record, _ in iter_records(root / AUDIT) if record]
    assert audit["selector"]["before_ms"] is None


def test_telegram_update_id_is_not_a_provider_message_identity(tmp_path: Path) -> None:
    from telegram import Update
    from yeoman_gateway.channels.telegram import raw_event_for_update

    root, archive = _setup(tmp_path)

    def event(update_id: int, message_id: int) -> RawEvent:
        update = Update.de_json(
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "date": OCT // 1000,
                    "chat": {"id": 101, "type": "supergroup"},
                    "text": "synthetic",
                },
            },
            bot=None,
        )
        return raw_event_for_update(update)

    archive.append(event(1001, 7))
    archive.append(event(1002, 1001))

    selector = PurgeSelector(channel="telegram", chat_id="101", native_id="7")
    assert plan_purge(root, selector).removed_lines == 1
    result = purge(root, selector, operator="dm", now_ms=OCT + 1)
    assert result.removed_lines == 1

    remaining = _records(root)
    assert [
        (record["native_id"], record["native"]["message"]["message_id"]) for record in remaining
    ] == [("1002", 1001)]
    assert archive.append(event(1003, 1001)) is True


def test_zero_match_disposition_closes_telegram_media_provider_identity(
    tmp_path: Path,
) -> None:
    from telegram import Update
    from yeoman_gateway.channels.telegram import raw_event_for_update

    root, archive = _setup(tmp_path)
    result = purge(
        root,
        PurgeSelector(channel="telegram", chat_id="101", native_id="7"),
        operator="dm",
        now_ms=OCT + 1,
    )
    assert result.removed_lines == 0
    [audit] = [record for _, record, _ in iter_records(root / AUDIT) if record]
    assert audit["disposition"]["message_identities"] == ["7"]
    assert audit["disposition"]["correlation_ids"] == []

    def event(update_id: int, message_id: int, chat_id: int = 101) -> RawEvent:
        update = Update.de_json(
            {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "date": OCT // 1000,
                    "chat": {"id": chat_id, "type": "supergroup"},
                    "text": "synthetic",
                },
            },
            bot=None,
        )
        return raw_event_for_update(update)

    selected = event(1001, 7)
    archive.append(selected)
    archive.append(
        RawEvent(
            channel="telegram",
            kind="media",
            direction="in",
            native={"update_id": 1001, "file_id": "selected-media"},
            native_id="1001",
            chat_id="101",
            correlation_id="7",
            received_ms=OCT,
        )
    )
    archive.append(event(2002, 1001))
    archive.append(event(2003, 7, chat_id=202))
    archive.append(
        RawEvent(
            channel="whatsapp",
            kind="message",
            direction="in",
            native={"payload": {"messageId": "7"}},
            native_id="whatsapp-event-7",
            chat_id="101",
            received_ms=OCT,
        )
    )

    assert [
        (record["channel"], record["kind"], record["chat_id"], record["native_id"])
        for record in _records(root)
    ] == [
        ("telegram", "update", "101", "2002"),
        ("telegram", "update", "202", "2003"),
        ("whatsapp", "message", "101", "whatsapp-event-7"),
    ]


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


def test_purged_append_before_spool_unlink_is_drained_without_resurrection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    event = _pending_message("m1")
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> bool:
        if path.parent == root / "whatsapp" and path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        return real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_month)
    assert archive.append(event) is False
    [spool_line] = archive.spool.glob("*.json")
    monkeypatch.setattr(writer_module, "append_line", real_append_line)

    real_unlink = Path.unlink
    failed = False

    def fail_spool_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal failed
        if path == spool_line and not failed:
            failed = True
            raise OSError("injected post-append spool unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_spool_unlink)
    assert archive.drain_spool() == 0
    assert len(_ids(root)) == 1
    result = purge(
        root, PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"), operator="dm"
    )
    assert result.removed_lines == 1

    monkeypatch.undo()
    assert archive.drain_spool() == 1
    assert _ids(root) == []
    assert list(archive.spool.glob("*.json")) == []


@pytest.mark.parametrize("pending_kind", ["default-spool", "custom-spool", "memory"])
def test_zero_match_disposition_drains_changed_pending_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending_kind: str
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root = tmp_path / "raw"
    spool = tmp_path / ("custom" if pending_kind == "custom-spool" else "raw-spool")
    archive = RawArchive(
        root,
        spool=spool,
        status_path=tmp_path / f"{pending_kind}.json",
        clock=lambda: OCT,
    )
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> bool:
        if path.parent == root / "whatsapp" and path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        return real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_month)
    if pending_kind == "memory":
        monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    assert archive.append(_pending_message("m1")) is False

    if pending_kind == "memory":
        channel, received_ms, line, destination, lease = archive._pending[0]
        record = json.loads(line)
        record["native"]["payload"]["attempt"] = "changed-frame"
        archive._pending[0] = (channel, received_ms, json.dumps(record), destination, lease)
    else:
        [spool_line] = archive.spool.glob("*.json")
        envelope = json.loads(spool_line.read_text(encoding="utf-8"))
        record = json.loads(envelope["line"])
        record["native"]["payload"]["attempt"] = "changed-frame"
        envelope["line"] = json.dumps(record)
        spool_line.write_text(json.dumps(envelope), encoding="utf-8")

    result = purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="m1"),
        operator="dm",
        now_ms=OCT,
    )
    assert result.removed_lines == 0
    assert result.disposition_recorded is True
    [audit] = [record for _, record, _ in iter_records(root / AUDIT) if record]
    assert audit["disposition"]["message_identities"] == ["m1"]

    monkeypatch.undo()
    assert archive.drain_spool() == 1
    assert _records(root) == []
    assert not list(archive.spool.glob("*.json"))
    assert archive.status().pending_in_memory == 0


def test_message_purge_disposition_covers_correlated_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    archive.append(
        RawEvent(
            channel="whatsapp",
            kind="outbound_result",
            direction="out",
            native={"message_id": "sent-1"},
            native_id="sent-1",
            chat_id="c1",
            correlation_id="request-1",
            received_ms=OCT,
        )
    )
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> bool:
        if path.parent == root / "whatsapp" and path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        return real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_month)
    assert (
        archive.append(
            RawEvent(
                channel="whatsapp",
                kind="outbound_request",
                direction="out",
                native={"text": "secret"},
                chat_id="c1",
                correlation_id="request-1",
                received_ms=OCT,
            )
        )
        is False
    )
    purge(
        root,
        PurgeSelector(channel="whatsapp", chat_id="c1", native_id="sent-1"),
        operator="dm",
        now_ms=OCT + 1,
    )
    monkeypatch.undo()
    assert archive.drain_spool() == 1
    assert _records(root) == []
    assert not list(archive.spool.glob("*.json"))


@pytest.mark.parametrize("channel", ["whatsapp", "telegram"])
def test_purge_message_expands_scoped_outbound_correlation_closure(
    tmp_path: Path, channel: str
) -> None:
    root, archive = _setup(tmp_path)

    def request(chat: str, correlation_id: str, content: str) -> None:
        archive.append(
            RawEvent(
                channel=channel,
                kind="outbound_request",
                direction="out",
                native={"content": content},
                chat_id=chat,
                correlation_id=correlation_id,
                received_ms=OCT,
            )
        )

    def result(chat: str, correlation_id: str, message_id: str) -> None:
        archive.append(
            RawEvent(
                channel=channel,
                kind="outbound_result",
                direction="out",
                native={"message_id": message_id},
                native_id=message_id,
                chat_id=chat,
                correlation_id=correlation_id,
                received_ms=OCT,
            )
        )

    request("c1", "req-selected", "selected secret")
    result("c1", "req-selected", "7")
    request("c1", "req-neighbor", "keep this")
    result("c1", "req-neighbor", "8")
    request("c2", "req-other-chat", "other chat")
    result("c2", "req-other-chat", "7")

    selector = PurgeSelector(channel=channel, chat_id="c1", native_id="7")
    assert plan_purge(root, selector).removed_lines == 2
    result = purge(root, selector, operator="dm", now_ms=OCT + 1)

    assert result.removed_lines == 2
    remaining = _records(root)
    assert {(row["chat_id"], row["kind"], row["native_id"]) for row in remaining} == {
        ("c1", "outbound_request", ""),
        ("c1", "outbound_result", "8"),
        ("c2", "outbound_request", ""),
        ("c2", "outbound_result", "7"),
    }
    [audit] = [record for _, record, _ in iter_records(root / AUDIT) if record]
    assert audit["removed_lines"] == 2
    assert len(audit["removed_sha256"]) == 2


def test_chat_disposition_expires_at_snapshot_and_keeps_later_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_shared.raw_archive.writer as writer_module

    root, archive = _setup(tmp_path)
    real_append_line = writer_module.append_line

    def fail_month(path: Path, line: str, **kwargs: object) -> bool:
        if path.parent == root / "whatsapp" and path.suffix == ".jsonl":
            raise OSError("injected archive failure")
        return real_append_line(path, line, **kwargs)

    monkeypatch.setattr(writer_module, "append_line", fail_month)
    assert archive.append(_pending_message("old")) is False
    result = purge(root, PurgeSelector(channel="whatsapp", chat_id="c1"), operator="dm", now_ms=OCT)
    assert result.removed_lines == 0 and result.disposition_recorded is True
    monkeypatch.undo()

    assert archive.append(_pending_message("new", received_ms=OCT + 1)) is True
    assert _ids(root) == ["new"]


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
    month_file = root / "whatsapp" / "2026-10.jsonl"
    old_inode = month_file.stat().st_ino
    append_started = threading.Event()
    append_finished = threading.Event()
    real_append_line = writer_module.append_line
    real_append_is_disposed = writer_module.append_is_disposed
    real_append_protected = purge_module.append_protected
    late_writer: threading.Thread | None = None

    def tracked_append_line(path: Path, line: str, **kwargs: object) -> None:
        if threading.current_thread().name == "late-archive-writer":
            append_started.set()
        real_append_line(path, line, **kwargs)

    def checked_disposition(audit_path: Path, record: dict, line: str) -> bool:
        if threading.current_thread().name == "late-archive-writer":
            assert audit_path.is_file()
            assert month_file.stat().st_ino != old_inode
        return real_append_is_disposed(audit_path, record, line)

    def append_late_message() -> None:
        try:
            _message(archive, "late", "c1", OCT + 1)
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
    monkeypatch.setattr(writer_module, "append_is_disposed", checked_disposition)
    monkeypatch.setattr(purge_module, "append_protected", audited_append)
    result = purge(
        root, PurgeSelector(channel="whatsapp", chat_id="c1"), operator="dm", now_ms=OCT + 1
    )

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
    real_lock_file = purge_module.lock_file
    appended = False

    def append_before_lock(path: Path, *, create: bool = False) -> int:
        nonlocal appended
        if path == root / ".purge-disposition.lock" and not appended:
            appended = True
            _message(archive, "arrived-before-lock", "c1", OCT)
        return real_lock_file(path, create=create)

    monkeypatch.setattr(purge_module, "lock_file", append_before_lock)
    result = purge(
        root, PurgeSelector(channel="whatsapp", chat_id="c1"), operator="dm", now_ms=OCT + 1
    )

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
                native={"file_id": "pending", "source_message_id": "pending"},
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
