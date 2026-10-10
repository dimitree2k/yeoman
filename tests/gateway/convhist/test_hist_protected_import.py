"""Synthetic protected-import witnesses; no provider sockets or production paths."""
import hashlib
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


def backfill_row(name, *, text='synthetic'):
    return backfill_line(channel='whatsapp', kind='message', provenance='native',
                         time_certainty='native', occurred_ms=100, direction='in', chat_id='c1',
                         payload={'messageId': name, 'text': text},
                         origin=Origin('snapshot', 'source.db', 'messages', name),
                         original={'uuid': name, 'received_ms': 100})


def derived_row(text):
    return {'derived_version': 1, 'kind': 'media_description', 'channel': 'whatsapp',
            'chat_id': 'c1', 'native_message_id': 'm1', 'generated_ms': 200, 'text': text}


def stage(root, files):
    """Write one staged package; every value is physical rows for that relative path."""
    for relative, rows in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(canonical_json(item) + '\n' for item in rows))
    return root


def digests(root):
    """Byte-level tree digest, so 'unchanged' covers content, not just the file list."""
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def imported(tmp_path, root, name, files):
    """A first, complete protected import of *files*; returns its manifest digest."""
    source = stage(tmp_path / name, files)
    manifest = prepare_import_manifest(source)
    assert records.import_backfill(root, source, manifest)['status'] == 'complete'
    return manifest['package_digest']


def assert_cutover_refs_bind(root, receipt, relative):
    """The exact binding prepare_final_owner_package applies to every completed row."""
    lines = (root / relative).read_bytes().splitlines(keepends=True)
    for number, expected in enumerate(receipt['files'][relative]['row_hashes'], 1):
        base, final = receipt['ref_map'][f'{relative}#{number}'].split('#')
        assert base == relative
        assert hashlib.sha256(lines[int(final) - 1]).hexdigest() == expected


