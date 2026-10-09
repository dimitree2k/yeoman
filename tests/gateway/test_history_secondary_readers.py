import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from history_turn_fixtures import Projector
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedReadContext
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)

from tests.gateway.test_history_source_compatibility import (
    AUTHOR,
    case,  # noqa: F401
)


def context(principal=AUTHOR):
    return TrustedReadContext(principal, 'whatsapp', 'g@g.us', frozenset({principal}), None, 1, 'reply', 150)


def test_history_export_requires_scope_and_defaults_to_aggregate(case):  # noqa: F811
    from yeoman_gateway.history.export import read_history_turn
    service, q, sources, _ = case
    with pytest.raises(HistoryPaused):
        read_history_turn(q.snapshot, context=context(), chat_ids=('g@g.us',), after_ms=0, limit=10)
    with service.history_scope(q, sources):
        result = read_history_turn(q.snapshot, context=context(), chat_ids=('g@g.us',), after_ms=0, limit=10)
        assert result['count'] == 1 and result['generation'] == 1
        assert 'Synthetic' not in json.dumps(result) and 'g@g.us' not in json.dumps(result)
        result = read_history_turn(q.snapshot, context=context(), chat_ids=('g@g.us',), after_ms=0, limit=10, aggregate=False)
        assert result['messages'][0]['text'] == 'Synthetic A'
        for bad in (replace(context(), principal_id='whatsapp:99999'), replace(context(), policy_revision=0)):
            with pytest.raises(KnowledgeError):
                read_history_turn(q.snapshot, context=bad, chat_ids=('g@g.us',), after_ms=0, limit=10)
        for args in ({'limit': 501}, {'limit': True}, {'after_ms': '0'}, {'chat_ids': ('other@g.us',)}, {'aggregate': 'false'}):
            with pytest.raises((ValueError, KnowledgeError)):
                read_history_turn(q.snapshot, context=context(), **({'chat_ids': ('g@g.us',), 'after_ms': 0, 'limit': 10} | args))
    assert not q.snapshot._closed


async def test_secondary_readers_use_history_and_seen_state_stays_operational(tmp_path):
    from yeoman_gateway.cron.types import CronPayload
    from yeoman_gateway.cron.voice import evaluate_voice_quiet_gate
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.export import secondary_archive
    p = Projector()
    seen = tmp_path / 'seen-chats.json'
    seen.write_text('{"chats":[]}')
    archive = Mock()
    archive.lookup_messages_in_range.side_effect = AssertionError('legacy')
    selected = SimpleNamespace(live_projection_enabled=True, readers=SimpleNamespace(secondary=True))
    payload = CronPayload(voice_wait_for_quiet=True)
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.history.export import SecondaryArchive
    tools = object.__new__(ConsciousnessTools)
    tools.config = SimpleNamespace(history=selected)
    tools._history_projector, tools._history_knowledge = p, None
    tools.inbound_archive = SecondaryArchive(archive, selected)
    tools._resolve_eligible = lambda chat_id, channel=None: SimpleNamespace(channel='whatsapp', chat_id=chat_id)
    tools._now = lambda: datetime.fromtimestamp(1800000000, UTC)
    tools._chat_window_since_for_trigger = lambda **kwargs: datetime.fromtimestamp(0, UTC)
    try:
        for count in (0, 1):
            if count:
                p.add('synthetic')
                p.generation += 1
            async with history_turn(p):
                rows = secondary_archive(archive, selected).lookup_messages_in_range('whatsapp', 'a@g.us', datetime.fromtimestamp(0, UTC), datetime.fromtimestamp(1800000000, UTC))
                assert len(rows) == count
                decision = evaluate_voice_quiet_gate(payload=payload, inbound_archive=secondary_archive(archive, selected), channel='whatsapp', chat_id='a@g.us', now=datetime.fromtimestamp(1700000010, UTC))
                assert decision.status == ('defer' if count else 'allowed')
            result = await tools.read_chat_window('a@g.us')
            assert len(result['messages']) == count
        assert seen.read_text() == '{"chats":[]}' and p.live_snapshot_count == 0
        p.status = 'rebuilding'
        with pytest.raises(HistoryPaused):
            async with history_turn(p):
                pass
    finally:
        p.close()


