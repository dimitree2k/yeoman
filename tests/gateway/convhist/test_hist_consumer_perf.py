# ruff: noqa: F811
"""Opt-in, isolated real-consumer measurements; reports contain only aggregates."""
# The same fixture-driven runner is used for synthetic and coordinator-owned copies.
import asyncio
import copy
import json
import logging
import math
import os
import re
import resource
import socket
import sqlite3
import threading
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.context import history_knowledge_scope, history_turn
from yeoman_gateway.history.export import require_isolated_paths
from yeoman_gateway.history.live import HistoryProjector
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.verify import verify_rebuild_candidate
from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.models import Identifier, RecallQuery, TrustedReadContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_shared.raw_archive.records import append_line, dumps
from yeoman_shared.raw_archive.writer import RawArchive

from tests.gateway.convhist import consumer_fixtures as consumers
from tests.gateway.convhist.consumer_fixtures import ConsumerCase
from tests.gateway.convhist.hist_fixtures import _bf, _raw, write_jsonl
from tests.gateway.test_history_capture_continuity import case as capture_case  # noqa: F401
from tests.gateway.test_history_capture_continuity import producer, worker

FAMILIES = ('admission', 'participation', 'reply_tools', 'knowledge', 'secondary')
DISTRIBUTION = dict.fromkeys(('count', 'p50', 'p95', 'p99', 'max'), 0)
PLAN = dict.fromkeys(('select_id', 'parent_id', 'scan', 'search', 'indexed', 'covering', 'temp_btree'), 0)


def distribution(values):
    ordered = sorted(values)
    return {'count': len(values), **{name: round(ordered[max(0, math.ceil(len(ordered)*q)-1)], 3)
            if ordered else 0 for name, q in (('p50', .5), ('p95', .95), ('p99', .99), ('max', 1))}}



_OPERATION = ContextVar('measured_history_operation', default=None)


class MeasuredOperationLock:
    """Test-local timing from acquisition through release, including cancellation."""
    def __init__(self, lock, holds, waits):
        self.lock, self.holds, self.waits = lock, holds, waits
        self.owners = {}

    async def __aenter__(self):
        task = asyncio.current_task()
        kind = _OPERATION.get()
        # Names support the small cancellation self-check without a projector.
        if kind is None and task is not None:
            kind = ('worker' if task.get_name().startswith('cutover-worker-') else
                    'reply' if task.get_name().startswith('cutover-reply-') else None)
        started = perf_counter()
        await self.lock.acquire()
        acquired = perf_counter()
        self.owners[task] = (kind, acquired)
        if kind == 'reply':
            self.waits.append((acquired-started)*1000)
        return self

    async def __aexit__(self, *exc):
        kind, acquired = self.owners.pop(asyncio.current_task())
        try:
            if kind == 'worker':
                self.holds.append((perf_counter()-acquired)*1000)
        finally:
            self.lock.release()

    def locked(self):
        return self.lock.locked()


def instrument_operation_lock(projector, holds, waits):
    projector._operation_lock = MeasuredOperationLock(projector._operation_lock, holds, waits)
    for name, kind in (('worker_snapshot', 'worker'), ('read_turn', 'reply'), ('barrier', 'reply')):
        original = getattr(projector, name)
        async def measured(*args, _original=original, _kind=kind, **kwargs):
            token = _OPERATION.set(_kind)
            try:
                return await _original(*args, **kwargs)
            finally:
                _OPERATION.reset(token)
        setattr(projector, name, measured)


async def measure_concurrent_reply(projector):
    entered, release = threading.Event(), threading.Event()
    def held(snapshot):
        entered.set()
        if not release.wait(30):
            raise AssertionError('worker measurement release missing')
        snapshot.assert_current(snapshot.generation)
    job = asyncio.create_task(projector.worker_snapshot(held))
    reply = None
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        reply = asyncio.create_task(projector.read_turn())
        await asyncio.sleep(0)
        assert not reply.done(), 'independent reply did not wait for worker lock'
        # Cancellation must keep the lock until the thread-owned lease closes.
        job.cancel()
        await asyncio.sleep(0)
        assert projector._operation_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        snapshot = await asyncio.wait_for(reply, 30)
        snapshot.close()
    finally:
        release.set()
        if not job.done():
            await asyncio.gather(job, return_exceptions=True)
        if reply is not None:
            results = await asyncio.gather(reply, return_exceptions=True)
            for result in results:
                if hasattr(result, 'close'):
                    result.close()

