import json

import pytest
from yeoman_gateway.history.layer1 import (
    Origin,
    backfill_line,
    iter_layer1,
    layer1_files,
    row_sha256,
    write_jsonl_once,
)
from yeoman_shared.raw_archive.paths import ProtectedPathError


def _origin():
    return Origin("memory", "data/memory/memory.db", "memory2_nodes", "12019")


def test_backfill_line_keeps_original_and_hash():
    original = {"id": "12019", "content": "hallo"}
    line = backfill_line(
        channel="whatsapp", kind="message", provenance="verbatim_unverified",
        time_certainty="capture_time_approx", occurred_ms=1, direction="in",
        chat_id="1-2@g.us", payload={"text": "hallo"}, origin=_origin(), original=original,
    )
    assert line["original"] == original
    assert line["origin"]["row_sha256"] == row_sha256(original)
    assert line["skip_reason"] is None and line["backfill_version"] == 1


@pytest.mark.parametrize("field", ["provenance", "time_certainty"])
def test_backfill_line_rejects_unknown_vocabulary(field):
    kwargs = dict(
        channel="whatsapp", kind="message", provenance="native", time_certainty="native",
        occurred_ms=None, direction=None, chat_id=None, payload={}, origin=_origin(), original={},
    )
    kwargs[field] = "guess"
    with pytest.raises(ValueError):
        backfill_line(**kwargs)


def test_write_once_refuses_existing_and_keeps_bytes(tmp_path):
    path = tmp_path / "backfill" / "memory.jsonl"
    assert write_jsonl_once(path, [{"a": 1}, {"b": 2}]) == 2
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_jsonl_once(path, [{"c": 3}])
    assert path.read_bytes() == before
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "backfill" / "memory.jsonl.partial").exists()


def test_write_once_refuses_protected_target_before_mutating(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "isolated-home"))
    path = tmp_path / "isolated-home" / "data" / "raw" / "whatsapp" / "events.jsonl"
    with pytest.raises(ProtectedPathError):
        write_jsonl_once(path, [{"kind": "message"}])
    assert not path.parent.exists()
    assert not path.with_name(path.name + ".partial").exists()


def test_iter_layer1_refs_and_invalid_last_line(tmp_path):
    raw = tmp_path / "raw"
    (raw / "whatsapp").mkdir(parents=True)
    (raw / "whatsapp" / "2026-10.jsonl").write_text(
        json.dumps({"kind": "message"}) + "\n\n" + '{"kind": "mess', encoding="utf-8"
    )
    lines = list(iter_layer1([raw]))
    assert [x.ref for x in lines] == ["whatsapp/2026-10.jsonl#1", "whatsapp/2026-10.jsonl#3"]
    assert lines[0].record == {"kind": "message"}
    assert lines[1].record is None


def test_layer1_files_across_roots_sorted_and_unique(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    (live / "whatsapp").mkdir(parents=True)
    (live / "whatsapp" / "2026-10.jsonl").write_text("", encoding="utf-8")
    (live / "media").mkdir()
    (dev / "backfill").mkdir(parents=True)
    (dev / "backfill" / "journal.jsonl").write_text("", encoding="utf-8")
    assert [rel for rel, _ in layer1_files([live, dev])] == [
        "backfill/journal.jsonl", "whatsapp/2026-10.jsonl",
    ]
    (dev / "whatsapp").mkdir()
    (dev / "whatsapp" / "2026-10.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        layer1_files([live, dev])