@pytest.mark.parametrize('cancel', [False, True])
async def test_tool_history_read_with_repair_queued_has_one_snapshot_and_no_lock_cycle(tmp_path, monkeypatch, cancel):
    from yeoman_gateway.agent.tools.history_read import HistoryReadTool
    from yeoman_gateway.agent.tools.shell import ExecTool
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.live import HistoryProjector
    from yeoman_gateway.history.project import project
    from yeoman_shared.config.schema import ExecIsolationConfig
    from yeoman_shared.raw_archive.writer import RawArchive, RawEvent
    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status')
    archive.append(RawEvent(channel='whatsapp', kind='message', direction='in', chat_id='g@g.us', native_id='M0',
        native={'messageId': 'M0', 'chatJid': 'g@g.us', 'participantJid': '10001@s.whatsapp.net',
                'text': 'Synthetic repair input', 'timestamp': 1700000000}, received_ms=1700000000000))
    project([root], db, publish_lineage_root=root)
    p = HistoryProjector(root, db, archive)
    await p.start()
    if p._startup_task is not None:
        await p._startup_task
    repair = None
    children = []
    real_spawn = asyncio.create_subprocess_shell
    async def spawn(*args, **kwargs):
        child = await real_spawn(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_shell', spawn)
    acquisitions = 0
    real_read = p.read_turn
    async def read():
        nonlocal acquisitions
        acquisitions += 1
        return await real_read()
    monkeypatch.setattr(p, 'read_turn', read)
    try:
        async with history_turn(p) as snapshot:
            token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='g@g.us', canonical_user_id=AUTHOR, history_snapshot=snapshot))
            try:
                repair = asyncio.create_task(p.rebuild(reason='synthetic queued repair'))
                async def locked():
                    while not p._operation_lock.locked() or p.health()['status'] != 'rebuilding':
                        await asyncio.sleep(0)
                await asyncio.wait_for(locked(), 30)
                result = await asyncio.wait_for(HistoryReadTool(None).execute(chat_ids=['g@g.us'], after_ms=0, limit=10), 30)
                assert 'paused' in result.lower() or 'scope' in result.lower()
                tool = ExecTool(allow_host_execution=True, isolation_config=ExecIsolationConfig(enabled=False))
                command = "/home/dm/Documents/yeoman/.venv/bin/python -c 'from yeoman_gateway.cli.conversation_history_commands import history_read; history_read(chat_ids=[\"g@g.us\"], after_ms=0, limit=10, content=False)'"
                task = asyncio.create_task(tool.execute(command=command))
                if cancel:
                    async def spawned():
                        while not children:
                            await asyncio.sleep(0)
                    await asyncio.wait_for(spawned(), 30)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await asyncio.wait_for(task, 30)
                    assert 'turn' in result.lower()
                assert acquisitions == 1 and not repair.done()
            finally:
                reset_tool_context(token)
        await asyncio.wait_for(repair, 30)
        assert not p._reader._snapshots and all(child.returncode is not None for child in children)
        assert 'YEOMAN_HISTORY_TOOL_TURN' not in os.environ
    finally:
        if repair is not None:
            await asyncio.wait_for(repair, 30)
        await p.stop()


def test_raw_seed_requires_isolated_explicit_inputs_and_destination(tmp_path, monkeypatch):
    from yeoman_gateway.storage.raw_seed import SeedPaths, seed_raw_archive
    from yeoman_shared.raw_archive.writer import RawArchive
    with pytest.raises(ValueError):
        SeedPaths.default()
    paths = SeedPaths(*(tmp_path / name for name in ('processing.db', 'reply.db', 'inbound', 'knowledge.db')))
    root = tmp_path / 'isolated-raw'
    with pytest.raises(ValueError):
        seed_raw_archive(root, replace(paths, processing_db=__import__('pathlib').Path('/home/dm/.yeoman/data/processing/processing.db')))
    assert not root.exists()
    RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status')
    paths.inbound_dir.mkdir()
    session = paths.inbound_dir / 'whatsapp_g@g.us.jsonl'
    session.write_text(json.dumps({'timestamp': '2020-01-01T00:00:00+00:00', 'role': 'user', 'content': 'Synthetic preserved text'}) + '\n')
    before = session.read_bytes()
    report = seed_raw_archive(root, paths, dry_run=True)
    assert report.dry_run and report.per_source['session_jsonl']['written'] == 1
    assert not list(root.glob('*/seed-*.jsonl'))
    report = seed_raw_archive(root, paths)
    assert report.per_source['session_jsonl']['written'] == 1 and session.read_bytes() == before
    assert 'Synthetic preserved text' in (root / 'whatsapp' / 'seed-session_jsonl.jsonl').read_text()