def empty_report():
    return {
        'counts': dict.fromkeys(('messages', 'ordinary', 'reconnect', 'reconnect_lines', 'warmup',
                                'statements', 'capture_backlog', 'reconnect_chats'), 0),
        'ordinary_ms': dict(DISTRIBUTION), 'reconnect_ms': dict(DISTRIBUTION),
        'raw_commit_to_context_ms': dict(DISTRIBUTION),
        'whole_reply_ms': dict(DISTRIBUTION),
        'worker_snapshot_lock_hold_ms': dict(DISTRIBUTION),
        'reply_barrier_wait_ms': dict(DISTRIBUTION),
        'families': {name: {'queries': 0, 'acquisition_ms': dict(DISTRIBUTION),
                            'path_ms': dict(DISTRIBUTION), 'query_plans': []} for name in FAMILIES},
        'storage': dict.fromkeys(('db_bytes', 'page_size', 'page_count'), 0),
        'resources': dict.fromkeys(('fd_start', 'fd_peak', 'fd_end', 'rss_peak_bytes'), 0),
        'leases': {'opened': 0, 'closed': 0},
        'lifecycle': dict.fromkeys(('build_ms', 'startup_ms', 'rebuild_ms', 'capture_recovery_ms', 'capture_recovery_passes',
                                  'pause_estimate_ms', 'prior_startup_ms', 'prior_rebuild_ms', 'rebuilds',
                                  'source_compatibility_ms', 'startup_to_capture_ready_ms', 'rebuild_to_capture_ready_ms'), 0),
        'checks': {'parity': False, 'integrity': False, 'capture_recovered': False},
    }


def validate_report(report):
    """Closed recursive schema: even nested IDs/bindings and arbitrary strings fail."""
    def check(value, template):
        if isinstance(template, dict):
            if not isinstance(value, dict) or set(value) != set(template):
                raise ValueError('measurement keys outside allowlist')
            for key in template:
                check(value[key], template[key])
        elif isinstance(template, list):
            if not isinstance(value, list):
                raise ValueError('measurement plans must be structural')
            for item in value:
                check(item, PLAN)
        elif isinstance(template, bool):
            if type(value) is not bool:
                raise ValueError('measurement check must be boolean')
        elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('measurement values must be finite nonnegative numbers')
    check(report, empty_report())


def sanitize_plan(plan):
    return re.sub(r'\b\d+(?:\.\d+)?\b', '?', re.sub(r"'(?:''|[^'])*'", '?', plan))


def structural_plan(row):
    # Keep the real SQLite plan topology/access strategy, never table/index names.
    detail = sanitize_plan(row[3]).upper()
    return dict(select_id=row[0], parent_id=row[1], scan=int('SCAN ' in detail),
                search=int('SEARCH ' in detail), indexed=int('INDEX' in detail),
                covering=int('COVERING' in detail), temp_btree=int('TEMP B-TREE' in detail))


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    from loguru import logger
    previous_logging = logging.root.manager.disable
    logger.disable('yeoman_gateway')
    logging.disable(logging.CRITICAL)
    def refused(*args, **kwargs):
        raise AssertionError('network forbidden in consumer measurement')
    monkeypatch.setattr(socket.socket, 'connect', refused)
    monkeypatch.setattr(socket.socket, 'connect_ex', refused)
    monkeypatch.setattr(socket, 'getaddrinfo', refused)
    yield
    logging.disable(previous_logging)
    logger.enable('yeoman_gateway')


