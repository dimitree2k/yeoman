"""S30: owner purge removes only the selected lines and leaves an audit record."""

from __future__ import annotations

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
        spool=tmp_path / "spool",
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