def test_short_reply_replay_refuses_runtime_defaults_and_protected_inputs(tmp_path, monkeypatch):
    from pathlib import Path

    from yeoman_gateway.short_reply.evaluation import load_replay_rows
    opened = Mock(side_effect=AssertionError('opened'))
    monkeypatch.setattr('yeoman_gateway.short_reply.evaluation._ro', opened)
    for protected in (Path('/home/dm/.yeoman/data/processing/processing.db'), tmp_path / 'alias'):
        if protected.name == 'alias':
            protected.symlink_to('/home/dm/.yeoman/data/processing/processing.db')
        with pytest.raises(ValueError):
            load_replay_rows(tmp_path / 'archive.db', protected, since_ts=0)
    opened.assert_not_called()
    monkeypatch.undo()
    from tests.gateway.test_short_reply_evaluation import _fixture
    archive, processing = _fixture(tmp_path)
    rows = load_replay_rows(archive, processing, since_ts=0)
    assert rows and all(row.chat_id == 'grp@g.us' for row in rows)


async def test_native_tool_uses_installed_scope_without_acquiring_or_closing(case):  # noqa: F811
    from yeoman_gateway.agent.tools.history_read import HistoryReadTool
    service, q, sources, db = case
    before = service._store.connection.total_changes
    token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='g@g.us', canonical_user_id=AUTHOR, history_snapshot=q.snapshot))
    try:
        with service.history_scope(q, sources):
            result = json.loads(await HistoryReadTool(service).execute(chat_ids=['g@g.us'], after_ms=0, limit=10, aggregate=False))
            assert result['messages'][0]['text'] == 'Synthetic A'
            assert not q.snapshot._closed and service._store.connection.total_changes == before
            assert 'rejected' in await HistoryReadTool(service).execute(chat_ids=['g@g.us'], after_ms=0, limit=10, sql='SELECT 1')
    finally:
        reset_tool_context(token)


async def test_standalone_export_owns_lease_and_disabled_ipc_creates_no_history(case, tmp_path):  # noqa: F811
    from contextlib import contextmanager

    from yeoman_gateway.history.export import read_history_export, request_history_read
    from yeoman_gateway.ipc.gateway_socket import GatewaySocket
    service, q, sources, db = case
    class Standalone:
        acquisitions = 0
        generation = 1
        def health(self):
            return {'status': 'ready', 'generation': self.generation}
        async def read_turn(self):
            self.acquisitions += 1
            return q.snapshot
    class Knowledge:
        history_source_ledger = sources.ledger
        _legacy_authority = sources.legacy_authority
        @contextmanager
        def history_scope(self, queries, authority):
            with service.history_scope(queries, authority):
                yield
    p = Standalone()
    p.history_knowledge = Knowledge()
    result = await read_history_export(p, context=context(), chat_ids=('g@g.us',), after_ms=0, limit=10)
    assert result['count'] == 1 and p.acquisitions == 1 and q.snapshot._closed
    server = GatewaySocket(tmp_path / 'g.sock')
    await server.start()
    try:
        assert (await request_history_read(server.path, dict(chat_ids=['g@g.us'], after_ms=0, limit=10)))['status'] == 'disabled'
        assert not list(tmp_path.rglob('history.db')) and not list(tmp_path.rglob('*.lock'))
    finally:
        await server.stop()


