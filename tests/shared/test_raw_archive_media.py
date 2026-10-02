"""D10: raw media originals are independent copies, content-addressed, read-only."""

from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path

from yeoman_shared.raw_archive import writer as writer_module
from yeoman_shared.raw_archive.writer import RawArchive

NOW = 1_790_000_000_000


def _archive(tmp_path: Path, **kwargs: object) -> RawArchive:
    return RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "raw-spool",
        status_path=tmp_path / "run" / "raw-archive.json",
        clock=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _source(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "var" / "media" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_image_is_copied_by_hash_and_read_only(tmp_path: Path) -> None:
    src = _source(tmp_path, "a.JPG", b"image-bytes")
    meta = _archive(tmp_path).store_media("whatsapp", src, kind="image")
    assert meta["stored"] is True and meta["reason"] == ""
    assert meta["path"] == f"media/whatsapp/2026-09/{meta['sha256']}.jpg"
    copy = tmp_path / "raw" / meta["path"]
    assert copy.read_bytes() == b"image-bytes"
    assert stat.S_IMODE(copy.stat().st_mode) == 0o444
    assert copy.stat().st_ino != src.stat().st_ino


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    first = archive.store_media("whatsapp", _source(tmp_path, "a.jpg", b"same"), kind="image")
    second = archive.store_media("whatsapp", _source(tmp_path, "b.jpg", b"same"), kind="image")
    assert first["path"] == second["path"]
    assert len(list((tmp_path / "raw" / "media" / "whatsapp" / "2026-09").iterdir())) == 1


def test_changing_the_source_later_does_not_change_the_archive(tmp_path: Path) -> None:
    src = _source(tmp_path, "a.jpg", b"original")
    meta = _archive(tmp_path).store_media("whatsapp", src, kind="image")
    src.write_bytes(b"tampered")
    assert (tmp_path / "raw" / meta["path"]).read_bytes() == b"original"


def test_source_change_at_copy_boundary_matches_archived_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    src = _source(tmp_path, "a.jpg", b"original")
    changed = b"changed-source-is-longer"
    copyfile = writer_module.shutil.copyfile

    def change_source_then_copy(source, destination):
        if Path(source) == src:
            src.write_bytes(changed)
        return copyfile(source, destination)

    monkeypatch.setattr(writer_module.shutil, "copyfile", change_source_then_copy)
    meta = _archive(tmp_path).store_media("whatsapp", src, kind="image")
    archived = (tmp_path / "raw" / meta["path"]).read_bytes()

    assert meta["stored"] is True
    assert archived == changed
    assert meta["bytes"] == len(archived)
    assert meta["sha256"] == hashlib.sha256(archived).hexdigest()


def test_video_growth_at_copy_boundary_still_obeys_size_cap(
    tmp_path: Path, monkeypatch
) -> None:
    src = _source(tmp_path, "v.mp4", b"123456789")
    grown = b"12345678901"
    copyfile = writer_module.shutil.copyfile

    def grow_source_then_copy(source, destination):
        if Path(source) == src:
            src.write_bytes(grown)
        return copyfile(source, destination)

    monkeypatch.setattr(writer_module.shutil, "copyfile", grow_source_then_copy)
    meta = _archive(tmp_path, max_video_bytes=10).store_media("whatsapp", src, kind="video")

    assert meta["stored"] is False and meta["reason"] == "too_large"
    assert meta["bytes"] == len(grown)
    assert meta["sha256"] == hashlib.sha256(grown).hexdigest()
    assert not (tmp_path / "raw" / "media").exists()


def test_large_video_keeps_metadata_only(tmp_path: Path) -> None:
    src = _source(tmp_path, "v.mp4", b"x" * 11)
    meta = _archive(tmp_path, max_video_bytes=10).store_media("whatsapp", src, kind="video")
    assert meta["stored"] is False and meta["reason"] == "too_large"
    assert meta["bytes"] == 11 and len(meta["sha256"]) == 64
    assert not (tmp_path / "raw" / "media").exists()


def test_large_images_are_always_kept(tmp_path: Path) -> None:
    src = _source(tmp_path, "big.png", b"x" * 11)
    meta = _archive(tmp_path, max_video_bytes=10).store_media("whatsapp", src, kind="image")
    assert meta["stored"] is True


def test_missing_source_and_disabled_media_never_raise(tmp_path: Path) -> None:
    missing = _archive(tmp_path).store_media("whatsapp", tmp_path / "nope.jpg", kind="image")
    assert missing == {
        "kind": "image",
        "stored": False,
        "path": None,
        "sha256": None,
        "bytes": None,
        "reason": "missing",
    }
    disabled = _archive(tmp_path, media_enabled=False).store_media(
        "whatsapp", _source(tmp_path, "a.jpg", b"x"), kind="image"
    )
    assert disabled["reason"] == "disabled"


def test_unsafe_suffix_falls_back_to_bin(tmp_path: Path) -> None:
    src = _source(tmp_path, "weird.$$$", b"x")
    meta = _archive(tmp_path).store_media("whatsapp", src, kind="document")
    assert meta["path"].endswith(".bin")


def test_async_media_storage_handles_missing_archive(tmp_path: Path) -> None:
    src = _source(tmp_path, "a.jpg", b"x")
    assert asyncio.run(writer_module.store_media_async(None, "whatsapp", src, kind="image")) is None
    result = asyncio.run(
        writer_module.store_media_async(_archive(tmp_path), "whatsapp", src, kind="image")
    )
    assert result is not None and result["stored"] is True
