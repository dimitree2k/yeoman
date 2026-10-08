"""Task 2 schema/normalization contracts; incremental engine behavior belongs to Task 3."""
import hashlib
import json
import sqlite3
from contextlib import closing

from hist_fixtures import _raw
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import verify


def test_checkpoint_uses_physical_bytes_lines_and_prefix_hash(tmp_path):
    root = tmp_path / "raw"
    source = root / "whatsapp/2026-10.jsonl"
    source.parent.mkdir(parents=True)
    native = _raw("message", "message", {
        "chatJid": "synthetic@g.us", "messageId": "utf8",
        "senderId": "97001@lid", "text": "Grüße 🐎", "timestamp": 1791000000})
    data = (json.dumps(native, ensure_ascii=False) + '\n\n{malformed\n{"purged_version":1}\n').encode()
    source.write_bytes(data)
    db = tmp_path / "history.db"
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(projector_state)")]
        assert columns == ["file", "lines", "end_offset", "sha256", "projector_version", "state_json"]
        row = conn.execute("SELECT lines, end_offset, sha256, projector_version, state_json "
                           "FROM projector_state WHERE file='whatsapp/2026-10.jsonl'").fetchone()
        assert row[:4] == (4, len(data), hashlib.sha256(data).hexdigest(), 3)
        assert json.loads(row[4]) == {}
        runtime = conn.execute("SELECT lines, end_offset, sha256, projector_version, state_json "
                               "FROM projector_state WHERE file='@runtime'").fetchone()
        assert runtime[:4] == (0, 0, hashlib.sha256(b"").hexdigest(), 3)
        state = json.loads(runtime[4])
        assert state["generation"] == 1 and state["status"] == "ready"
        assert state["pending_pairs"] == {}
        assert state["outcomes"] == report["outcomes"]
        assert state["review"] == {key: len(items) for key, items in report["review"].items()}
        assert "Grüße" not in runtime[4] and "native" not in state
    assert report["projector_state_line_basis"] == "physical"
    assert report["blank_lines_skipped"] == {"whatsapp/2026-10.jsonl": 1}
    assert report["accounting"] == {"whatsapp/2026-10.jsonl": {"lines": 4, "accounted": 4}}
    assert report["outcomes"]["whatsapp/2026-10.jsonl"] == {
        "message": 1, "invalid_json": 1, "skipped:blank": 1, "skipped:purged": 1}
    assert report["accounting_ok"]
    checked = verify([root], db, scratch=None)
    assert checked["accounting"] == report["accounting"] and checked["accounting_ok"]


def test_full_checkpoint_pending_pairs_contains_only_refs(tmp_path):
    from hist_fixtures import write_jsonl
    from yeoman_gateway.history.layer1 import canonical_json

    root = tmp_path / "raw"
    write_jsonl(root / "whatsapp/2026-10.jsonl", [
        _raw("outbound_request", "send_text", {"to": "synthetic@g.us", "text": "private fixture body"},
             direction="out", corr="missing"),
        _raw("outbound_result", "send_text", {}, direction="out", corr="reverse"),
    ])
    db = tmp_path / "history.db"
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        raw_state = conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0]
        assert json.loads(raw_state)["pending_pairs"] == {
            canonical_json(["default", "missing"]): ["whatsapp/2026-10.jsonl#1"],
            canonical_json(["default", "reverse"]): ["whatsapp/2026-10.jsonl#2"],
        }
        assert "private fixture body" not in raw_state
        assert conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)


def test_full_checkpoint_rejects_partial_tail_without_replacing_db(tmp_path):
    import pytest
    from hist_fixtures import write_jsonl

    root = tmp_path / "raw"
    source = write_jsonl(root / "whatsapp/2026-10.jsonl", [{"purged_version": 1}])
    db = tmp_path / "history.db"
    project([root], db)
    before = db.read_bytes()
    with source.open("ab") as handle:
        handle.write(b'{"partial":')
    original = source.read_bytes()
    with pytest.raises(ValueError, match="incomplete Layer 1 tail"):
        project([root], db)
    assert source.read_bytes() == original and db.read_bytes() == before
    assert not db.with_name(db.name + ".building").exists()
