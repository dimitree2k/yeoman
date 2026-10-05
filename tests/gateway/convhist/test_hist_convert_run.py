import json
from pathlib import Path

import pytest
from hist_fixtures import INBOUND_DDL, make_db, write_jsonl
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.history.convert.run import run_conversion

runner = CliRunner()


def _home(tmp_path):
    home = tmp_path / "snapshot"
    make_db(home / "data/inbound/reply_context.db", INBOUND_DDL, {"inbound_messages": [
        {"channel": "whatsapp", "chat_id": "1-2@g.us", "message_id": "M1", "participant": None,
         "sender_id": "4917632625469", "text": "hi", "timestamp": 1788945095, "created_at": None,
         "sender_name": "Frank", "reply_to_message_id": None}]})
    write_jsonl(home / "data/inbound/whatsapp_1-2@g.us.jsonl", [
        {"_type": "metadata"}, {"role": "user", "content": "hi", "timestamp": "1788945095",
                                "sender_id": "4917632625469", "message_id": "M1"}])
    return home


def test_run_writes_every_target_and_reports(tmp_path):
    report = run_conversion(_home(tmp_path), tmp_path / "out" / "raw", decode=None)
    files = report["files"]
    assert files["backfill/reply_context.jsonl"]["lines"] == 1
    assert files["backfill/session_jsonl.jsonl"] == {
        "lines": 2, "kinds": {"message": 1, "session_meta": 1}, "skipped": {"metadata": 1}}
    assert files["backfill/journal.jsonl"]["lines"] == 0
    assert (tmp_path / "out/raw/derived/media-descriptions.jsonl").exists()


def test_rerun_refuses_and_keeps_bytes(tmp_path):
    home, out = _home(tmp_path), tmp_path / "out" / "raw"
    run_conversion(home, out, decode=None)
    before = {p: p.read_bytes() for p in out.rglob("*.jsonl")}
    with pytest.raises(FileExistsError):
        run_conversion(home, out, decode=None)
    assert {p: p.read_bytes() for p in out.rglob("*.jsonl")} == before


def test_refuses_protected_output(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    with pytest.raises(PermissionError):
        run_conversion(_home(tmp_path), tmp_path / "home" / "data" / "raw", decode=None)


def test_refuses_live_home_as_source(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    with pytest.raises(PermissionError):
        run_conversion(tmp_path / "home", tmp_path / "out", decode=None)


def test_cli_convert_and_seed(tmp_path):
    home, out = _home(tmp_path), tmp_path / "out" / "raw"
    result = runner.invoke(app, ["history", "convert", "--source-home", str(home), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"]["backfill/reply_context.jsonl"]["lines"] == 1
    again = runner.invoke(app, ["history", "convert", "--source-home", str(home), "--out", str(out)])
    assert again.exit_code != 0
    seed = runner.invoke(app, ["history", "seed-attestations", "--out", str(out)])
    assert seed.exit_code == 0 and (out / "owner/attestations.jsonl").exists()


def test_partial_target_refuses_before_writing_other_targets(tmp_path):
    home, out = _home(tmp_path), tmp_path / "out" / "raw"
    partial = out / "derived" / "media-descriptions.jsonl.partial"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"unfinished\n")
    with pytest.raises(FileExistsError):
        run_conversion(home, out, decode=None)
    assert set(out.rglob("*")) == {out / "derived", partial}
    assert partial.read_bytes() == b"unfinished\n"


@pytest.mark.parametrize("suffix", ["", ".partial"])
def test_dangling_late_target_refuses_before_writing_or_mutating_links(tmp_path, suffix):
    home, out = _home(tmp_path), tmp_path / "out" / "raw"
    target = out / "derived" / f"media-descriptions.jsonl{suffix}"
    target.parent.mkdir(parents=True)
    dangling_to = "missing-destination"
    target.symlink_to(dangling_to)

    with pytest.raises(FileExistsError):
        run_conversion(home, out, decode=None)

    assert set(out.rglob("*")) == {out / "derived", target}
    assert target.is_symlink()
    assert target.readlink() == Path(dangling_to)