@pytest.mark.parametrize('isolated', [False, True])
async def test_exec_marker_is_child_only_and_cannot_enable_history(tmp_path, isolated):
    from yeoman_gateway.agent.tools.shell import ExecTool
    from yeoman_shared.config.schema import ExecIsolationConfig
    class Manager:
        commands = []
        async def execute(self, **kwargs):
            self.commands.append(kwargs['command'])
            child = await asyncio.create_subprocess_shell(kwargs['command'], stdout=asyncio.subprocess.PIPE)
            output, _ = await child.communicate()
            return SimpleNamespace(output=output.decode(), exit_code=child.returncode)
    tool = ExecTool(allow_host_execution=True, isolation_config=ExecIsolationConfig(enabled=False))
    manager = Manager()
    if isolated:
        tool._sandbox_manager = manager
    p = Projector()
    snapshot = await p.read_turn()
    token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='g@g.us', history_snapshot=snapshot))
    try:
        assert (await tool.execute(command='echo ${YEOMAN_HISTORY_TOOL_TURN:-absent}')).strip() == '1'
        assert 'YEOMAN_HISTORY_TOOL_TURN' not in os.environ
    finally:
        reset_tool_context(token)
        p.close()
    assert (await tool.execute(command='echo ${YEOMAN_HISTORY_TOOL_TURN:-absent}')).strip() == 'absent'
    if isolated:
        assert manager.commands[0].startswith('(export YEOMAN_HISTORY_TOOL_TURN=1;')
        assert not manager.commands[1].startswith('(export')


async def test_secondary_persona_and_policy_metadata_reopen_in_owned_scope(tmp_path):
    from contextlib import contextmanager
    from unittest.mock import AsyncMock

    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.persona_evolution import collect_persona_evolution_evidence
    from yeoman_gateway.policy.schema import PolicyConfig

    from tests.gateway.convhist.test_hist_queries import event
    p = Projector()
    config = SimpleNamespace(live_projection_enabled=True, readers=SimpleNamespace(secondary=True), legacy_writers_disabled=False)
    persona = 'personas/synthetic.md'
    (tmp_path / 'personas').mkdir()
    (tmp_path / persona).write_text('# Synthetic persona')
    policy = PolicyConfig.model_validate({'channels': {'whatsapp': {'chats': {'a@g.us': {'personaFile': persona}}}}})
    memory = SimpleNamespace(learned_chat_taste=lambda **kwargs: [], recent_chat_preferences=Mock(side_effect=AssertionError('legacy preferences')))
    log = SimpleNamespace(history=AsyncMock(return_value=[]))
    legacy = SimpleNamespace(lookup_messages_in_range=Mock(side_effect=AssertionError('legacy archive')))
    generations = []
    class Knowledge:
        history_source_ledger = SimpleNamespace()
        _legacy_authority = SimpleNamespace()
        @contextmanager
        def history_scope(self, queries, sources):
            assert queries.snapshot is current_history_snapshot()
            yield
        def owner_read_context(self, **kwargs):
            return object()
        def recall(self, *args, **kwargs):
            generations.append(current_history_snapshot().generation)
            return SimpleNamespace(entry_texts=(f'Synthetic preference {p.generation}',))
    adapter = object.__new__(EnginePolicyAdapter)
    adapter._history_config = config
    try:
        for generation in (1, 2):
            p.generation = generation
            p.add(f'm{generation}')
            event(p.db, f's{generation}', 'group_subject', generation, {'subject': f'Synthetic group {generation}'}, chat='a@g.us')
            p.db.commit()
            evidence = await collect_persona_evolution_evidence(policy=policy, workspace=tmp_path, persona_file=persona,
                memory=memory, speakup_log=log, inbound_archive=legacy, since=datetime.fromtimestamp(0, UTC),
                now=datetime.fromtimestamp(1800000000, UTC), history_projector=p, history_config=config, knowledge=Knowledge())
            assert evidence.chats[0].recent_message_count == generation
            assert evidence.chats[0].recent_preferences == [f'Synthetic preference {generation}']
            async with history_turn(p):
                assert adapter._get_group_name('a@g.us') == f'Synthetic group {generation}'
        assert generations == [1, 2] and p.live_snapshot_count == 0
        memory.recent_chat_preferences.assert_not_called()
        legacy.lookup_messages_in_range.assert_not_called()
    finally:
        p.close()


def test_frozen_export_is_readonly_and_closes_its_own_connection(case, tmp_path):  # noqa: F811
    import sqlite3

    from yeoman_gateway.history.export import read_history_frozen
    from yeoman_gateway.history.live import HistoryBoundary
    service, q, sources, db = case
    target = tmp_path / 'frozen.db'
    db.execute("INSERT INTO projector_state VALUES ('@runtime',0,0,'synthetic',4,?)", ('{"generation":1}',))
    db.commit()
    with sqlite3.connect(target) as copied:
        db.backup(copied)
    before = target.read_bytes()
    result = read_history_frozen(target, HistoryBoundary(1, ()), knowledge=service,
        context=context(), chat_ids=('g@g.us',), after_ms=0, limit=10, aggregate=False)
    assert result['status'] == 'frozen/non-live' and result['messages'][0]['text'] == 'Synthetic A'
    assert target.read_bytes() == before and not q.snapshot._closed


