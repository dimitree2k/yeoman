"""Owner-local bounded control uses the real protected writer and Unix transport."""
import hashlib
import json
import os
from contextlib import asynccontextmanager

import pytest
from test_hist_incremental import PN, T0
from test_hist_live import oracle_parity, projector_fixture, start_ready
from yeoman_gateway.history.attestations import make
from yeoman_gateway.ipc.gateway_socket import GatewaySocket


def package(tmp_path, min_bytes=0):
    path = tmp_path / 'package.jsonl'
    records = [make('name', T0, 'synthetic ' + 'x' * min_bytes, anchor=PN, name='Synthetic')]
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), records


@asynccontextmanager
async def server_fixture(tmp_path):
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    server = GatewaySocket(tmp_path / 'g.sock', history_control_handler=p.control, rate_limit=100)
    await server.start()
    try:
        yield root, db, p, server
    finally:
        await server.stop()
        await p.stop()


async def request(server, operation, **args):
    from yeoman_gateway.history.control import request_history_control
    return await request_history_control(server.path, operation, args)


@pytest.mark.asyncio
@pytest.mark.parametrize('min_bytes', [0, 65537])
async def test_owner_package_over_64k_uses_pinned_locator_one_fence(tmp_path, min_bytes):
    from yeoman_gateway.history.control import MAX_IPC_REQUEST_BYTES
    async with server_fixture(tmp_path) as (root, db, p, server):
        path, digest, records = package(tmp_path, min_bytes)
        generation = p.health()['generation']
        args = dict(confirm=True, package_path=str(path), package_sha256=digest)
        wire = json.dumps({'cmd': 'history_control', 'args': {'operation': 'attest', **args}}).encode() + b'\n'
        assert len(wire) < MAX_IPC_REQUEST_BYTES
        if min_bytes:
            assert path.stat().st_size > MAX_IPC_REQUEST_BYTES
        result = await request(server, 'attest', **args)
        assert result['status'] == 'ok', result
        assert result['committed'] == len(records)
        assert p.health()['generation'] == generation + 1
        assert (root / 'owner/attestations.jsonl').read_text().count('\n') == len(records)
        await oracle_parity(p, root, tmp_path)


@pytest.mark.asyncio
async def test_owner_package_one_fence_and_one_rebuild(tmp_path):
    async with server_fixture(tmp_path) as (root, db, p, server):
        path, _, records = package(tmp_path)
        records += [make('name', T0 + 1, 'synthetic second', anchor=PN, name='Second')]
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        generation = p.health()['generation']
        response = await request(server, 'attest', confirm=True, package_path=str(path), package_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        assert response['status'] == 'ok' and response['committed'] == 2
        assert p.health()['generation'] == generation + 1


@pytest.mark.asyncio
async def test_owner_package_digest_mismatch_makes_no_partial_append(tmp_path):
    async with server_fixture(tmp_path) as (root, db, p, server):
        path, digest, _ = package(tmp_path)
        path.write_bytes(path.read_bytes().replace(b'Synthetic', b'Synthetix'))
        before, generation = db.read_bytes(), p.health()['generation']
        response = await request(server, 'attest', confirm=True, package_path=str(path), package_sha256=digest)
        assert response == {'status': 'error', 'code': 'PACKAGE_DIGEST_MISMATCH'}
        assert not (root / 'owner/attestations.jsonl').exists()
        assert db.read_bytes() == before and p.health()['generation'] == generation
        assert (await request(server, 'status'))['status'] == 'ok'


def test_owner_package_limit_refuses_without_partial_append(tmp_path):
    from yeoman_gateway.history.control import MAX_OWNER_PACKAGE_BYTES, load_pinned_owner_package
    path, _, _ = package(tmp_path)
    data = path.read_bytes()
    path.write_bytes(data.rstrip(b'\n') + b' ' * (MAX_OWNER_PACKAGE_BYTES - len(data)) + b'\n')
    assert path.stat().st_size == MAX_OWNER_PACKAGE_BYTES
    assert len(load_pinned_owner_package(path, hashlib.sha256(path.read_bytes()).hexdigest())) == 1
    with path.open('ab') as out:
        out.write(b' ')
    with pytest.raises(ValueError, match='PACKAGE_TOO_LARGE'):
        load_pinned_owner_package(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'relative', 'digest'])