class PerfCase(ConsumerCase):
    """Task 11 call sites with persistent real responder and no observation queries."""
    def __init__(self, root, raw, path, knowledge, policy, *, chat, sender, now):
        self.root, self.raw, self.path = root, raw, path
        self.knowledge, self.policy = knowledge, policy
        self.chat, self.sender, self.now = chat, sender, now
        self.author = 'whatsapp:' + sender.removesuffix('@s.whatsapp.net')
        self.native_path = raw / 'whatsapp/2099-01.jsonl'
        self.archive = RawArchive(raw, spool=root / 'spool', status_path=root / 'raw-status.json')
        self.projector = HistoryProjector(raw, path, self.archive)
        self.projector.history_knowledge = knowledge
        self.p = producer(knowledge)
        self.runners, self.recorded, self.snapshots = {}, [], []
        self.seen, self.effects = [], []
        self.turn_number = 0
        self.active_family = None
        self.report = empty_report()
        self.worker_holds, self.reply_waits = [], []
        instrument_operation_lock(self.projector, self.worker_holds, self.reply_waits)
        self.acquisitions = {name: [] for name in FAMILIES}
        self.family_times = {name: [] for name in FAMILIES}
        self.collect_plans = True
        self.plan_sql = {name: set() for name in FAMILIES}
        self.closed = False
        self.plan_errors = []

    async def initialize_perf(self, monkeypatch):
        self.append('message', 'consumer-source', chat=self.chat, sender=self.sender,
                    ms=self.now, text='Synthetic performance probe')
        await self.settle()
        with self.snapshot() as snapshot, self.p.scope(snapshot) as (q, authority):
            self.message_id = q.native_message(chat_id=self.chat, native_id='consumer-source')['message_id']
            self.source = authority.issue(self.message_id)
            self.contact_id = self.knowledge.person_for_principal(self.author)
            assert self.contact_id is not None
            groups = [self.chat] + [row[0] for row in snapshot.connection.execute(
                "SELECT DISTINCT chat_id FROM messages WHERE channel='whatsapp' AND chat_id LIKE '%@g.us'"
                " AND chat_id<>? ORDER BY chat_id LIMIT 17", (self.chat,))]
            if len(groups) < 2:
                raise ValueError('reconnect measurement requires copied groups')
            # Keep the recorded 18-entry burst shape; a copy with fewer groups cycles over them.
            self.burst_chats = [groups[i % len(groups)] for i in range(18)]
            self.burst_distinct_groups = len(groups)
            if self.p._state('handover') is None:
                if not self.synthetic:
                    raise ValueError('coordinator must supply a verified Knowledge handover copy')
                processed = []
                # Historical synthetic sources are explicitly processed, never re-extracted.
                with self.knowledge._store.transaction():
                    mids = snapshot.connection.execute(
                        'SELECT message_id FROM messages_current WHERE chat_id=? AND deleted=0', (self.chat,))
                    for (mid,) in mids:
                        if mid != self.message_id:
                            processed.append(authority.issue(mid))
                    self.p.prepare_handover(snapshot, pending=(self.source,), processed=tuple(processed),
                                             legacy_boundary=(0, ''))
        await self.recover_capture()
        await self.setup_responder()
        from yeoman_gateway.app.bootstrap import participant_is_allowed
        from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
        builder = ParticipationContextBuilder(archive=None, policy=self.policy.engine,
            source_authorizer=lambda row: participant_is_allowed(engine=self.policy.engine,
                channel=str(row.get('channel') or ''), chat_id=str(row.get('chat_id') or ''),
                sender=str(row.get('sender_id') or row.get('participant') or '')))
        builder._history_selected = True
        self.runners['participation'] = builder
        self.observe_acquisitions(monkeypatch)

    async def recover_capture(self, target=None):
        target = target or self.message_id
        started = perf_counter()
        # A real copy carries the handover's pending backlog ahead of the probe; drain it
        # (bounded) and report how many passes recovery took.
        passes = 0
        for passes in range(1, 401):
            await worker(self, self.knowledge, self.p, max_jobs=20).run_due(now_ms=self.now+100000)
            if self.outcomes().get(target) == 'published':
                break
        self.report['lifecycle']['capture_recovery_passes'] = self.report['lifecycle'].get('capture_recovery_passes', 0) + passes
        self.report['checks']['capture_recovered'] = self.outcomes().get(target) == 'published'
        assert self.report['checks']['capture_recovered']
        self.report['lifecycle']['capture_recovery_ms'] += (perf_counter()-started)*1000

    def observe_acquisitions(self, monkeypatch):
        original_open = self.projector._reader.open_snapshot
        original_read = self.projector.read_turn
        original_connect = sqlite3.connect
        uri = self.path.resolve().as_uri()+'?mode=ro'
        def connect(database, *args, **kwargs):
            connection = original_connect(database, *args, **kwargs)
            family = self.active_family
            if database == uri and family is not None:
                def trace(sql):
                    if not sql.lstrip().upper().startswith(('SELECT', 'WITH')):
                        return
                    metric = self.report['families'][family]
                    metric['queries'] += 1
                    normalized = sanitize_plan(sql)
                    if self.collect_plans and normalized not in self.plan_sql[family] and len(self.plan_sql[family]) < 16:
                        self.plan_sql[family].add(normalized)
                        try:
                            rows = connection.execute('EXPLAIN QUERY PLAN '+sql).fetchall()
                        except sqlite3.Error:
                            self.plan_errors.append(True)
                            return
                        metric['query_plans'].extend(structural_plan(row) for row in rows)
                # Attach before BEGIN/runtime pinning, so admission-only scopes count real SQL too.
                connection.set_trace_callback(trace)
            return connection
        monkeypatch.setattr(sqlite3, 'connect', connect)
        def opened(boundary):
            snapshot = original_open(boundary)
            self.snapshots.append(snapshot)
            self.report['leases']['opened'] += 1
            resources = self.report['resources']
            resources['fd_peak'] = max(resources['fd_peak'], len(os.listdir('/proc/self/fd')))
            return snapshot
        async def read():
            started = perf_counter()
            snapshot = await original_read()
            if self.active_family is not None:
                self.acquisitions[self.active_family].append((perf_counter()-started)*1000)
            return snapshot
        self.projector._reader.open_snapshot = opened
        self.projector.read_turn = read

    async def admission(self):
        if 'admission' not in self.runners:
            await self.run_admission()
        channel, bus, _ = self.runners['admission']
        event = channel._parse_inbound_event(dict(chatJid=self.chat,
            messageId=f'perf-admission-{self.turn_number}', senderId=self.sender,
            text='Synthetic performance probe', timestamp=self.now//1000,
            replyToMessageId='consumer-source'))
        assert event is not None
        await channel._ingest_inbound_event(event)
        assert bus.inbound_size > 0, 'measurement policy must admit the supplied sender/chat'
        await bus.consume_inbound()

    async def setup_responder(self):
        from yeoman_gateway.adapters.responder_llm import LLMResponder
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
        from yeoman_gateway.app.bootstrap import OrchestratorService
        from yeoman_gateway.bus.queue import MessageBus
        from yeoman_gateway.core.intents import SendOutboundIntent
        from yeoman_gateway.core.models import OutboundEvent
        from yeoman_gateway.core.orchestrator import Orchestrator
        from yeoman_gateway.core.pipeline import Pipeline
        from yeoman_gateway.pipeline.contacts import ContactsMiddleware
        from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
        from yeoman_gateway.processing.tool_context import current_tool_context
        from yeoman_gateway.providers.base import LLMProvider, LLMResponse
        from yeoman_gateway.session.manager import SessionManager
        from yeoman_gateway.session.operational import OperationalSessions

        sessions = SessionManager(self.root, sessions_dir=self.root / 'sessions', history_selected=True,
            legacy_history_disabled=True, operational_store=OperationalSessions(self.root / 'sessions.db'))
        tool = RecallConversationTool(sessions)
        class Provider(LLMProvider):
            async def chat(self, **kwargs):
                snapshot = current_tool_context().history_snapshot
                assert snapshot is not None
                assert 'Synthetic performance probe' in await tool.execute(query='Synthetic performance probe', limit=5)
                assert not snapshot._closed
                return LLMResponse(content='Synthetic answer')
            def get_default_model(self):
                return 'synthetic/model'
        responder = LLMResponder(provider=Provider(), workspace=self.root, bus=MessageBus(), session_manager=sessions)
        responder._history_selected = responder._history_tools_selected = True
        async def generate(ctx, next):
            answer = await responder._generate(session_key='perf', channel='whatsapp', chat_id=self.chat,
                content=ctx.event.content, sender_id=self.sender, media=(),
                metadata={'message_id': 'consumer-source'}, allowed_tools=set(), persona_text=None)
            ctx.intents.append(SendOutboundIntent(event=OutboundEvent(channel='whatsapp', chat_id=self.chat, content=answer)))
            await next(ctx)
        orchestrator = object.__new__(Orchestrator)
        orchestrator._history_reply_selected = True
        orchestrator._pipeline = Pipeline([ContactsMiddleware(history_selected=True), ReplyContextMiddleware(history_selected=True), generate])
        bus = SimpleNamespace(publish_outbound=AsyncMock(), publish_reaction=AsyncMock())
        service = OrchestratorService(bus=bus, orchestrator=orchestrator, typing_adapter=AsyncMock(),
            telemetry=SimpleNamespace(), memory=None, history_projector=self.projector, history_selected=True, history_knowledge=self.knowledge)
        self.responder, self.reply_service, self.reply_bus = responder, service, bus
        self.runners['reply_tools'] = sessions, tool

    async def reply(self):
        from yeoman_gateway.core.models import InboundEvent
        before = self.reply_bus.publish_outbound.call_count
        await self.reply_service._process_message(InboundEvent(channel='whatsapp', chat_id=self.chat,
            sender_id=self.sender, content='Synthetic performance probe', message_id='consumer-source', is_group=True))
        assert self.reply_bus.publish_outbound.call_count == before+1

    async def knowledge_reads(self):
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                scope = self.knowledge.history_source_ledger
                assert scope.lookup(*self.source.key) is not None
                from yeoman_gateway.knowledge._history_identity import current_history_scope
                assert current_history_scope().sources.verify_source(self.source)
                resolved = self.knowledge.resolve_identifier(Identifier('whatsapp', 'phone_jid', self.sender,
                    namespace='whatsapp'), at_ms=self.now)
                assert resolved.person_id == self.contact_id
                self.knowledge.display_name(resolved.person_id)
                context = TrustedReadContext(self.author, 'whatsapp', self.chat, frozenset({self.author}),
                    None, self.knowledge.policy_revision, 'reply', self.now+100000)
                self.knowledge.recall(RecallQuery('Synthetic'), context=context)

    def commit(self, reconnect=False):
        self.turn_number += 1
        token = f'perf-{self.turn_number}'
        if reconnect:
            for i, chat in enumerate(self.burst_chats):
                self.append('membership_snapshot', f'{token}-roster-{i}', chat=chat, sender=self.sender,
                    ms=self.now+self.turn_number, participants=[self.sender], complete=True)
                payload = dict(chatJid=chat, value='Synthetic subject', snapshot=True,
                               observedAtMs=self.now+self.turn_number)
                append_line(self.native_path, dumps(_raw('group_subject', 'group_subject', payload,
                    received=self.now+self.turn_number)))
            for i in range(5):
                payload = dict(chatJid=self.burst_chats[i], value='Synthetic description', snapshot=True,
                               observedAtMs=self.now+self.turn_number)
                append_line(self.native_path, dumps(_raw('group_description', 'group_description', payload,
                    received=self.now+self.turn_number)))
        for i in range(2 if reconnect else 1):
            self.append('message', f'{token}-message-{i}', chat=self.chat, sender=self.sender,
                ms=self.now+self.turn_number, text='Synthetic performance turn')
        return perf_counter()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if hasattr(self, 'responder'):
            await self.responder.aclose()
        await super().close()


