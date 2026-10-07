import json
import os
import subprocess
import sys

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.history.attestations import make
from yeoman_shared.raw_archive.paths import raw_root

runner = CliRunner()
ANCHOR = '10001@s.whatsapp.net'


def snapshot(root):
    return {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_attest_cli_validates_before_any_write(tmp_path, monkeypatch):
    from yeoman_shared.raw_archive.writer import RawArchive

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    source = tmp_path / 'owner.jsonl'
    contact = make('contact', 100, 'synthetic private note', identifiers=[ANCHOR])
    source.write_text(json.dumps(contact) + '\n')
    argv = ['history', 'attest', '--file', str(source)]
    monkeypatch.setattr(RawArchive, '__init__', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('constructed archive')))
    before = snapshot(tmp_path)
    for modes in [[], ['--dry-run', '--confirm']]:
        assert runner.invoke(app, argv + modes).exit_code != 0
        assert snapshot(tmp_path) == before
    dry = runner.invoke(app, argv + ['--dry-run'])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)['validated'] == 1
    assert 'synthetic private note' not in dry.output and ANCHOR not in dry.output
    assert snapshot(tmp_path) == before
    for invalid in [make('author', 200, 'synthetic', source_ref='backfill/no.jsonl#1', anchor=ANCHOR),
                    {'attestation_version': 2, 'type': 'identifier', 'at_ms': 200, 'note': 'synthetic',
                     'anchor': ANCHOR, 'identifier': '20001@lid', 'valid_from_ms': 20, 'valid_until_ms': 10},
                    make('name', 200, 'synthetic', anchor='99999@lid', name='synthetic')]:
        source.write_text(json.dumps(contact) + '\n' + json.dumps(invalid) + '\n')
        before = snapshot(tmp_path)
        result = runner.invoke(app, argv + ['--confirm'])
        assert result.exit_code != 0
        assert snapshot(tmp_path) == before
        assert ANCHOR not in result.output
    source.write_text(json.dumps(contact) + '\n')
    confirmed = runner.invoke(app, argv + ['--confirm'])
    assert confirmed.exit_code == 0, confirmed.output
    assert json.loads(confirmed.output)['committed'] == 1
    assert json.loads((root / 'owner/attestations.jsonl').read_text()) == contact
    # A valid target and an invalid later target cannot cause a partial owner package.
    raw = root / 'whatsapp/test.jsonl'
    raw.parent.mkdir()
    raw.write_text(json.dumps({'raw_archive_version': 1, 'kind': 'message', 'channel': 'whatsapp',
                              'chat_id': 'c1', 'received_ms': 100,
                              'native': {'type': 'message', 'payload': {'chatJid': 'c1', 'messageId': 'm1',
                                                                        'text': 'synthetic', 'senderPhoneJid': ANCHOR}}}) + '\n')
    good = make('author', 200, 'synthetic', source_ref='whatsapp/test.jsonl#1', anchor=ANCHOR)
    source.write_text(json.dumps(good) + '\n' + json.dumps({**good, 'source_ref': 'whatsapp/test.jsonl#2'}) + '\n')
    before = snapshot(tmp_path)
    assert runner.invoke(app, argv + ['--confirm']).exit_code != 0
    assert snapshot(tmp_path) == before
    source.write_text(json.dumps(good) + '\n' + json.dumps({**good, 'anchor': '99999@lid'}) + '\n')
    before = snapshot(tmp_path)
    assert runner.invoke(app, argv + ['--confirm']).exit_code != 0
    assert snapshot(tmp_path) == before
    source.write_text(json.dumps(good) + '\n')
    assert runner.invoke(app, argv + ['--dry-run']).exit_code == 0