def test_owner_package_rejects_unsafe_locator(tmp_path, kind):
    from yeoman_gateway.history.control import load_pinned_owner_package
    path, digest, _ = package(tmp_path)
    if kind == 'symlink':
        link = tmp_path / 'link'
        link.symlink_to(path)
        path = link
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path)
    elif kind == 'relative':
        path = type(path)('relative')
    else:
        digest = digest.upper()
    with pytest.raises((ValueError, OSError)):
        load_pinned_owner_package(path, digest)


@pytest.mark.asyncio
async def test_gateway_history_control_rejects_non_owner_and_path_override(tmp_path, monkeypatch):
    async with server_fixture(tmp_path) as (root, db, p, server):
        generation = p.health()['generation']
        import asyncio
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(json.dumps({'cmd': 'history_control', 'args': {'operation': 'rebuild', 'confirm': True, 'db_path': str(tmp_path / 'override')}}).encode() + b'\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert response['status'] == 'error'
        monkeypatch.setattr(server, '_peer_is_owner', lambda writer: False)
        assert (await request(server, 'rebuild', confirm=True))['code'] == 'OWNER_REQUIRED'
        assert p.health()['generation'] == generation


@pytest.mark.asyncio
async def test_history_control_disabled_cannot_enable_projector(tmp_path):
    server = GatewaySocket(tmp_path / 'g.sock')
    await server.start()
    try:
        assert (await request(server, 'rebuild', confirm=True))['status'] == 'disabled'
        assert not list(tmp_path.rglob('*.db'))
        assert not list(tmp_path.rglob('*.lock'))
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_partial_import_keeps_history_fenced(tmp_path, monkeypatch):
    from hist_fixtures import _bf, write_jsonl
    from yeoman_gateway.history.convert.run import prepare_import_manifest
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_shared.raw_archive import records as raw_records
    async with server_fixture(tmp_path) as (root, db, p, server):
        staged = tmp_path / 'staged'
        write_jsonl(staged / 'backfill/synthetic.jsonl', [_bf('journal', 'message', {'text': 'synthetic', 'chat_id': 'synthetic@g.us'})])
        write_jsonl(staged / 'backfill/second.jsonl', [_bf('journal', 'message', {'text': 'synthetic second', 'messageId': 'SECOND', 'chat_id': 'synthetic@g.us'})])
        manifest = tmp_path / 'manifest.json'
        manifest.write_text(json.dumps(prepare_import_manifest(staged)))
        original = raw_records._publish_import_file
        calls = []
        def partial(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('synthetic partial import')
            return original(*args, **kwargs)
        monkeypatch.setattr(raw_records, '_publish_import_file', partial)
        response = await request(server, 'import-backfill', confirm=True, staged_path=str(staged), package_path=str(manifest), package_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        assert response['status'] == 'error'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        assert len(calls) == 2 and calls[0].exists()
        before = calls[0].read_bytes()
        repair = await request(server, 'rebuild', confirm=True)
        assert repair['status'] == 'error'
        monkeypatch.setattr(raw_records, '_publish_import_file', original)
        response = await request(server, 'import-backfill', confirm=True, staged_path=str(staged), package_path=str(manifest), package_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        assert response['status'] == 'ok'
        assert calls[0].read_bytes() == before
        await oracle_parity(p, root, tmp_path)


def test_cli_rebuild_uses_gateway_single_writer_control(monkeypatch):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app
    from yeoman_gateway.history import control
    calls = []
    async def fake(path, operation, args):
        calls.append((operation, args))
        return {'status': 'ok'}
    monkeypatch.setattr(control, 'request_history_control', fake)
    result = CliRunner().invoke(app, ['history', 'rebuild', '--confirm'])
    assert result.exit_code == 0, result.output
    assert calls == [('rebuild', {'confirm': True})]


def test_active_owner_mutation_socket_failure_never_falls_back(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app
    from yeoman_shared.raw_archive.records import acquire_projection_owner
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    root = tmp_path / 'data/raw'
    root.mkdir(parents=True)
    from hist_fixtures import write_jsonl
    from test_hist_incremental import observation
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [observation()])
    path, _, _ = package(tmp_path)
    fd = acquire_projection_owner(root)
    try:
        result = CliRunner().invoke(app, ['history', 'attest', '--file', str(path), '--confirm'])
        assert result.exit_code != 0
        assert not (root / 'owner/attestations.jsonl').exists()
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_disconnected_cli_does_not_cancel_accepted_mutation(tmp_path, monkeypatch):
    import asyncio
    import threading
    async with server_fixture(tmp_path) as (root, db, p, server):
        path, digest, _ = package(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = p._build_replace_release
        def blocked():
            entered.set()
            assert release.wait(10)
            return original()
        monkeypatch.setattr(p, '_build_replace_release', blocked)
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(json.dumps({'cmd': 'history_control', 'args': {'operation': 'attest', 'confirm': True, 'package_path': str(path), 'package_sha256': digest}}).encode() + b'\n')
        await writer.drain()
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert p.health()['status'] == 'rebuilding'
            writer.close()
            await writer.wait_closed()
            release.set()
            async with asyncio.timeout(10):
                while p.health()['status'] == 'rebuilding':
                    await asyncio.sleep(.02)
            assert p.health()['status'] == 'ready'
            assert (root / 'owner/attestations.jsonl').read_text().count('\n') == 1
            await oracle_parity(p, root, tmp_path)
        finally:
            release.set()
            writer.close()


def test_cli_rebuild_confirmation_and_offline_project_ownership(tmp_path, monkeypatch):
    from test_hist_live import fixture
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app
    from yeoman_gateway.history.live import acquire_history_writer
    runner = CliRunner()
    assert runner.invoke(app, ['history', 'rebuild']).exit_code != 0
    root, db, archive = fixture(tmp_path)
    before = db.read_bytes()
    fd = acquire_history_writer(db)
    try:
        result = runner.invoke(app, ['history', 'project', '--layer1', str(root), '--db', str(db)])
        assert result.exit_code != 0 and 'history rebuild' in result.output
        assert db.read_bytes() == before
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_cli_active_attest_routes_one_locator_over_real_socket(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app
    from yeoman_shared.config import loader
    from yeoman_shared.raw_archive import paths
    async with server_fixture(tmp_path) as (root, db, p, server):
        monkeypatch.setattr(paths, 'raw_root', lambda: root)
        monkeypatch.setattr(loader, 'load_config', lambda: SimpleNamespace(ipc=SimpleNamespace(gateway_socket_path=str(server.path))))
        path, _, _ = package(tmp_path, 65537)
        generation = p.health()['generation']
        result = await asyncio.to_thread(CliRunner().invoke, app, ['history', 'attest', '--file', str(path), '--confirm'])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)['committed'] == 1
        assert p.health()['generation'] == generation + 1


@pytest.mark.asyncio
async def test_control_purge_uses_owner_fence(tmp_path):
    async with server_fixture(tmp_path) as (root, db, p, server):
        generation = p.health()['generation']
        result = await request(server, 'purge', confirm=True, selector={'channel': 'whatsapp', 'chat_id': None, 'native_id': 'M0', 'before_ms': None})
        assert result['status'] == 'ok', result
        assert p.health()['generation'] == generation + 1
        snapshot = await p.read_turn()
        try:
            assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone() == (0,)
        finally:
            snapshot.close()


@pytest.mark.asyncio
async def test_control_direct_disabled_and_confirmation_are_write_free(tmp_path):
    root, db, archive, p = projector_fixture(tmp_path)
    before = db.read_bytes()
    assert (await p.control('rebuild', {'confirm': True}))['status'] == 'disabled'
    assert (await p.control('rebuild', {'confirm': False}))['status'] == 'error'
    assert db.read_bytes() == before
    assert not db.with_name(db.name + '-wal').exists()


@pytest.mark.asyncio
async def test_control_package_preflight_does_not_block_intake_or_status(tmp_path, monkeypatch):
    import asyncio
    import threading

    from yeoman_gateway.history import attestations
    async with server_fixture(tmp_path) as (root, db, p, server):
        path, digest, _ = package(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = attestations.validate_owner_package
        def block(root, records):
            entered.set()
            assert release.wait(3), 'package preflight blocked event loop'
            return original(root, records)
        monkeypatch.setattr(attestations, 'validate_owner_package', block)
        task = asyncio.create_task(request(server, 'attest', confirm=True, package_path=str(path), package_sha256=digest))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            from test_hist_incremental import T0, G, observation
            from yeoman_shared.raw_archive.writer import RawEvent
            assert p.archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', chat_id=G, account='default', native=observation(native_id='INTAKE')['native'], received_ms=T0))
            assert (await request(server, 'status'))['status'] == 'ok'
            release.set()
            assert (await task)['status'] == 'ok'
        finally:
            release.set()
            await task
