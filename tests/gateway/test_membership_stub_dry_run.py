from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import extract_membership_stubs as reader


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    cold = tmp_path / "cold" / "data" / "bridge" / "whatsapp-message-references"
    rolling = tmp_path / "bridge-references" / "files"
    cold.mkdir(parents=True)
    rolling.mkdir(parents=True)
    return cold, rolling


def _write(root: Path, name: str, encoded: str, *, message_id: str = "native-1") -> Path:
    path = root / name
    path.write_text(json.dumps({"encoded": encoded, "chatJid": "group@g.us", "messageId": message_id, "text": "PRIVATE MESSAGE BODY"}))
    return path


def _decoder(encoded: str, _package: Path) -> tuple[dict | None, str | None]:
    if encoded == "bad-decode":
        return None, "offline_decoder_failed"
    if encoded == "nonascii":
        raise UnicodeEncodeError("ascii", encoded, 0, 1, "invalid input")
    stub_type = encoded if encoded.startswith("GROUP_PARTICIPANT_") else ("UNKNOWN" if encoded == "ordinary" else "GROUP_PARTICIPANT_ADD")
    return {
        "key": {"remoteJid": "group@g.us", "id": "native-1", "participant": "actor@s.whatsapp.net"},
        "messageTimestamp": "1725000000",
        "messageStubType": stub_type,
        "messageStubParameters": ["member@s.whatsapp.net"],
        "message": {"conversation": "PRIVATE MESSAGE BODY"},
    }, None


def test_extracts_only_membership_stubs_without_message_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    _write(cold, "one.json", "member")
    _write(cold, "ordinary.json", "ordinary")
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    report = reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert report["counts"]["membership_stubs"] == 1
    assert report["counts"]["non_membership"] == 1
    row = report["stubs"][0]
    assert (row["chat_jid"], row["native_message_id"], row["stub_type"], row["action"]) == (
        "group@g.us", "native-1", "GROUP_PARTICIPANT_ADD", "add"
    )
    assert row["actor"] == "actor@s.whatsapp.net"
    assert row["participants"] == ["member@s.whatsapp.net"]
    assert row["provider_timestamp"] == "1725000000"
    assert set(row) == {
        "source_locator", "source_sha256", "payload_sha256", "chat_jid", "native_message_id",
        "provider_timestamp", "stub_type", "action", "actor", "participants",
        "duplicate_locators", "conflicting_payloads",
    }
    assert "PRIVATE MESSAGE BODY" not in json.dumps(report)


def test_duplicate_reference_sources_keep_both_locators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    _write(cold, "a.json", "same")
    _write(rolling, "b.json", "same")
    _write(rolling, "c.json", "different")
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    report = reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert report["counts"]["membership_stubs"] == 3
    assert report["counts"]["duplicate_groups"] == 1
    assert report["counts"]["conflict_groups"] == 1
    assert len({row["source_locator"] for row in report["stubs"]}) == 3
    assert all(len(row["duplicate_locators"]) == 3 for row in report["stubs"])
    assert all(row["conflicting_payloads"] is True for row in report["stubs"])


def test_corrupt_reference_is_reported_without_stopping_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    (cold / "broken.json").write_text("{")
    _write(cold, "decode.json", "bad-decode")
    _write(cold, "nonascii.json", "nonascii")
    _write(cold, "surrogate.json", "\ud800")
    _write(rolling, "good.json", "member")
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    report = reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert report["counts"]["malformed"] == 2
    assert report["counts"]["decode_failures"] == 2
    assert report["counts"]["membership_stubs"] == 1
    assert "PRIVATE MESSAGE BODY" not in json.dumps(report)


def test_all_membership_stub_actions_and_direct_json_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    expected = {
        "GROUP_PARTICIPANT_ADD": "add",
        "GROUP_PARTICIPANT_REMOVE": "remove",
        "GROUP_PARTICIPANT_LEAVE": "remove",
        "GROUP_PARTICIPANT_INVITE": "add",
        "GROUP_PARTICIPANT_PROMOTE": "promote",
        "GROUP_PARTICIPANT_DEMOTE": "demote",
    }
    for index, stub_type in enumerate(expected):
        _write(cold, f"stub-{index}.json", stub_type)
    _write(cold, "ignored.txt", "GROUP_PARTICIPANT_ADD")
    nested = cold / "nested"
    nested.mkdir()
    _write(nested, "ignored.json", "GROUP_PARTICIPANT_ADD")
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    report = reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert report["counts"]["source_files"] == 6
    assert report["counts"]["membership_stubs"] == 6
    assert {row["stub_type"]: row["action"] for row in report["stubs"]} == expected


def test_unreadable_reference_is_counted_and_scan_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    unreadable = _write(cold, "a-unreadable.json", "member")
    _write(rolling, "z-good.json", "member")
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == unreadable:
            raise PermissionError("synthetic unreadable file")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    report = reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert report["counts"]["source_files"] == 2
    assert report["counts"]["unreadable"] == 1
    assert report["counts"]["membership_stubs"] == 1
    assert report["stubs"][0]["source_locator"] == "rolling/z-good.json"


def test_reader_does_not_modify_source_trees_or_accept_raw_archive_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cold, rolling = _roots(tmp_path)
    first = _write(cold, "one.json", "member")
    second = _write(rolling, "two.json", "member")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (first, second)}
    monkeypatch.setattr(reader, "_decode_bridge", _decoder)

    reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")

    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (first, second)} == before
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    _write(raw, "forbidden.json", "member")
    with pytest.raises(ValueError, match="bridge reference"):
        reader.extract_membership_stubs(raw, rolling, tmp_path / "bridge")
    traversing = tmp_path / "cold" / ".." / "cold" / "data" / "bridge" / "whatsapp-message-references"
    with pytest.raises(ValueError, match="traversal"):
        reader.extract_membership_stubs(traversing, rolling, tmp_path / "bridge")
    (cold / "linked.json").symlink_to(raw / "forbidden.json")
    with pytest.raises(ValueError, match="symlink"):
        reader.extract_membership_stubs(cold, rolling, tmp_path / "bridge")