def test_owner_package_counts_physical_refs_and_rejects_ambiguous_anchor(tmp_path, monkeypatch):
    import pytest
    from yeoman_gateway.history import attestations
    from yeoman_gateway.history.layer1 import Layer1Line

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    owner = root / 'owner/attestations.jsonl'
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps(make('contact', 100, 'synthetic', identifiers=[ANCHOR])) + '\n\n')
    refs = []
    real = attestations.parse

    def parse(line):
        if isinstance(line, Layer1Line):
            refs.append(line.ref)
        return real(line)

    monkeypatch.setattr(attestations, 'parse', parse)
    attestations.validate_owner_package(root, [make('name', 200, 'synthetic', anchor=ANCHOR, name='synthetic')])
    assert 'owner/attestations.jsonl#3' in refs
    other = '10002@s.whatsapp.net'
    alias = '20001@lid'
    owner.write_text('\n'.join(json.dumps(record) for record in [
        make('contact', 100, 'synthetic', identifiers=[ANCHOR]),
        make('contact', 100, 'synthetic', identifiers=[other]),
        make('identifier', 100, 'synthetic', anchor=ANCHOR, identifier=alias,
             valid_from_ms=0, valid_until_ms=100),
        make('identifier', 100, 'synthetic', anchor=other, identifier=alias,
             valid_from_ms=100, valid_until_ms=200)]) + '\n')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError):
        attestations.validate_owner_package(root, [make('name', 200, 'synthetic', anchor=alias, name='synthetic')])
    assert snapshot(tmp_path) == before


def test_attest_conflicting_known_authors_and_tombstone_targets_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    raw = root / 'whatsapp/test.jsonl'
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps({'raw_archive_version': 1, 'kind': 'message', 'channel': 'whatsapp',
                              'chat_id': 'c1', 'received_ms': 100,
                              'native': {'type': 'message', 'payload': {'chatJid': 'c1', 'messageId': 'm1',
                                                                        'text': 'synthetic'}}}) + '\n' +
                   '{"purged_version":1}\n')
    source = tmp_path / 'owner.jsonl'
    contacts = [make('contact', 100, 'synthetic', identifiers=[anchor])
                for anchor in [ANCHOR, '10002@s.whatsapp.net']]
    good = make('author', 200, 'synthetic', anchor=ANCHOR, source_ref='whatsapp/test.jsonl#1')
    for tail in [[good, {**good, 'anchor': '10002@s.whatsapp.net'}],
                 [good, {**good, 'source_ref': 'whatsapp/test.jsonl#2'}]]:
        source.write_text('\n'.join(json.dumps(row) for row in contacts + tail) + '\n')
        before = snapshot(tmp_path)
        result = runner.invoke(app, ['history', 'attest', '--file', str(source), '--confirm'])
        assert result.exit_code != 0 and snapshot(tmp_path) == before
        assert ANCHOR not in result.output and 'm1' not in result.output


def test_owner_package_publisher_envelope_agreement_before_any_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()  # Resolve the synthetic home before taking its no-mutation baseline.
    source = tmp_path / 'owner.jsonl'
    source.write_text(json.dumps(make('contact', 100, 'synthetic', identifiers=[ANCHOR])) + '\n' +
                      json.dumps({'type': 'name', 'at_ms': 200, 'anchor': ANCHOR, 'name': 'synthetic'}) + '\n')
    for mode in ['--dry-run', '--confirm']:
        before = set(tmp_path.rglob('*'))
        result = runner.invoke(app, ['history', 'attest', '--file', str(source), mode])
        assert result.exit_code != 0
        assert set(tmp_path.rglob('*')) == before and not root.exists()


def test_attest_refuses_existing_and_dangling_root_lock_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    root = raw_root()
    root.mkdir(parents=True)
    source = tmp_path / 'owner.jsonl'
    source.write_text(json.dumps(make('contact', 100, 'synthetic', identifiers=[ANCHOR])) + '\n')
    lock = root / '.purge-disposition.lock'
    for exists in [False, True]:
        target = tmp_path / f'foreign-{exists}'
        if exists:
            target.write_bytes(b'synthetic untouched')
        lock.symlink_to(target)
        before = snapshot(tmp_path)
        for mode in ['--dry-run', '--confirm']:
            result = runner.invoke(app, ['history', 'attest', '--file', str(source), mode])
            assert result.exit_code != 0
            assert snapshot(tmp_path) == before
            assert not (root / 'owner').exists()
            assert target.exists() == exists
        lock.unlink()