async def measure_turns(case, *, parity=True):
    ordinary, reconnect, committed, whole_reply = [], [], [], []
    resources = case.report['resources']
    resources['fd_start'] = len(os.listdir('/proc/self/fd'))
    async def turn(burst, warmup=False):
        committed_at = case.commit(burst)
        start = perf_counter()
        for name, call in (('admission', case.admission), ('participation', case.run_participation),
                           ('reply_tools', case.reply), ('knowledge', case.knowledge_reads),
                           ('secondary', case.run_secondary_consciousness_persona)):
            case.active_family = name
            family_start = perf_counter()
            await call()
            if not warmup:
                case.family_times[name].append((perf_counter()-family_start)*1000)
                if name == 'reply_tools':
                    whole_reply.append((perf_counter()-start)*1000)
        elapsed = (perf_counter()-start)*1000
        case.active_family = None
        case.assert_no_leases()
        resources['fd_peak'] = max(resources['fd_peak'], len(os.listdir('/proc/self/fd')))
        if not warmup:
            (reconnect if burst else ordinary).append(elapsed)
            committed.append((perf_counter()-committed_at)*1000)
    for _ in range(5):
        await turn(False, warmup=True)
    case.collect_plans = False
    for values in case.acquisitions.values():
        values.clear()
    for value in case.report['families'].values():
        value['queries'] = 0
    for i in range(100):
        await turn(False)
        if i % 5 == 4:
            await turn(True)
    report = case.report
    await measure_concurrent_reply(case.projector)
    report['worker_snapshot_lock_hold_ms'] = distribution(case.worker_holds)
    report['reply_barrier_wait_ms'] = distribution(case.reply_waits)
    report['ordinary_ms'], report['reconnect_ms'] = distribution(ordinary), distribution(reconnect)
    report['raw_commit_to_context_ms'] = distribution(committed)
    report['whole_reply_ms'] = distribution(whole_reply)
    report['counts'].update(ordinary=len(ordinary), reconnect=len(reconnect), reconnect_lines=len(reconnect)*43, warmup=5, reconnect_chats=len(case.burst_chats))
    for name in FAMILIES:
        report['families'][name]['acquisition_ms'] = distribution(case.acquisitions[name])
        report['families'][name]['path_ms'] = distribution(case.family_times[name])
    report['leases']['closed'] = sum(snapshot._closed for snapshot in case.snapshots)
    async with history_turn(case.projector) as snapshot:
        db = snapshot.connection
        report['counts']['messages'] = db.execute('SELECT count(*) FROM messages').fetchone()[0]
        report['storage'].update(db_bytes=case.path.stat().st_size,
            page_size=db.execute('PRAGMA page_size').fetchone()[0], page_count=db.execute('PRAGMA page_count').fetchone()[0])
        report['checks']['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
        boundaries = snapshot.sources
    # Independent projection/parity runs outside all measured turns and read leases.
    if parity:
        await asyncio.to_thread(verify_rebuild_candidate, [case.raw], case.path, boundaries=boundaries)
        report['checks']['parity'] = True
    report['leases']['closed'] = sum(snapshot._closed for snapshot in case.snapshots)
    report['counts']['statements'] = case.knowledge._store.scalar('SELECT count(*) FROM knowledge_statements')
    report['counts']['capture_backlog'] = case.knowledge._store.scalar("SELECT count(*) FROM knowledge_history_capture WHERE outcome='pending'")
    resources['fd_end'] = len(os.listdir('/proc/self/fd'))
    resources['rss_peak_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    assert not case.plan_errors, 'consumer query plan unavailable'
    validate_report(report)
    return report


def isolated_inputs(root):
    """Fixed layout avoids path arguments that could bypass the copy-root boundary."""
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise ValueError('absolute isolated copy root required')
    require_isolated_paths(root)
    protected_home = Path('/home/dm/.yeoman')
    if root.resolve() == protected_home or protected_home in root.resolve().parents:
        raise ValueError('runtime copy root refused')
    paths = (root / 'raw', root / 'knowledge-copy.db', root / 'policy-copy.json', root / 'measurement')
    require_isolated_paths(*paths, root / 'measurement/consumer-performance.json')
    for path in (root, *root.parents, *root.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlinked measurement input refused')
    raw, knowledge, policy, destination = paths
    if not raw.is_dir() or not knowledge.is_file() or not policy.is_file():
        raise ValueError('explicit raw, Knowledge and policy copies required')
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError('empty measurement destination required')
    return SimpleNamespace(root=root, raw=raw, knowledge=knowledge, policy=policy, destination=destination)


def seed_raw(raw, count):
    rows = []
    for i in range(60):
        chat = consumers.GROUP if i == 0 else f'synthetic-{i}@g.us'
        sender = f'{10001+i}@s.whatsapp.net'
        row = _raw('membership_snapshot', 'membership_snapshot', dict(chatJid=chat,
            messageId=f'roster-{i}', timestamp=consumers.MS-10000,
            participants=[sender], complete=True), received=consumers.MS-10000)
        row['chat_id'] = chat
        rows.append(row)
    for i in range(count):
        chat = consumers.GROUP if i % 60 == 0 else f'synthetic-{i%60}@g.us'
        sender = f'{10001+i%60}@s.whatsapp.net'
        payload = dict(chatJid=chat, messageId=f'seed-{i}', senderId=sender,
                       timestamp=consumers.MS-5000+i, text='Synthetic historical text')
        if i % 100 == 0:
            payload['media'] = {'type': 'image', 'caption': 'Synthetic media'}
        row = _raw('message', 'message', payload, received=consumers.MS-5000+i)
        row['chat_id'] = chat
        rows.append(row)
        if i % 200 == 0:
            edit = _raw('edit', 'edit', dict(payload, text='Synthetic edited text'), received=consumers.MS+i)
            edit['chat_id'] = chat
            rows.append(edit)
        if i % 300 == 1:
            delete = _raw('delete', 'delete', dict(chatJid=chat, messageId=f'seed-{i}'), received=consumers.MS+i)
            delete['chat_id'] = chat
            rows.append(delete)
    write_jsonl(raw / 'whatsapp/2026-10.jsonl', rows)
    # Frozen contacts plus an owner merge exercise persisted redirect lookups.
    records = []
    for i, sender in enumerate((consumers.PHONE, '10009@s.whatsapp.net')):
        cid = f'00000000-0000-4000-8000-{i+1:012}'
        records.extend([
            _bf('synthetic-contacts', 'contact_record', {'contactRef': cid, 'displayName': 'Synthetic contact',
                'createdMs': i+1}, provenance='derived_only', channel='any'),
            _bf('synthetic-identifiers', 'identifier_record', {'contactRef': cid, 'identifier': sender,
                'status': 'active'}, provenance='derived_only'),
        ])
    write_jsonl(raw / 'backfill/contacts.jsonl', records)
    write_jsonl(raw / 'owner/attestations.jsonl', [make('merge', consumers.MS-8000,
        'synthetic merge', a=consumers.PHONE, b='10009@s.whatsapp.net')])


async def start_ready(projector):
    await projector.start()
    assert projector._startup_task is not None
    await projector._startup_task
    if projector._automatic_rebuild_task is not None:
        await projector._automatic_rebuild_task
    assert projector.health()['status'] == 'ready', 'isolated history startup failed'


async def prepare_case(inputs, monkeypatch, *, synthetic=False):
    inputs.destination.mkdir(parents=True, exist_ok=True)
    if synthetic:
        chat, sender, now = consumers.GROUP, consumers.PHONE, consumers.MS+60000
    else:
        # Supplied by the coordinator from the isolated copy; never written to evidence.
        chat = os.environ['YEOMAN_PERF_CHAT']
        sender = os.environ['YEOMAN_PERF_SENDER']
        now = int(os.environ['YEOMAN_PERF_NOW_MS'])
    policy = RuntimeKnowledgePolicy(PolicyEngine(PolicyConfig.model_validate_json(inputs.policy.read_text()), inputs.destination))
    with closing(sqlite3.connect(inputs.knowledge.as_uri()+'?mode=ro', uri=True)) as source:
        version = source.execute("SELECT value FROM knowledge_meta WHERE key='schema_version'").fetchone()[0]
        target = inputs.destination / 'knowledge-bench.db'
        if version == '3':
            with closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
    if version == '2':
        upgrade_history_knowledge(source=inputs.knowledge, target=target)
    elif version != '3':
        raise ValueError('verified Knowledge schema 2 or 3 required')
    knowledge = open_knowledge_store(target, workspace_id='synthetic-perf', source_authority=RuntimeKnowledgeSources(),
        policy_authority=policy, history_mode=True)
    case = PerfCase(inputs.destination, inputs.raw, inputs.destination / 'history.db', knowledge, policy,
                    chat=chat, sender=sender, now=now)
    case.synthetic = synthetic
    # Task 11 factories use these module-level fixture parameters. No source changes.
    monkeypatch.setattr(consumers, 'GROUP', chat)
    monkeypatch.setattr(consumers, 'PHONE', sender)
    monkeypatch.setattr(consumers, 'MS', now)
    try:
        start = perf_counter()
        await start_ready(case.projector)  # absent destination => real fenced schema-4 build
        case.report['lifecycle']['build_ms'] = (perf_counter()-start)*1000
        case.report['lifecycle']['rebuilds'] += 1
        await case.projector.stop()
        start = perf_counter()
        await start_ready(case.projector)  # cold service startup, OS cache deliberately retained
        case.report['lifecycle']['startup_ms'] = (perf_counter()-start)*1000
        compatibility_start = perf_counter()
        await case.initialize_perf(monkeypatch)
        initialized_ms = (perf_counter()-compatibility_start)*1000
        lifecycle = case.report['lifecycle']
        lifecycle['source_compatibility_ms'] = max(0, initialized_ms-lifecycle['capture_recovery_ms'])
        lifecycle['startup_to_capture_ready_ms'] = lifecycle['startup_ms']+initialized_ms
        # Append a real raw tail after independent candidate verification, before release.
        import yeoman_gateway.history.live as live
        real_verify = live.verify_rebuild_candidate
        def with_tail(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            case.append('message', 'perf-rebuild-tail', chat=chat, sender=sender,
                        ms=now+1, text='Synthetic rebuild tail')
            return result
        with monkeypatch.context() as patch:
            patch.setattr(live, 'verify_rebuild_candidate', with_tail)
            start = perf_counter()
            await case.projector.rebuild(reason='isolated consumer performance')
            case.report['lifecycle']['rebuild_ms'] = (perf_counter()-start)*1000
            case.report['lifecycle']['rebuilds'] += 1
        with case.snapshot() as snapshot:
            tail = HistoryQueries(snapshot).native_message(chat_id=chat, native_id='perf-rebuild-tail')['message_id']
        recovery_before = case.report['lifecycle']['capture_recovery_ms']
        await case.recover_capture(target=tail)
        case.report['lifecycle']['rebuild_to_capture_ready_ms'] = case.report['lifecycle']['rebuild_ms']+case.report['lifecycle']['capture_recovery_ms']-recovery_before
        with case.snapshot() as snapshot:
            assert HistoryQueries(snapshot).native_message(chat_id=chat, native_id='perf-rebuild-tail') is not None
            if synthetic:
                db = snapshot.connection
                assert db.execute('SELECT count(DISTINCT chat_id) FROM messages').fetchone()[0] >= 60
                assert db.execute('SELECT count(*) FROM contacts WHERE merged_into IS NOT NULL').fetchone()[0] > 0
                assert db.execute("SELECT count(*) FROM message_events WHERE kind='edit'").fetchone()[0] > 0
                assert db.execute('SELECT count(*) FROM messages_current WHERE deleted=1').fetchone()[0] > 0
                assert db.execute('SELECT count(*) FROM messages WHERE media_json IS NOT NULL').fetchone()[0] > 0
                assert case.knowledge._store.scalar('SELECT count(*) FROM knowledge_statements') > 0
        lifecycle = case.report['lifecycle']
        lifecycle.update(prior_startup_ms=40000, prior_rebuild_ms=85800,
            pause_estimate_ms=max(lifecycle['startup_to_capture_ready_ms'], lifecycle['rebuild_to_capture_ready_ms']))
        return case
    except BaseException:
        await case.close()
        raise


@pytest.fixture
async def perf_case(tmp_path, capture_case, monkeypatch):
    root = tmp_path / 'synthetic-copy'
    root.mkdir()
    raw = root / 'raw'
    seed_raw(raw, 30000)
    # Snapshot the already isolated v3 fixture, including its committed WAL.
    with closing(sqlite3.connect(root / 'knowledge-copy.db')) as destination:
        capture_case[1]._store._conn.backup(destination)
    (root / 'policy-copy.json').write_text(capture_case[2].engine.policy.model_dump_json(by_alias=True))
    case = await prepare_case(isolated_inputs(root), monkeypatch, synthetic=True)
    try:
        yield case
    finally:
        await case.close()


@pytest.fixture
async def copy_case(monkeypatch):
    root = os.environ.get('YEOMAN_PERF_COPY_ROOT')
    if not root:
        pytest.skip('coordinator opt-in requires YEOMAN_PERF_COPY_ROOT')
    try:
        case = await prepare_case(isolated_inputs(root), monkeypatch)
    except Exception:
        raise AssertionError('isolated_copy_preparation_failed') from None
    try:
        yield case
    finally:
        await case.close()


async def measure_copy(case, *, parity=True):
    report = await measure_turns(case, parity=parity)
    validate_report(report)
    # Write even a failed budget verdict for coordinator review; assertions follow.
    output = case.root / 'consumer-performance.json'
    require_isolated_paths(output)
    with output.open('x') as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
    return report


pytestmark = pytest.mark.perf


async def test_consumer_queries_do_not_full_resolve_per_turn(perf_case, monkeypatch):
    import yeoman_gateway.history.incremental as incremental
    import yeoman_gateway.history.live as live
    import yeoman_gateway.history.project as project_module
    import yeoman_gateway.history.resolve as resolve_module

    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary consumer ran full resolve/project/rebuild')

    real_resolve = incremental.resolve
    def bounded_resolve(identity_input):
        assert len(identity_input.sightings) < len(perf_case.projector._index._input.sightings)
        return real_resolve(identity_input)
    before_threads = set(threading.enumerate())
    with monkeypatch.context() as patch:
        patch.setattr(resolve_module, 'resolve', forbidden)
        patch.setattr(project_module, 'project', forbidden)
        patch.setattr(project_module, 'build_rows', forbidden)
        patch.setattr(project_module, 'resolve', forbidden)
        patch.setattr(incremental, 'build_rows', forbidden)
        patch.setattr(incremental, 'resolve', bounded_resolve)
        patch.setattr(live.HistoryProjector, 'rebuild', forbidden)
        report = await measure_copy(perf_case, parity=False)
    async with history_turn(perf_case.projector) as snapshot:
        boundaries = snapshot.sources
    await asyncio.to_thread(verify_rebuild_candidate, [perf_case.raw], perf_case.path, boundaries=boundaries)
    report['checks']['parity'] = True
    report['leases']['closed'] = sum(snapshot._closed for snapshot in perf_case.snapshots)
    (perf_case.root / 'consumer-performance.json').write_text(json.dumps(report, sort_keys=True, indent=2))
    validate_report(report)
    assert report['counts']['messages'] >= 30000
    assert report['counts']['ordinary'] >= 100
    assert report['counts']['reconnect'] >= 20
    assert report['counts']['reconnect_lines'] == 20 * 43
    assert report['counts']['reconnect_chats'] == 18
    assert all(report['families'][name]['query_plans'] for name in FAMILIES)
    assert report['ordinary_ms']['p95'] <= 1000
    assert report['reconnect_ms']['p95'] <= 3000
    assert set(report['families']) == set(FAMILIES)
    assert all(report['families'][name]['queries'] > 0 for name in FAMILIES)
    assert all(report['families'][name]['acquisition_ms']['count'] == 120 for name in FAMILIES)
    assert report['whole_reply_ms']['count'] == 120
    assert report['leases']['opened'] == report['leases']['closed'] > 0
    assert report['checks']['integrity'] and report['checks']['parity']
    perf_case.assert_no_leases()
    executor_threads = tuple(perf_case.projector._executor._threads)
    await perf_case.close()
    assert all(not thread.is_alive() for thread in executor_threads)
    assert set(threading.enumerate()) <= before_threads


def test_consumer_performance_measurement_is_content_free():
    report = empty_report()
    validate_report(report)
    for key, value in [('text', 'private body'), ('message_id', 'private-id'),
                       ('name', 'private name'), ('bindings', ['private query'])]:
        for location in ((), ('counts',), ('families', 'admission')):
            leaked = copy.deepcopy(report)
            target = leaked
            for part in location:
                target = target[part]
            target[key] = value
            with pytest.raises(ValueError):
                validate_report(leaked)
    for key in ('queries', 'query_plans'):
        leaked = copy.deepcopy(report)
        leaked['families']['admission'][key] = 'private identifier'
        with pytest.raises(ValueError):
            validate_report(leaked)
    plan = sanitize_plan("SEARCH messages USING INDEX idx (chat_id='private-id' AND sent_ms>12345)")
    assert 'private-id' not in plan and '12345' not in plan
    assert '?' in plan
    assert 'private' not in json.dumps(report)


async def test_isolated_copy_measurement_entrypoint(copy_case):
    try:
        report = await measure_copy(copy_case)
    except Exception:
        raise AssertionError('isolated_copy_measurement_failed') from None
    validate_report(report)
    assert report['ordinary_ms']['p95'] <= 1000
    assert report['reconnect_ms']['p95'] <= 3000



def test_measurement_rejects_protected_and_ambiguous_inputs(tmp_path, monkeypatch):
    for root in (Path('/home/dm/.yeoman/data/perf-copy'), Path('/home/dm/.yeoman/backups/perf-copy')):
        with pytest.raises(ValueError):
            isolated_inputs(root)
    for path in (Path('relative-copy'), tmp_path / 'absent-inputs'):
        with pytest.raises(ValueError):
            isolated_inputs(path)
    root = tmp_path / 'isolated'
    (root / 'raw').mkdir(parents=True)
    (root / 'knowledge-copy.db').touch()
    (root / 'policy-copy.json').write_text('{}')
    inputs = isolated_inputs(root)
    assert inputs.destination == root / 'measurement'
    inputs.destination.mkdir()
    (inputs.destination / 'history.db').touch()
    with pytest.raises(ValueError):
        isolated_inputs(root)
    (inputs.destination / 'history.db').unlink()
    (root / 'raw' / 'outside.jsonl').symlink_to(tmp_path / 'outside.jsonl')
    with pytest.raises(ValueError):
        isolated_inputs(root)
    for value in (float('nan'), float('inf'), -1, 'private', True):
        report = empty_report()
        report['counts']['messages'] = value
        with pytest.raises(ValueError):
            validate_report(report)
