"""Synthetic protected-import witnesses; no provider sockets or production paths."""
import json

import pytest
from yeoman_gateway.history.convert import run as convert_run
from yeoman_gateway.history.layer1 import Origin, backfill_line, canonical_json
from yeoman_shared.raw_archive import records
from yeoman_shared.raw_archive.paths import raw_root
from yeoman_shared.raw_archive.purge import PurgeSelector, purge


def prepare_import_manifest(staged):
    return convert_run.prepare_import_manifest(staged)


def package(tmp_path):
    staged = tmp_path / 'staged'
    for name, mid in [('a', 'm1'), ('b', 'm2')]:
        row = backfill_line(channel='whatsapp', kind='message', provenance='native',
                            time_certainty='native', occurred_ms=100, direction='in', chat_id='c1',
                            payload={'messageId': mid, 'text': 'synthetic'},
                            origin=Origin('snapshot', 'source.db', 'messages', name),
                            original={'uuid': name, 'received_ms': 100})
        path = staged / 'backfill' / f'{name}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(row) + '\n')
    derived = {'derived_version': 1, 'kind': 'media_description', 'channel': 'whatsapp',
               'chat_id': 'c1', 'native_message_id': 'm1', 'generated_ms': 200, 'text': 'synthetic'}
    path = staged / 'derived/media-descriptions.jsonl'
    path.parent.mkdir()
    path.write_text(canonical_json(derived) + '\n' + canonical_json({**derived, 'text': 'other'}) + '\n')
    return staged


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_protected_import_resume_refuses_changed_destination(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    manifest = prepare_import_manifest(staged)
    # Every invalid package must fail before even creating the destination root.
    for bad in [{}, {**manifest, 'package_digest': '0' * 64}]:
        with pytest.raises(ValueError):
            records.import_backfill(root, staged, bad)
        assert not root.exists()
    path = staged / 'backfill/b.jsonl'
    original = path.read_bytes()
    path.write_bytes(original + b'{}\n')
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert not root.exists()
    path.write_bytes(original)
    path.unlink()
    path.symlink_to(staged / 'backfill/a.jsonl')
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert not root.exists()
    path.unlink()
    path.write_bytes(original)
    hidden = staged / 'backfill/b.jsonl.hidden'
    path.rename(hidden)
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert not root.exists()
    hidden.rename(path)
    live = root / 'derived/media-descriptions.jsonl'
    live.parent.mkdir(parents=True)
    first = (staged / 'derived/media-descriptions.jsonl').read_bytes().splitlines(keepends=True)[0]
    live.write_bytes(b'{"live":true}\n' + first)
    before = tree(root)
    foreign = root / 'backfill/b.jsonl'
    foreign.parent.mkdir()
    foreign.write_bytes(b'foreign\n')
    with pytest.raises((ValueError, FileExistsError)):
        records.import_backfill(root, staged, manifest)
    assert live.read_bytes() == before['derived/media-descriptions.jsonl']
    assert not (root / 'backfill/a.jsonl').exists()
    foreign.unlink()
    real = records._publish_import_file
    calls = 0

    def crash(*args, **kwargs):
        nonlocal calls
        result = real(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError('synthetic crash after durable publication')
        return result

    monkeypatch.setattr(records, '_publish_import_file', crash)
    with pytest.raises(OSError):
        records.import_backfill(root, staged, manifest)
    receipt = json.loads((root / records.IMPORT_RECEIPTS).read_text().splitlines()[-1])
    assert receipt['status'] == 'partial'
    assert (root / 'backfill/a.jsonl').read_bytes() == (staged / 'backfill/a.jsonl').read_bytes()
    monkeypatch.setattr(records, '_publish_import_file', real)
    live_prefix = live.read_bytes()
    live.write_bytes(b'changed\n')
    changed = tree(root)
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert tree(root) == changed
    live.write_bytes(live_prefix)
    result = records.import_backfill(root, staged, manifest)
    assert result['status'] == 'complete'
    assert result['ref_map']['derived/media-descriptions.jsonl#1'] == 'derived/media-descriptions.jsonl#2'
    assert result['ref_map']['derived/media-descriptions.jsonl#2'] == 'derived/media-descriptions.jsonl#3'
    assert live.read_bytes() == b'{"live":true}\n' + (staged / 'derived/media-descriptions.jsonl').read_bytes()
    complete = tree(root)
    assert records.import_backfill(root, staged, manifest) == result
    assert tree(root) == complete
    (root / 'backfill/a.jsonl').write_bytes(b'altered\n')
    changed = tree(root)
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert tree(root) == changed


def test_protected_import_and_owner_append_obey_disposition(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=300)
    result = records.import_backfill(root, staged, prepare_import_manifest(staged))
    assert result['status'] == 'complete'
    assert json.loads((root / 'backfill/a.jsonl').read_text()) == records.TOMBSTONE
    assert (root / 'derived/media-descriptions.jsonl').read_text().splitlines() == [records.dumps(records.TOMBSTONE)] * 2
    assert result['ref_map']['backfill/a.jsonl#1'] == 'backfill/a.jsonl#1'
    assert result['ref_map']['derived/media-descriptions.jsonl#2'] == 'derived/media-descriptions.jsonl#2'
    seed = {'attestation_version': 2, 'type': 'contact', 'channel': 'whatsapp', 'chat_id': 'c1',
            'native_id': 'm1', 'received_ms': 100}
    assert records.append_owner_record(root, seed) is None
    assert not (root / 'owner/attestations.jsonl').read_bytes()


def test_disposed_content_cannot_return_via_drain_or_import(tmp_path, monkeypatch):
    import yeoman_shared.raw_archive.writer as writer
    from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    archive = RawArchive(root=root, spool=tmp_path / 'spool')
    real = writer.append_line
    monkeypatch.setattr(writer, 'append_line', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk')))
    assert archive.append(RawEvent(channel='whatsapp', kind='inbound', direction='in',
                                   native={'payload': {'messageId': 'm1', 'text': 'synthetic'}},
                                   native_id='m1', chat_id='c1', received_ms=100)) is False
    monkeypatch.setattr(writer, 'append_line', real)
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=300)
    assert archive.drain_spool() == 1
    result = records.import_backfill(root, staged, prepare_import_manifest(staged))
    assert result['status'] == 'complete'
    assert json.loads((root / 'backfill/a.jsonl').read_text()) == records.TOMBSTONE
    assert not list((tmp_path / 'spool').glob('*.json'))


def test_import_partial_batch_keeps_segment_slots_and_original_scrub(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    path = staged / 'backfill/a.jsonl'
    row = json.loads(path.read_text())
    row['payload']['segments'] = [{'messageId': 'm1', 'text': 'synthetic erased'},
                                   {'messageId': 'm2', 'text': 'synthetic kept'}]
    path.write_text(canonical_json(row) + '\n')
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=300)
    result = records.import_backfill(root, staged, prepare_import_manifest(staged))
    published = json.loads((root / 'backfill/a.jsonl').read_text())
    assert published['payload']['segments'][0] == records.TOMBSTONE
    assert published['payload']['segments'][1]['text'] == 'synthetic kept'
    assert 'original' not in published and 'origin' not in published
    assert result['ref_map']['backfill/a.jsonl#1/1'] == 'backfill/a.jsonl#1/1'
    assert records.append_owner_record(root, {'attestation_version': 2, 'type': 'author',
                                             'source_ref': 'backfill/a.jsonl#1/0'}) is None


def test_import_crash_mid_derived_and_staging_resumes_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    manifest = prepare_import_manifest(staged)
    real = records.append_line

    def crash(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if path.name == 'media-descriptions.jsonl':
            raise OSError('synthetic crash after first derived line')
        return result

    monkeypatch.setattr(records, 'append_line', crash)
    with pytest.raises(OSError):
        records.import_backfill(root, staged, manifest)
    monkeypatch.setattr(records, 'append_line', real)
    assert records.import_backfill(root, staged, manifest)['status'] == 'complete'
    assert (root / 'derived/media-descriptions.jsonl').read_bytes() == (staged / 'derived/media-descriptions.jsonl').read_bytes()
    # New package while old journal partial entries exist is valid after completion.
    other = tmp_path / 'delta'
    (other / 'backfill').mkdir(parents=True)
    (other / 'backfill/c.jsonl').write_bytes((staged / 'backfill/a.jsonl').read_bytes())
    assert records.import_backfill(root, other, prepare_import_manifest(other))['status'] == 'complete'


def test_import_rechecks_disposition_under_root_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    manifest = prepare_import_manifest(staged)
    real = records.lock_file
    injected = False

    def lock(path, **kwargs):
        nonlocal injected
        fd = real(path, **kwargs)
        if path == root / records.PURGE_DISPOSITION_LOCK and not injected:
            injected = True
            records.append_line(root / 'AUDIT', records.dumps({
                'removed_sha256': [], 'disposition': {'scope': 'message', 'channel': 'whatsapp',
                'chat_id': 'c1', 'before_ms': None, 'message_identities': ['m1'], 'correlation_ids': []}}))
        return fd

    monkeypatch.setattr(records, 'lock_file', lock)
    result = records.import_backfill(root, staged, manifest)
    assert injected and result['status'] == 'complete'
    assert json.loads((root / 'backfill/a.jsonl').read_text()) == records.TOMBSTONE


def test_prevalidation_rejects_destination_symlink_and_staging_partial(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    manifest = prepare_import_manifest(staged)
    dest = root / 'backfill/b.jsonl'
    dest.parent.mkdir(parents=True)
    dest.symlink_to(tmp_path / 'outside.jsonl')
    before = {str(p) for p in root.rglob('*')}
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert {str(p) for p in root.rglob('*')} == before
    dest.unlink()
    pending = dest.with_name(dest.name + '.import-partial')
    pending.write_bytes(b'unbound')
    before = tree(root)
    with pytest.raises(ValueError):
        records.import_backfill(root, staged, manifest)
    assert tree(root) == before


def test_old_legacy_owner_author_cannot_revive_disposed_message(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=300)
    assert records.append_owner_record(root, {'attestation_version': 2, 'type': 'message_author',
                                             'message_id': 'whatsapp:c1:m1', 'anchor': '10001@s.whatsapp.net',
                                             'at_ms': 400, 'note': 'synthetic'}) is None
    assert records.append_owner_record(root, {'attestation_version': 2, 'type': 'message_author',
                                             'message_id': 'whatsapp:other:m1', 'anchor': '10001@s.whatsapp.net',
                                             'at_ms': 400, 'note': 'synthetic'}) is not None
    assert len((root / 'owner/attestations.jsonl').read_text().splitlines()) == 1


def test_owner_disposition_missing_source_keeps_audit_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    source = root / 'whatsapp/test.jsonl'
    owner = {'attestation_version': 2, 'type': 'author', 'source_ref': 'whatsapp/test.jsonl#1',
             'channel': 'whatsapp', 'chat_id': 'c1', 'native_message_id': 'm1',
             'at_ms': 400, 'note': 'synthetic', 'anchor': '10001@s.whatsapp.net'}
    assert records.append_owner_record(root, owner) is not None
    source.parent.mkdir(exist_ok=True)
    source.write_text(records.dumps({'channel': 'whatsapp', 'chat_id': 'c1', 'native_id': 'm1'}) + '\n')
    real = records.iter_records

    def vanished(path):
        if path == source:
            path.unlink(missing_ok=True)
        yield from real(path)

    monkeypatch.setattr(records, 'iter_records', vanished)
    assert records.append_owner_record(root, owner) is not None
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=500)
    assert records.append_owner_record(root, owner) is None
    # The missing source never masks malformed AUDIT, even after a matching entry.
    records.append_protected(root / 'AUDIT', '{broken')
    with pytest.raises(OSError, match='purge dispositions'):
        records.append_owner_record(root, owner)


@pytest.mark.parametrize('scenario', ['missing', 'restored', 'explicit', 'base'])
def test_canonical_author_is_bound_to_purged_source_evidence(tmp_path, monkeypatch, scenario):
    from yeoman_gateway.history.attestations import make

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    source = root / 'backfill/a.jsonl'
    row = json.loads((package(tmp_path) / 'backfill/a.jsonl').read_text())
    if scenario in ('explicit', 'base'):
        row['payload'] = {'messageId': 'm1', 'segments': [
            {'messageId': 'm2', 'text': 'synthetic kept'}, {'messageId': 'm1', 'text': 'synthetic erased'}]}
    source.parent.mkdir(parents=True)
    original = (canonical_json(row) + '\n').encode()
    source.write_bytes(original)
    purge(root, PurgeSelector('whatsapp', chat_id='c1', native_id='m1'), operator='synthetic', now_ms=300)
    if scenario == 'missing':
        source.unlink()
    else:
        source.write_bytes(original)  # Simulate an uncoordinated restored original, not a tombstone rescue.
    ref = 'backfill/a.jsonl#1' + ('/1' if scenario == 'explicit' else '')
    author = make('author', 400, 'synthetic', source_ref=ref, anchor='10001@s.whatsapp.net')
    try:
        result = records.append_owner_record(root, author)
    except (ValueError, OSError):
        result = None
    assert result is None
    owner = root / 'owner/attestations.jsonl'
    assert not owner.exists() or not owner.read_bytes()
    if scenario in ('explicit', 'base'):
        # Purging the row's native-owning segment must not suppress the other speaker.
        survivor = make('author', 400, 'synthetic', source_ref='backfill/a.jsonl#1/0', anchor='10001@s.whatsapp.net')
        assert records.append_owner_record(root, survivor) is not None


def test_owner_disposition_rechecked_at_lock_acquisition(tmp_path, monkeypatch):
    from yeoman_gateway.history.attestations import make

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    source = root / 'backfill/a.jsonl'
    source.parent.mkdir(parents=True)
    source.write_bytes((package(tmp_path) / 'backfill/a.jsonl').read_bytes())
    real = records.lock_file
    injected = False

    def lock(path, **kwargs):
        nonlocal injected
        fd = real(path, **kwargs)
        if path == root / records.PURGE_DISPOSITION_LOCK and not injected:
            injected = True
            records.append_line(root / 'AUDIT', records.dumps({
                'removed_sha256': [], 'disposition': {'scope': 'message', 'channel': 'whatsapp',
                'chat_id': 'c1', 'before_ms': None, 'message_identities': ['m1'], 'correlation_ids': []}}))
        return fd

    monkeypatch.setattr(records, 'lock_file', lock)
    author = make('author', 400, 'synthetic', source_ref='backfill/a.jsonl#1', anchor='10001@s.whatsapp.net')
    assert records.append_owner_record(root, author) is None
    assert injected


def test_self_consistent_traversal_manifest_reaches_path_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root, staged = raw_root(), package(tmp_path)
    manifest = prepare_import_manifest(staged)
    old, malicious = 'backfill/b.jsonl', '../escape.jsonl'
    manifest['files'][malicious] = manifest['files'].pop(old)
    for row in manifest['files'][malicious]['rows']:
        row['source_ref'] = row['source_ref'].replace(old, malicious)
    manifest['ref_map'] = {key.replace(old, malicious): value.replace(old, malicious)
                           for key, value in manifest['ref_map'].items()}
    manifest['snapshot_boundary']['files'][malicious] = manifest['snapshot_boundary']['files'].pop(old)
    from yeoman_gateway.history.layer1 import row_sha256
    manifest['snapshot_identity'] = row_sha256({'files': manifest['files'], 'source_inventory': manifest['source_inventory']})
    manifest['package_digest'] = records.import_manifest_digest(manifest)
    # Supply an inventory response matching the malicious envelope; no outside file is read.
    real = type(staged).rglob
    class Escape:
        def relative_to(self, root):
            return type(staged)(malicious)
    monkeypatch.setattr(type(staged), 'rglob', lambda self, pattern: [
        Escape() if p.relative_to(staged).as_posix() == old else p for p in real(self, pattern)])
    with pytest.raises(ValueError, match='unsupported import destination'):
        records.import_backfill(root, staged, manifest)
    assert not root.exists()