def test_cli_package_root_lock_contention_spans_validation_and_all_appends(tmp_path, monkeypatch):
    import select
    import time

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path / 'home'))
    source = tmp_path / 'owner.jsonl'
    source.write_text('\n'.join(json.dumps(record) for record in [
        make('contact', 100, 'synthetic', identifiers=[ANCHOR]),
        make('name', 200, 'synthetic', anchor=ANCHOR, name='synthetic')]) + '\n')
    script = r'''
import fcntl, os, sys
from pathlib import Path
from yeoman_shared.raw_archive import records
from yeoman_shared.raw_archive.paths import raw_root
from yeoman_gateway.history import attestations
from yeoman_gateway.cli.commands import app
root, tag, output = raw_root(), sys.argv[1], int(sys.argv[3])
def emit(message):
    os.write(output, (message + '\n').encode())
def held():
    fd = os.open(root / records.PURGE_DISPOSITION_LOCK, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise AssertionError('root lock was not held')
    finally:
        os.close(fd)
real_lock, real_validate, real_append = records.lock_file, attestations.validate_owner_package, records.append_owner_record_locked
validations = appends = 0
def lock(path, **kwargs):
    if path == root / records.PURGE_DISPOSITION_LOCK and tag == 'second':
        held()
        emit('contended')
    result = real_lock(path, **kwargs)
    if path == root / records.PURGE_DISPOSITION_LOCK:
        emit('locked')
    return result
def validate(*args):
    global validations
    validations += 1
    if validations == 2:
        held()
        emit('validated-under-lock')
    return real_validate(*args)
def append(*args):
    global appends
    held()
    result = real_append(*args)
    appends += 1
    emit('appended-under-lock')
    if tag == 'first' and appends == 1:
        assert sys.stdin.readline().strip() == 'release'
    return result
records.lock_file = lock
attestations.validate_owner_package = validate
records.append_owner_record_locked = append
app(args=['history', 'attest', '--file', sys.argv[2], '--confirm'], standalone_mode=False)
emit('complete')
'''
    children, readers = [], []
    env = {**os.environ, 'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}

    def start(tag):
        read_fd, write_fd = os.pipe()
        readers.append(read_fd)
        try:
            child = subprocess.Popen([sys.executable, '-c', script, tag, str(source), str(write_fd)],
                                     pass_fds=(write_fd,), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        finally:
            os.close(write_fd)
        children.append(child)
        return child, read_fd

    def event(fd):
        deadline = time.monotonic() + 45
        line = b''
        while not line.endswith(b'\n'):
            ready, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
            assert ready, 'bounded synchronization timed out'
            chunk = os.read(fd, 1)
            assert chunk, 'child exited before expected synchronization'
            line += chunk
        return line.decode().strip()

    try:
        first, one = start('first')
        assert [event(one) for _ in range(3)] == ['locked', 'validated-under-lock', 'appended-under-lock']
        second, two = start('second')
        assert event(two) == 'contended'  # LOCK_NB actually failed while first is paused.
        first.stdin.write(b'release\n')
        first.stdin.flush()
        assert [event(one) for _ in range(2)] == ['appended-under-lock', 'complete']
        assert [event(two) for _ in range(5)] == ['locked', 'validated-under-lock',
                                               'appended-under-lock', 'appended-under-lock', 'complete']
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, (stdout, stderr)
        lines = (raw_root() / 'owner/attestations.jsonl').read_text().splitlines()
        assert [json.loads(line)['type'] for line in lines] == ['contact', 'name', 'contact', 'name']
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        for fd in readers:
            os.close(fd)