def test_raw_seed_cli_defaults_refuse_and_isolated_dry_run_creates_nothing(tmp_path):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.commands import app
    runner = CliRunner()
    assert runner.invoke(app, ['raw', 'seed']).exit_code != 0
    root = tmp_path / 'new-isolated-destination'
    args = ['raw', 'seed', '--processing-db', str(tmp_path / 'processing.db'), '--reply-context-db', str(tmp_path / 'reply.db'),
            '--inbound-dir', str(tmp_path / 'inbound'), '--knowledge-db', str(tmp_path / 'knowledge.db'), '--destination', str(root), '--dry-run']
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert not root.exists() and not list(tmp_path.glob('*-spool'))


@pytest.mark.parametrize('channel,chat,principal,owner,allowed', [
    ('system', 'g@g.us', AUTHOR, False, False),
    ('system', 'g@g.us', 'whatsapp:10002', False, False),
    ('whatsapp', '10001@s.whatsapp.net', 'whatsapp:10002', True, False),
    ('system', 'g@g.us', '', False, False),
    ('system', 'g@g.us', AUTHOR, True, False),
    ('whatsapp', 'g@g.us', AUTHOR, True, False),
    ('whatsapp', '10001@s.whatsapp.net', AUTHOR, True, True),
    ('cli', 'direct', AUTHOR, True, True),
    ('cli', 'direct', AUTHOR, False, True),
])
async def test_history_tool_owner_authority_requires_owner_only_recipients(case, monkeypatch, channel, chat, principal, owner, allowed):  # noqa: F811
    from yeoman_gateway.agent.tools.history_read import HistoryReadTool

    from tests.gateway.convhist.test_hist_queries import event, message
    service, q, sources, db = case
    monkeypatch.setattr(service._policy, 'admin_actor', lambda: AUTHOR)
    event(db, 'leave-before', 'member_remove', 50, {'participants': [['10002@s.whatsapp.net']]})
    event(db, 'join-after', 'member_add', 200, {'participants': [['10002@s.whatsapp.net']]})
    message(db, 'private', chat='other@g.us', text='Private synthetic')
    token = set_tool_context(ToolInvocationContext(channel=channel, chat_id=chat, canonical_user_id=principal, is_owner=owner, history_snapshot=q.snapshot))
    try:
        with service.history_scope(q, sources):
            result = json.loads(await HistoryReadTool(service).execute(chat_ids=['other@g.us'], after_ms=0, limit=10, aggregate=False))
            if allowed:
                assert result['messages'][0]['text'] == 'Private synthetic'
            else:
                assert result['status'] == 'rejected'
                local = json.loads(await HistoryReadTool(service).execute(chat_ids=['g@g.us'], after_ms=0, limit=10, aggregate=False))
                assert not local.get('messages')  # Current recipients include a member absent at source time.
    finally:
        reset_tool_context(token)


@pytest.mark.parametrize('channel,session,owner,origin,expected', [
    ('cli', 'cli:direct', False, '', AUTHOR),
    ('cli', 'cron:evolve', True, '', AUTHOR),
    ('cli', 'cron:untrusted', False, '', ''),
    ('system', 'background', False, AUTHOR, AUTHOR),
    ('system', 'background', True, '', ''),
])
def test_selected_direct_tool_context_preserves_trusted_origin(case, monkeypatch, channel, session, owner, origin, expected):  # noqa: F811
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.processing.tool_context import current_tool_context
    service, _, _, _ = case
    monkeypatch.setattr(service._policy, 'admin_actor', lambda: AUTHOR)
    responder = SimpleNamespace(tools=SimpleNamespace(get=lambda _: None), knowledge=service,
        _history_secondary_selected=True, _history_tools_selected=False,
        _trusted_turn_binding=lambda: SimpleNamespace(turn=SimpleNamespace(principal=origin)))
    token = set_tool_context(None)
    try:
        LLMResponder._set_tool_context(responder, channel=channel, chat_id='direct' if channel == 'cli' else 'g@g.us', session_key=session, is_owner=owner)
        assert current_tool_context().canonical_user_id == expected
    finally:
        reset_tool_context(token)
