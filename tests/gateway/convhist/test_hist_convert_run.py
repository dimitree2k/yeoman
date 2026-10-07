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


def test_import_manifest_and_cli_are_read_only_until_confirm(tmp_path, monkeypatch):
    from yeoman_gateway.history.convert.run import prepare_import_manifest
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.writer import RawArchive

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    staged = tmp_path / 'staged'
    run_conversion(_home(tmp_path), staged, decode=None)
    manifest = prepare_import_manifest(staged)
    assert manifest['version'] == 1 and manifest['snapshot_identity']
    item = manifest['files']['backfill/reply_context.jsonl']
    assert item['lines'] == 1 and item['bytes'] > 0 and len(item['sha256']) == 64
    assert item['rows'][0]['original_row_sha256'] and 'uuid' in item['rows'][0]
    assert manifest['ref_map']['backfill/reply_context.jsonl#1'] == 'backfill/reply_context.jsonl#1'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(RawArchive, '__init__', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('RawArchive')))
    argv = ['history', 'import-backfill', '--staged', str(staged), '--manifest', str(path)]
    for modes in [[], ['--dry-run', '--confirm']]:
        assert runner.invoke(app, argv + modes).exit_code != 0
    result = runner.invoke(app, argv + ['--dry-run'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['files'] == len(manifest['files'])
    assert 'reply_context' not in result.output and str(staged) not in result.output
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    result = runner.invoke(app, argv + ['--confirm'])
    assert result.exit_code == 0, result.output
    assert (raw_root() / 'backfill/reply_context.jsonl').read_bytes() == (staged / 'backfill/reply_context.jsonl').read_bytes()
    for command in [['history', 'seed-attestations', '--out', str(raw_root())],
                    ['history', 'convert', '--source-home', str(_home(tmp_path / 'other')), '--out', str(raw_root())]]:
        before = {str(p): p.read_bytes() for p in raw_root().rglob('*') if p.is_file()}
        assert runner.invoke(app, command).exit_code != 0
        assert {str(p): p.read_bytes() for p in raw_root().rglob('*') if p.is_file()} == before


def test_import_manifest_binds_absolute_origins_without_absolute_locators(tmp_path):
    from yeoman_gateway.history.convert.run import prepare_import_manifest
    from yeoman_gateway.history.layer1 import Origin, backfill_line, canonical_json

    staged = tmp_path / 'staged'
    path = staged / 'backfill/source.jsonl'
    path.parent.mkdir(parents=True)
    row = backfill_line(channel='whatsapp', kind='message', provenance='native', time_certainty='native',
                        occurred_ms=100, direction='in', chat_id='c1', payload={'messageId': 'm1'},
                        origin=Origin('synthetic', '/synthetic-private/source.db', 'messages', 'one'),
                        original={'uuid': 'synthetic-uuid', 'text': 'synthetic'})
    path.write_text(canonical_json(row) + '\n')
    original = path.read_bytes()
    manifest = prepare_import_manifest(staged)
    assert '/synthetic-private/source.db' not in json.dumps(manifest)
    locator = manifest['files']['backfill/source.jsonl']['rows'][0]
    assert not Path(locator['origin']['path']).is_absolute()
    assert locator['origin']['inventory_id'] in manifest['source_inventory']
    assert locator['original_row_sha256'] == row['origin']['row_sha256']
    assert locator['uuid'] == 'synthetic-uuid'
    assert path.read_bytes() == original

    from yeoman_shared.raw_archive import records
    locator['origin']['path'] = '/synthetic-private/source.db'
    manifest['package_digest'] = records.import_manifest_digest(manifest)
    with pytest.raises(ValueError, match='original locator'):
        records.validate_import_manifest(staged, manifest)
    assert path.read_bytes() == original