def test_import_suppresses_destination_of_an_earlier_complete_import(tmp_path, monkeypatch):
    """Witness 1: earlier complete receipt pins the destination; rows are suppressed."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    earlier = imported(tmp_path, root, 'first', {'backfill/a.jsonl': [backfill_row('m1')]})
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    # The later acquisition still stages the same legacy rows; only the derived snapshot moved on.
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': [backfill_row('m1')],
                                         'derived/media-descriptions.jsonl': [derived_row('new')]})
    manifest = prepare_import_manifest(source)
    assert manifest['package_digest'] != earlier
    assert records._import_receipt(root, earlier)['status'] == 'complete'
    before = digests(root)
    preview = records.preview_import(root, source, manifest)
    assert digests(root) == before and destination.read_bytes() == pinned
    planned = preview['files']['backfill/a.jsonl']
    assert planned['preexisting'] is True and planned['suppressed'] == 1
    assert planned['bytes'] == len(pinned) and planned['base_sha256'] == hashlib.sha256(pinned).hexdigest()
    result = records.import_backfill(root, source, manifest)
    assert result['status'] == 'complete'
    assert destination.read_bytes() == pinned
    assert result['files']['backfill/a.jsonl'] == planned
    assert result['ref_map']['backfill/a.jsonl#1'] == 'backfill/a.jsonl#1'
    receipt = records._import_receipt(root, manifest['package_digest'])
    assert receipt['status'] == 'complete' and receipt['files'] == result['files']
    assert_cutover_refs_bind(root, receipt, 'backfill/a.jsonl')
    assert_cutover_refs_bind(root, receipt, 'derived/media-descriptions.jsonl')
    assert digests(root)['backfill/a.jsonl'] == before['backfill/a.jsonl']
    assert digests(root)['derived/media-descriptions.jsonl'] == result['files']['derived/media-descriptions.jsonl']['sha256']


def test_import_still_refuses_destination_without_any_receipt(tmp_path, monkeypatch):
    """Witness 2: no covering receipt anywhere keeps the FileExistsError refusal."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    destination = root / 'backfill/a.jsonl'
    destination.parent.mkdir(parents=True)
    destination.write_bytes((canonical_json(backfill_row('m1')) + '\n').encode())
    source = stage(tmp_path / 'staged', {'backfill/a.jsonl': [backfill_row('m1')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    with pytest.raises(FileExistsError, match='without import receipt'):
        records.preview_import(root, source, manifest)
    assert digests(root) == before
    with pytest.raises(FileExistsError, match='without import receipt'):
        records.import_backfill(root, source, manifest)
    assert digests(root) == before
    assert records._import_receipt(root, manifest['package_digest']) is None


def test_import_refuses_when_recorded_post_image_no_longer_matches(tmp_path, monkeypatch):
    """Witness 3: a receipt covers the path but the file diverged from its post-image."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    imported(tmp_path, root, 'first', {'backfill/a.jsonl': [backfill_row('m1')]})
    destination = root / 'backfill/a.jsonl'
    destination.write_bytes(destination.read_bytes() + b'{"tampered":true}\n')
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': [backfill_row('m1')],
                                         'derived/media-descriptions.jsonl': [derived_row('new')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    with pytest.raises(ValueError, match='import destination prefix changed'):
        records.preview_import(root, source, manifest)
    with pytest.raises(ValueError, match='import destination prefix changed'):
        records.import_backfill(root, source, manifest)
    assert digests(root) == before


def test_import_refuses_when_destination_lacks_the_planned_rows(tmp_path, monkeypatch):
    """A divergent package is refused, never silently marked suppressed."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    imported(tmp_path, root, 'first', {'backfill/a.jsonl': [backfill_row('m1')]})
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': [backfill_row('m1'), backfill_row('m2')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    with pytest.raises(ValueError, match='does not contain the planned rows'):
        records.preview_import(root, source, manifest)
    with pytest.raises(ValueError, match='does not contain the planned rows'):
        records.import_backfill(root, source, manifest)
    assert digests(root) == before and destination.read_bytes() == pinned


def test_import_uses_the_latest_covering_receipt_without_stale_fallback(tmp_path, monkeypatch):
    """Several receipts cover one path: the newest post-image decides, older ones are stale."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    first = {'backfill/a.jsonl': [backfill_row('m1')]}
    imported(tmp_path, root, 'first', first)
    destination = root / 'backfill/a.jsonl'
    rewound = destination.read_bytes()
    destination.unlink()
    later = imported(tmp_path, root, 'second', {'backfill/a.jsonl': [backfill_row('m1', text='later')]})
    assert records._import_receipt(root, later)['files']['backfill/a.jsonl']['sha256'] == hashlib.sha256(
        destination.read_bytes()).hexdigest()
    destination.write_bytes(rewound)  # a rewound destination still matching the older receipt
    source = stage(tmp_path / 'third', {'backfill/a.jsonl': [backfill_row('m1')],
                                        'derived/media-descriptions.jsonl': [derived_row('new')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    with pytest.raises(ValueError, match='import destination prefix changed'):
        records.preview_import(root, source, manifest)
    with pytest.raises(ValueError, match='import destination prefix changed'):
        records.import_backfill(root, source, manifest)
    assert digests(root) == before


def test_fresh_import_plan_and_receipt_are_unchanged(tmp_path, monkeypatch):
    """Witness 4: no file and no receipt still appends, records, and re-runs idempotently."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    source = stage(tmp_path / 'staged', {'backfill/a.jsonl': [backfill_row('m1')]})
    manifest = prepare_import_manifest(source)
    preview = records.preview_import(root, source, manifest)
    info = preview['files']['backfill/a.jsonl']
    assert preview['status'] == 'partial' and 'preexisting' not in info
    assert info['suppressed'] == 0 and info['base_bytes'] == 0
    assert info['bytes'] == (source / 'backfill/a.jsonl').stat().st_size
    assert not root.exists()
    result = records.import_backfill(root, source, manifest)
    assert result['status'] == 'complete' and result['files'] == preview['files']
    assert (root / 'backfill/a.jsonl').read_bytes() == (source / 'backfill/a.jsonl').read_bytes()
    receipt = records._import_receipt(root, manifest['package_digest'])
    assert receipt['status'] == 'complete' and receipt['files'] == result['files']
    complete = digests(root)
    assert records.import_backfill(root, source, manifest) == result
    assert digests(root) == complete


def test_import_suppresses_old_destination_and_appends_new_one(tmp_path, monkeypatch):
    """Witness 5: one already-imported destination and one genuinely new one in a package."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    imported(tmp_path, root, 'first', {'backfill/a.jsonl': [backfill_row('m1')]})
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': [backfill_row('m1')],
                                         'backfill/c.jsonl': [backfill_row('m3')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    preview = records.preview_import(root, source, manifest)
    assert digests(root) == before
    assert preview['files']['backfill/a.jsonl']['suppressed'] == 1
    assert preview['files']['backfill/c.jsonl']['suppressed'] == 0
    result = records.import_backfill(root, source, manifest)
    assert result['status'] == 'complete' and result['files'] == preview['files']
    assert destination.read_bytes() == pinned
    assert (root / 'backfill/c.jsonl').read_bytes() == (source / 'backfill/c.jsonl').read_bytes()
    receipt = records._import_receipt(root, manifest['package_digest'])
    assert set(receipt['files']) == {'backfill/a.jsonl', 'backfill/c.jsonl'}
    assert receipt['files']['backfill/a.jsonl']['preexisting'] is True
    assert 'preexisting' not in receipt['files']['backfill/c.jsonl']
    assert receipt['files']['backfill/c.jsonl'] == result['files']['backfill/c.jsonl']
    assert receipt['ref_map']['backfill/a.jsonl#1'] == 'backfill/a.jsonl#1'
    assert receipt['ref_map']['backfill/c.jsonl#1'] == 'backfill/c.jsonl#1'
    assert_cutover_refs_bind(root, receipt, 'backfill/a.jsonl')
    assert_cutover_refs_bind(root, receipt, 'backfill/c.jsonl')


def test_preview_import_never_mutates_the_archive(tmp_path, monkeypatch):
    """Witness 6: preview hashes the same before and after in every destination state."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    fresh = stage(tmp_path / 'fresh', {'backfill/a.jsonl': [backfill_row('m1')]})
    assert records.preview_import(root, fresh, prepare_import_manifest(fresh))['status'] == 'partial'
    assert not root.exists()
    assert records.import_backfill(root, fresh, prepare_import_manifest(fresh))['status'] == 'complete'
    covered = stage(tmp_path / 'covered', {'backfill/a.jsonl': [backfill_row('m1')],
                                           'derived/media-descriptions.jsonl': [derived_row('new')]})
    manifest = prepare_import_manifest(covered)
    before = digests(root)
    preview = records.preview_import(root, covered, manifest)
    assert preview['files']['backfill/a.jsonl']['suppressed'] == 1
    assert digests(root) == before
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    destination.write_bytes(pinned + b'{"tampered":true}\n')
    before = digests(root)
    with pytest.raises(ValueError):
        records.preview_import(root, covered, manifest)
    assert digests(root) == before
    destination.write_bytes(pinned)
    foreign = stage(tmp_path / 'foreign', {'backfill/z.jsonl': [backfill_row('z1')]})
    (root / 'backfill/z.jsonl').write_bytes((canonical_json(backfill_row('z1')) + '\n').encode())
    before = digests(root)
    with pytest.raises(FileExistsError):
        records.preview_import(root, foreign, prepare_import_manifest(foreign))
    assert digests(root) == before


def test_suppressed_destination_resumes_after_a_partial_publication(tmp_path, monkeypatch):
    """A crash after the pinned partial receipt resumes without touching the suppressed file."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    imported(tmp_path, root, 'first', {'backfill/a.jsonl': [backfill_row('m1')]})
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': [backfill_row('m1')],
                                         'backfill/c.jsonl': [backfill_row('m3')]})
    manifest = prepare_import_manifest(source)
    real = records._publish_import_file
    published = []

    def crash(path, addition, **kwargs):
        result = real(path, addition, **kwargs)
        published.append(path.name)
        if path.name == 'c.jsonl':
            raise OSError('synthetic crash after the new publication')
        return result

    monkeypatch.setattr(records, '_publish_import_file', crash)
    with pytest.raises(OSError):
        records.import_backfill(root, source, manifest)
    partial = records._import_receipt(root, manifest['package_digest'])
    assert published == ['a.jsonl', 'c.jsonl']
    assert partial['status'] == 'partial' and partial['files']['backfill/a.jsonl']['preexisting'] is True
    assert destination.read_bytes() == pinned
    monkeypatch.setattr(records, '_publish_import_file', real)
    before = digests(root)
    assert records.preview_import(root, source, manifest)['files']['backfill/a.jsonl']['suppressed'] == 1
    assert digests(root) == before
    result = records.import_backfill(root, source, manifest)
    assert result['status'] == 'complete'
    assert destination.read_bytes() == pinned
    assert records._import_receipt(root, manifest['package_digest'])['status'] == 'complete'


def test_suppressed_rows_claim_distinct_identical_slots_only(tmp_path, monkeypatch):
    """Byte-identical rows each claim their own slot; a row without one is refused."""
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    repeated = [backfill_row('m1'), backfill_row('m1')]
    imported(tmp_path, root, 'first', {'backfill/a.jsonl': repeated})
    destination = root / 'backfill/a.jsonl'
    pinned = destination.read_bytes()
    source = stage(tmp_path / 'second', {'backfill/a.jsonl': repeated,
                                         'derived/media-descriptions.jsonl': [derived_row('new')]})
    manifest = prepare_import_manifest(source)
    before = digests(root)
    result = records.import_backfill(root, source, manifest)
    assert digests(root)['backfill/a.jsonl'] == before['backfill/a.jsonl'] == hashlib.sha256(pinned).hexdigest()
    assert result['files']['backfill/a.jsonl']['suppressed'] == 2
    refs = {result['ref_map'][f'backfill/a.jsonl#{n}'] for n in (1, 2)}
    assert refs == {'backfill/a.jsonl#1', 'backfill/a.jsonl#2'}
    assert destination.read_bytes() == pinned
    # One further identical row has no unclaimed destination slot: refused, not double-counted.
    extra = stage(tmp_path / 'third', {'backfill/a.jsonl': [*repeated, backfill_row('m1')],
                                       'derived/media-descriptions.jsonl': [derived_row('new')]})
    extra_manifest = prepare_import_manifest(extra)
    before = digests(root)
    with pytest.raises(ValueError, match='does not contain the planned rows'):
        records.preview_import(root, extra, extra_manifest)
    with pytest.raises(ValueError, match='does not contain the planned rows'):
        records.import_backfill(root, extra, extra_manifest)
    assert digests(root) == before
