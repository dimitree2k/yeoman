# ruff: noqa: F811
"""Shared synthetic integrated consumer fixtures (Task 11)."""
# Reuse Task 6's isolated v3 store, policy and deterministic extractor, not its tests.
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, OutboundEvent
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.context import (
    current_history_snapshot,
    history_knowledge_scope,
    history_turn,
)
from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
from yeoman_gateway.history.project import project
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import HistorySourceAlias
from yeoman_gateway.knowledge.models import (
    RecallQuery,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)
from yeoman_shared.raw_archive.records import append_line, append_owner_record, dumps
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

from tests.gateway.convhist.hist_fixtures import _raw, write_jsonl
from tests.gateway.history_turn_fixtures import Projector
from tests.gateway.test_history_capture_continuity import (
    GROUP,
    MS,
    PHONE,
    extract,
    outcomes,
    producer,
    statements,
    worker,
)
from tests.gateway.test_history_capture_continuity import (
    case as capture_case,  # noqa: F401
)

FAMILIES = ('participation', 'admission', 'reply_tools', 'knowledge_worker',
            'knowledge_identity_cache', 'secondary_consciousness_persona',
            'cli_overseer', 'a2a_recipient')


@pytest.fixture
async def reply_case(tmp_path, monkeypatch):
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.core.orchestrator import Orchestrator
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.pipeline.contacts import ContactsMiddleware
    from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
    from yeoman_gateway.providers.base import LLMProvider, LLMResponse
    from yeoman_gateway.session.manager import SessionManager

    class ReplyCase(Projector):
        def __init__(self):
            super().__init__()
            self.reply_snapshot_ids = []
            self.prompt_transcript = ''
            self.responder_acquisitions = 0
            self.legacy_history_reads = 0
            self.bus = SimpleNamespace(publish_outbound=AsyncMock(), publish_reaction=AsyncMock())

        async def run_voice_reply(self):
            self.add('voice', text='[Voice Message]')
            async with history_turn(self) as admission:
                assert HistoryQueries(admission).native_message(chat_id='a@g.us', native_id='voice')['media_json'] is None
            # This derived observation commits only after the admission lease closes.
            self.db.execute("UPDATE messages SET media_json=? WHERE message_id='voice'", ('{"transcript":{"text":"synthetic transcript"}}',))
            self.db.commit()
            self.add('prior', text='prior synthetic text')
            event = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender',
                content='synthetic transcript', message_id='voice', is_group=True)
            await self.service._process_message(event)
            assert self.bus.publish_outbound.call_count == 1
            assert self.bus.publish_outbound.call_args.args[0].metadata['history_generation'] == self.generation

    case = ReplyCase()
    sessions = SessionManager(tmp_path, sessions_dir=tmp_path / 'sessions', history_selected=True,
                              legacy_history_disabled=True)
    def legacy_read(*args, **kwargs):
        case.legacy_history_reads += 1
        raise AssertionError('legacy history read')
    monkeypatch.setattr(sessions, '_load', legacy_read)

    class Provider(LLMProvider):
        async def chat(self, **kwargs):
            from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
            from yeoman_gateway.processing.tool_context import current_tool_context
            context = current_tool_context()
            snapshot = current_history_snapshot()
            case.reply_snapshot_ids.append(id(context.history_snapshot))
            assert context.history_snapshot is snapshot
            assert 'synthetic transcript' in await RecallConversationTool(sessions).execute(query='synthetic')
            assert 'synthetic transcript' in str(kwargs['messages'])
            return LLMResponse(content='synthetic answer')

        def get_default_model(self):
            return 'synthetic/model'

    responder = LLMResponder(provider=Provider(), workspace=tmp_path, bus=MessageBus(), session_manager=sessions)
    responder._history_selected = True
    responder._history_tools_selected = True
    original = responder.context.build_messages
    def build_messages(**kwargs):
        case.prompt_transcript = kwargs['current_message']
        case.reply_snapshot_ids.append(id(current_history_snapshot()))
        return original(**kwargs)
    monkeypatch.setattr(responder.context, 'build_messages', build_messages)

    async def generate(ctx, next):
        before = case.acquisitions
        reply = await responder._generate(session_key='opaque', channel='whatsapp', chat_id=ctx.event.chat_id,
            content=ctx.event.content, sender_id=ctx.event.sender_id, media=(),
            metadata={'message_id': ctx.event.message_id}, allowed_tools=set(), persona_text=None)
        case.responder_acquisitions += case.acquisitions - before
        ctx.intents.append(SendOutboundIntent(event=OutboundEvent(channel='whatsapp', chat_id=ctx.event.chat_id, content=reply)))
        await next(ctx)

    orchestrator = object.__new__(Orchestrator)
    orchestrator._history_reply_selected = True
    orchestrator._pipeline = Pipeline([ContactsMiddleware(history_selected=True), ReplyContextMiddleware(history_selected=True), generate])
    case.service = OrchestratorService(bus=case.bus, orchestrator=orchestrator,
        typing_adapter=AsyncMock(), telemetry=SimpleNamespace(), memory=None,
        history_projector=case, history_selected=True)
    yield case
    await responder.aclose()
    case.close()


MEMBER_PHONE = '10002@s.whatsapp.net'
LATER_PHONE = '10003@s.whatsapp.net'


class IntegratedCase:
    """Real projection/leases/rebuild and service; only extractor/transport are fake."""
    initial_name = 'Synthetic original'
    author = 'whatsapp:10001'
    member = 'whatsapp:10002'
    later = 'whatsapp:10003'

    def __init__(self, root, capture):
        self.root = root
        self.knowledge = capture[1]
        self.policy = capture[2]
        self.open_service = capture[3]
        self.raw = root / 'raw'
        self.path = root / 'integrated-history.db'
        self.raw.mkdir(parents=True)
        self.native_path = self.raw / 'whatsapp/2026-10.jsonl'
        self.append('membership_snapshot', 'roster', participants=[PHONE, MEMBER_PHONE],
                    complete=True, ms=MS-10000)
        write_jsonl(self.raw / 'owner/attestations.jsonl', [
            make('name', MS-2000, 'synthetic name', anchor=PHONE, name=self.initial_name),
        ])
        project([self.raw], self.path, publish_lineage_root=self.raw)
        self.archive = RawArchive(self.raw, spool=root / 'spool', status_path=root / 'raw-status.json')
        self.projector = HistoryProjector(self.raw, self.path, self.archive)
        self.projector.history_knowledge = self.knowledge
        self.snapshots = []
        self.seen = []
        self.extractor = self.extract
        self.p = producer(self.knowledge)
        self.now = MS+1000
        self.effects = []
        self.observations = 0

    def append(self, kind, native_id, *, chat=GROUP, ms=MS, sender=PHONE, text=None, **payload):
        body = dict(chatJid=chat, messageId=native_id, senderId=sender, timestamp=ms, **payload)
        if text is not None:
            body['text'] = text
        row = _raw(kind, kind, body, received=ms)
        row['chat_id'] = chat
        append_line(self.native_path, dumps(row))

    async def start(self):
        await self.projector.start()
        await self.projector._startup_task
        assert self.projector.health()['status'] == 'ready'
        # Observe real acquisitions without replacing lease or connection behavior.
        real = self.projector._reader.open_snapshot
        def observe(boundary):
            snapshot = real(boundary)
            self.snapshots.append(snapshot)
            return snapshot
        self.projector._reader.open_snapshot = observe

    async def close(self):
        await self.projector.stop()
        self.knowledge.close()

    def assert_no_leases(self):
        assert not self.projector._reader._snapshots
        assert all(s._closed for s in self.snapshots)
        assert current_history_snapshot() is None
        assert all(item.snapshot._closed for item in getattr(self, 'recorded', ()))

    @contextmanager
    def snapshot(self):
        from yeoman_gateway.history.live import HistoryBoundary
        boundary = HistoryBoundary(self.projector.health()['generation'], self.projector._index._boundaries)
        snapshot = self.projector._reader.open_snapshot(boundary)
        try:
            yield snapshot
        finally:
            snapshot.close()

    async def settle(self):
        snapshot = await self.projector.read_turn()
        snapshot.close()

    async def add(self, native_id, *, chat=GROUP, ms=MS, known=True, text=None, **payload):
        self.append('message', native_id, chat=chat, ms=ms,
                    sender=PHONE if known else 'untyped-sender',
                    text=text or f'Synthetic source {native_id}', **payload)
        await self.settle()
        with self.snapshot() as snapshot:
            return HistoryQueries(snapshot).native_message(chat_id=chat, native_id=native_id)['message_id']

    async def prepare_empty(self):
        await self.settle()
        with self.snapshot() as snapshot:
            self.p.prepare_handover(snapshot, pending=(), processed=(), legacy_boundary=(123, 'legacy'))

    def extract(self, items):
        self.seen.extend(item.event_id for item in items)
        assert not self.projector._operation_lock.locked()
        return extract(items)

    async def work(self, offset=1000):
        return await worker(self, self.knowledge, self.p, self.extractor).run_due(now_ms=MS+offset)

    def reopen(self):
        self.knowledge.close()
        self.knowledge = self.open_service()
        self.p = producer(self.knowledge)
        self.projector.history_knowledge = self.knowledge

    def outcomes(self):
        return outcomes(self.knowledge)

    def statements(self):
        return set(statements(self.knowledge))

    def completed_sources(self):
        return {s['event_id'] for row in self.knowledge._store.query(
            "SELECT sources_json FROM knowledge_jobs WHERE state='done'")
            for s in json.loads(row['sources_json'])}

    async def continuity(self):
        self.historical = await self.add('historical')
        queued = await self.add('queued')
        pending = await self.add('raw-pending')
        with self.snapshot() as snapshot, self.p.scope(snapshot) as (_, authority):
            refs = tuple(authority.issue(mid) for mid in (self.historical, queued, pending))
            context = TrustedCaptureContext('legacy', self.knowledge.policy_revision,
                                            'observed_source_batch', (refs[1],))
            receipt = self.knowledge.enqueue_capture((refs[1],), context=context,
                         extractor_version='statement-capture-v1', ts_ms=MS)
            handover = self.p.prepare_handover(snapshot, pending=refs[1:], processed=refs[:1],
                                             legacy_boundary=(123, 'legacy'))
            assert self.p.prepare_handover(snapshot, pending=refs[1:], processed=refs[:1],
                                          legacy_boundary=(123, 'legacy')) == handover
        equal = await self.add('equal-time')
        self.projector._status = 'rebuilding'
        with pytest.raises(HistoryPaused):
            await self.work()
        # A late observation, timestamped before the handover, lands after its vector.
        self.append('message', 'late-pair', ms=MS-5000, text='Synthetic late pair')
        self.append('message', 'during-rebuild', text='Synthetic during rebuild')
        await self.projector.rebuild(reason='synthetic continuity')
        with self.snapshot() as snapshot:
            q = HistoryQueries(snapshot)
            late = q.native_message(chat_id=GROUP, native_id='late-pair')['message_id']
            during = q.native_message(chat_id=GROUP, native_id='during-rebuild')['message_id']
        for _ in range(3):
            await self.work()
        self.reopen()
        await self.work(offset=100000)
        assert self.knowledge._store.scalar('SELECT state FROM knowledge_jobs WHERE job_id=?',
                                          (receipt.job_id,)) == 'done'
        return {queued, pending, equal, late, during}

    async def starvation(self):
        # Exercise reserved discovery quota after the exact five-source witness.
        before = set(self.seen)
        for i in range(501):
            self.append('message', f'unknown-{i:04}', sender='untyped-sender', text='Synthetic unknown')
        other = 'z-other@g.us'
        self.append('membership_snapshot', 'other-roster', chat=other, ms=MS-1000,
                    participants=[PHONE], complete=True)
        from yeoman_gateway.policy.engine import PolicyEngine
        from yeoman_gateway.policy.schema import PolicyConfig
        self.policy.engine = PolicyEngine(PolicyConfig.model_validate({
            'defaults': {'whoCanTalk': {'mode': 'everyone'}},
            'channels': {'whatsapp': {'chats': {GROUP: {}, other: {}}}},
        }), self.root)
        self.policy.policy_revision += 1
        # Discover the new policy chat before its forward message arrives.
        await self.settle()
        for preparation in range(3):
            await self.work(offset=200000+preparation)
        pending = {mid for mid, outcome in self.outcomes().items() if outcome == 'pending'}
        assert len(pending) == 501
        mid = await self.add('eligible-other', chat=other)
        assert mid > max(pending)
        for pass_number in range(3):
            report = await self.work(offset=201000+pass_number)
            assert report.examined <= 500
            if self.outcomes().get(mid) == 'published':
                break
        assert self.outcomes()[mid] == 'published'
        assert set(self.seen)-before == {mid}
        unknowns = {r['message_id'] for r in self.knowledge._store.query(
            "SELECT message_id FROM knowledge_history_capture WHERE outcome='pending'")}
        assert len(unknowns) == 501
        assert (mid, f'Expected statement {mid}') in self.statements()


@pytest.fixture
async def continuity_case(tmp_path, capture_case):
    case = IntegratedCase(tmp_path / 'combined', capture_case)
    # Parent directory is synthetic, distinct from Task 6's databases.
    await case.start()
    try:
        yield case
    finally:
        await case.close()

class StatementCase(IntegratedCase):
    async def publish(self, *, old, author_only):
        self.chat = PHONE if author_only else GROUP
        self.author_only = author_only
        self.message_id = await self.add('statement-source', chat=self.chat)
        with self.snapshot() as snapshot, self.p.scope(snapshot) as (q, authority):
            self.original_contact = q.message(self.message_id)['sender_contact_id']
            current = authority.issue(self.message_id)
            if old:
                self.source = SourceRef('legacy-source', 7, 'whatsapp', self.chat, self.author, MS)
                alias = HistorySourceAlias(self.source, self.message_id, 7, self.original_contact,
                                            q.content_fingerprint(self.message_id), q.audience(self.message_id))
                self.knowledge.history_source_ledger.persist_aliases({self.source.key: alias})
            else:
                self.source = current
            self.p.prepare_handover(snapshot, pending=(self.source,), processed=(),
                                     legacy_boundary=(123, 'legacy'))
        await self.work()
        self.statement_id = self.knowledge._store.scalar('SELECT statement_id FROM knowledge_statements')
        assert self.statement_id

    def speaker(self):
        return self.knowledge._store.scalar("SELECT person_id FROM knowledge_statement_people WHERE role='speaker'")

    def audience(self):
        row = self.knowledge.history_source_ledger.alias(self.source.key)
        record = self.knowledge.history_source_ledger.lookup(*self.source.key)
        return (row.audience if row else record.audience).members

    async def curate(self):
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                context = TrustedCaptureContext('curation', self.knowledge.policy_revision,
                                                'native', (self.source,))
                receipt = self.knowledge.correct_statement(self.statement_id,
                    StatementCandidate('Curated synthetic statement', (self.source,)),
                    expected_source=self.source, context=context)
                self.statement_id = receipt.changed_ids[-1]
                self.authorized_context = self.read_context(self.author)
                self.selection = self.knowledge.recall(RecallQuery('Curated'), context=self.authorized_context)
                assert self.selection.statement_ids == (self.statement_id,)

    def curated_rows(self):
        return {table: [dict(r) for r in self.knowledge._store.query(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('knowledge_statements', 'knowledge_statement_audit', 'memory2_nodes')}

    def source_rows(self):
        return [dict(r) for r in self.knowledge._store.query('SELECT * FROM knowledge_statement_sources ORDER BY rowid')]

    async def redirect(self):
        from tests.gateway.convhist.hist_fixtures import _bf
        # Layer 1 promotes the generated ID to a frozen contact, then another earlier contact.
        # Projection flattens redirects; two real rebuilds exercise both terminal transitions.
        for cid, created in (('00000000-0000-4000-8000-000000000011', 20), ('00000000-0000-4000-8000-000000000012', 10)):
            records = [
                _bf('synthetic-contacts', 'contact_record', {'contactRef': cid, 'displayName': cid,
                    'createdMs': created}, provenance='derived_only', channel='any'),
                _bf('synthetic-identifiers', 'identifier_record', {'contactRef': cid,
                    'identifier': PHONE if cid == '00000000-0000-4000-8000-000000000011' else '10009@s.whatsapp.net',
                    'status': 'active'}, provenance='derived_only'),
            ]
            for record in records:
                append_line(self.raw / 'backfill/contacts.jsonl', dumps(record))
            if cid == '00000000-0000-4000-8000-000000000012':
                def merge(fd):
                    append_owner_record(self.raw, make('merge', MS+50, 'synthetic curated merge',
                        a=PHONE, b='10009@s.whatsapp.net'), projection_owner_fd=fd)
                await self.projector.rebuild(reason='synthetic redirect', mutation=merge)
            else:
                await self.projector.rebuild(reason='synthetic redirect')
            with self.snapshot() as snapshot:
                q = HistoryQueries(snapshot)
                self.terminal_contact = q.terminal(self.original_contact)
                assert self.terminal_contact == cid, (cid, self.terminal_contact)
        self.append('membership_change', 'later-member', ms=MS+100,
                    action='add', participants=[LATER_PHONE])
        await self.settle()

    def read_context(self, principal):
        return TrustedReadContext(principal, 'whatsapp', self.chat, frozenset({principal}),
            None, self.knowledge.policy_revision, 'reply', self.now, is_direct=self.author_only)

    async def recall(self, principal, *, person=None):
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                return self.knowledge.recall(RecallQuery('Curated', person_ids=(person,) if person else ()),
                                             context=self.read_context(principal)).statement_ids

    async def remove_member(self):
        self.append('membership_change', 'removed-member', ms=MS+200,
                    action='remove', participants=[MEMBER_PHONE])
        await self.settle()

    async def reuse_identifier(self):
        self.now = MS+4000
        def mutation(fd):
            # Explicit windows split the reused phone between two different native anchors.
            for record in (
                make('unmerge', MS+3000, 'synthetic separate reassigned holders',
                     a=PHONE, b='10009@s.whatsapp.net'),
                make('identifier', MS+3000, 'synthetic original holder', anchor='10009@s.whatsapp.net',
                     identifier=PHONE, valid_until_ms=MS+3000),
                make('identifier', MS+3000, 'synthetic new holder', anchor='10007@s.whatsapp.net',
                     identifier=PHONE, valid_from_ms=MS+3000),
            ):
                append_owner_record(self.raw, record, projection_owner_fd=fd)
        await self.projector.rebuild(reason='synthetic reassignment', mutation=mutation)
        with self.snapshot() as snapshot:
            q = HistoryQueries(snapshot)
            previous = q.resolve_identifier(PHONE, at_ms=MS, time_basis='native')
            current = q.resolve_identifier(PHONE, at_ms=self.now, time_basis='native')
            assert previous and current and previous != current

    async def dispose(self, disposition):
        if disposition == 'delete':
            self.append('delete', 'statement-source', chat=self.chat, ms=MS+5000)
            await self.settle()
        else:
            from yeoman_shared.raw_archive.purge import PurgeSelector, purge
            await self.projector.rebuild(reason='synthetic purge', mutation=lambda fd: purge(
                self.raw, PurgeSelector('whatsapp', chat_id=self.chat, native_id='statement-source'),
                operator='synthetic', projection_owner_fd=fd))

        with self.snapshot() as snapshot:
            assert HistoryQueries(snapshot).native_message(chat_id=self.chat, native_id='statement-source') is None

    async def revalidate(self):
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                return bool(self.knowledge.revalidate(self.selection,
                    context=self.read_context(self.author)).statement_ids)


@pytest.fixture
async def statement_case(tmp_path, capture_case):
    case = StatementCase(tmp_path / 'statement', capture_case)
    await case.start()
    try:
        yield case
    finally:
        await case.close()

class ConsumerCase(IntegratedCase):
    initial_name = 'synthetic-contact'
    async def initialize(self):
        self.message_id = await self.add('consumer-source', text='Synthetic original')
        with self.snapshot() as snapshot, self.p.scope(snapshot) as (_, authority):
            self.source = authority.issue(self.message_id)
            self.contact_id = self.knowledge.person_for_principal(self.author)
        self.runners = {}
        self.recorded = []
        original_open = self.projector._reader.open_snapshot
        def observe(boundary):
            snapshot = original_open(boundary)
            self.record(snapshot)
            return snapshot
        self.projector._reader.open_snapshot = observe
        original_worker = self.projector.worker_snapshot
        async def worker_snapshot(callback):
            def observed(snapshot):
                self.record(snapshot)
                return callback(snapshot)
            return await original_worker(observed)
        self.projector.worker_snapshot = worker_snapshot
        # Seed release controls in the synthetic v2 copy before the explicit v3 upgrade.
        from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
        from yeoman_gateway.knowledge.api import open_knowledge_store
        legacy_authority = self.knowledge._legacy_authority
        self.knowledge.close()
        source = self.root.parent / 'v2.db'
        import sqlite3
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO contacts(id,display_name,created_at,updated_at) VALUES (?,?,'date','date')",
                       (self.contact_id, self.initial_name))
            db.execute("INSERT INTO contact_aliases(contact_id,alias,source,first_seen,last_seen,status,address_allowed,normalized_alias)"
                       " VALUES (?,?,'owner_confirmed','date','date','confirmed',1,?)",
                       (self.contact_id, self.initial_name, self.initial_name))
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.close()
        target = self.root / 'alias-v3.db'
        upgrade_history_knowledge(source=source, target=target)
        self.open_service = lambda: open_knowledge_store(target, workspace_id='synthetic',
            source_authority=legacy_authority, policy_authority=self.policy, history_mode=True)
        self.knowledge = self.open_service()
        self.projector.history_knowledge = self.knowledge
        self.p = producer(self.knowledge)
        with self.snapshot() as snapshot, self.p.scope(snapshot) as (_, authority):
            self.source = authority.issue(self.message_id)
        with self.snapshot() as snapshot:
            self.p.prepare_handover(snapshot, pending=(self.source,), processed=(),
                                     legacy_boundary=(123, 'legacy'))

    def record(self, snapshot):
        q = HistoryQueries(snapshot)
        row = q.message(self.message_id)
        from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
        authority = HistoryKnowledgeSources(q, {}, self.knowledge.history_source_ledger,
                                             self.knowledge._legacy_authority)
        self.recorded.append(SimpleNamespace(snapshot=snapshot, connection=snapshot.connection,
            generation=snapshot.generation, text=row['current_text'] if row else None,
            name=q.contact(self.contact_id)['display_name'],
            members=q.members(chat_id=GROUP, at_ms=MS+1000).members,
            source_valid=authority.verify_source(self.source)))

    async def replace(self):
        def mutation(fd):
            self.append('edit', 'consumer-source', ms=MS+100, text='Synthetic edited')
            self.append('message', 'replacement-source', ms=MS+400, text='Synthetic edited')
            self.append('membership_snapshot', 'new-roster', ms=MS+200,
                        complete=True, participants=[PHONE, LATER_PHONE])
            append_owner_record(self.raw, make('name', MS+300, 'synthetic new name',
                anchor=PHONE, name='Synthetic new'), projection_owner_fd=fd)
        await self.projector.rebuild(reason='synthetic consumer replacement', mutation=mutation)

    async def run(self, family):
        begin = len(self.recorded)
        result = await getattr(self, 'run_' + family)()
        observed = self.recorded[begin:]
        assert observed, f'{family} never acquired a history lease'
        assert all(item.snapshot._closed for item in observed)
        value = observed[-1]
        value.result = result
        return value

    async def run_participation(self):
        from yeoman_gateway.processing.participation import ParticipationOpportunity
        from yeoman_gateway.processing.participation_context import ParticipationContextBuilder

        from tests.gateway.test_participation_context import _inputs
        builder = self.runners.setdefault('participation', ParticipationContextBuilder(
            archive=SimpleNamespace(lookup_messages_in_range=Mock(side_effect=AssertionError('legacy archive'))),
            policy=self.policy.engine, source_authorizer=lambda row: True))
        builder._history_selected = True
        opportunity = ParticipationOpportunity(opportunity_id='synthetic-opportunity', channel='whatsapp',
            chat_id=GROUP, trigger='inbound', source_event_ids=('consumer-source',),
            observed_revision=1, activation_epoch=1, created_at_ms=MS)
        async with history_turn(self.projector):
            return await builder.build(opportunity, inputs=_inputs(current_source_ids=('consumer-source',)),
                                       now_ms=MS+1000)

    async def run_admission(self):
        from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
        from yeoman_gateway.bus.queue import MessageBus
        from yeoman_gateway.channels.whatsapp import WhatsAppChannel
        from yeoman_gateway.media.storage import MediaStorage
        from yeoman_gateway.processing.policy import AdapterSnapshotProvider, IngestGate
        from yeoman_gateway.processing.store import ProcessingStore
        from yeoman_shared.config.schema import ProcessingConfig, WhatsAppConfig
        if 'admission' not in self.runners:
            bus = MessageBus()
            channel = WhatsAppChannel(WhatsAppConfig(), bus,
                media_storage=MediaStorage(self.root / 'incoming', self.root / 'outgoing'),
                legacy_history_disabled=True)
            channel._history_projector = self.projector
            channel._history_selected = True
            channel._history_knowledge = self.knowledge
            store = ProcessingStore(self.root / 'admission.db')
            adapter = EnginePolicyAdapter(engine=self.policy.engine, known_tools=set(),
                workspace=self.root, reload_on_change=False, processing_store=store)
            channel._processing_gate = IngestGate(config=ProcessingConfig(enabled=True), store=store,
                snapshots=AdapterSnapshotProvider(adapter), evaluate=lambda request: adapter.evaluate(request.event),
                clock=lambda: MS+1000)
            self.runners['admission'] = channel, bus, store
        channel, bus, _ = self.runners['admission']
        # New native intake IDs exercise admission on the same channel after replacement.
        event = channel._parse_inbound_event(dict(chatJid=GROUP,
            messageId=f'admission-probe-{self.projector.health()["generation"]}',
            senderId=PHONE, text='Synthetic admission', timestamp=MS//1000,
            replyToMessageId='consumer-source'))
        assert event is not None
        await channel._ingest_inbound_event(event)
        if bus.inbound_size == 0:
            return None
        message = await bus.consume_inbound()
        return message.content, message.metadata

    async def run_reply_tools(self):
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
        from yeoman_gateway.processing.tool_context import (
            ToolInvocationContext,
            reset_tool_context,
            set_tool_context,
        )
        from yeoman_gateway.session.manager import SessionManager
        from yeoman_gateway.session.operational import OperationalSessions
        if 'reply_tools' not in self.runners:
            sessions = SessionManager(self.root, sessions_dir=self.root / 'sessions', history_selected=True,
                legacy_history_disabled=True, operational_store=OperationalSessions(self.root / 'sessions.db'))
            self.runners['reply_tools'] = sessions, RecallConversationTool(sessions)
        sessions, tool = self.runners['reply_tools']
        async with history_turn(self.projector) as snapshot:
            token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=GROUP,
                                                           history_snapshot=snapshot))
            try:
                history = sessions.recent_history(channel='whatsapp', chat_id=GROUP, snapshot=snapshot, limit=50)
                return history, await tool.execute(query='Synthetic')
            finally:
                reset_tool_context(token)

    async def run_knowledge_worker(self):
        from yeoman_gateway.knowledge._capture_worker import StatementDraft
        if 'worker' not in self.runners:
            def model(items):
                return [StatementDraft(item.text, source_index=i) for i, item in enumerate(items)]
            self.runners['worker'] = worker(self, self.knowledge, self.p, model)
        await self.runners['worker'].run_due(now_ms=MS+1000)
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                context = TrustedReadContext(self.author, 'whatsapp', GROUP, frozenset({self.author}),
                    None, self.knowledge.policy_revision, 'reply', MS+1000)
                return self.knowledge.recall(RecallQuery('Synthetic'), context=context).entry_texts

    async def run_knowledge_identity_cache(self):
        await self.run_knowledge_worker()
        from yeoman_gateway.knowledge.models import Identifier
        async with history_turn(self.projector) as snapshot:
            with history_knowledge_scope(snapshot, self.knowledge):
                resolved = self.knowledge.resolve_identifier(Identifier('whatsapp', 'phone_jid', PHONE,
                                                                        namespace='whatsapp'), at_ms=MS)
                name = self.knowledge.display_name(resolved.person_id)
                context = TrustedReadContext(self.author, 'whatsapp', GROUP, frozenset({self.author}),
                                             None, self.knowledge.policy_revision, 'reply', MS+1000)
                recall = self.knowledge.recall(RecallQuery('Synthetic'), context=context)
                return name, resolved.person_id, recall.statement_ids

    async def run_secondary_consciousness_persona(self):
        from yeoman_gateway.consciousness.tools import ConsciousnessTools
        from yeoman_gateway.history.export import SecondaryArchive
        if 'secondary' not in self.runners:
            selected = SimpleNamespace(live_projection_enabled=True, readers=SimpleNamespace(secondary=True))
            tools = object.__new__(ConsciousnessTools)
            tools.config = SimpleNamespace(history=selected)
            tools._history_projector, tools._history_knowledge = self.projector, self.knowledge
            tools.inbound_archive = SecondaryArchive(Mock(), selected)
            tools._resolve_eligible = lambda chat_id, channel=None: SimpleNamespace(channel='whatsapp', chat_id=chat_id)
            tools._now = lambda: datetime.fromtimestamp((MS+1000)/1000, UTC)
            tools._chat_window_since_for_trigger = lambda **kwargs: datetime.fromtimestamp(0, UTC)
            self.runners['secondary'] = tools
        return await self.runners['secondary'].read_chat_window(GROUP)

    async def run_cli_overseer(self):
        import asyncio

        from typer.testing import CliRunner
        from yeoman_gateway.cli.conversation_history_commands import history_app
        from yeoman_gateway.history.export import read_history_export
        from yeoman_gateway.ipc.gateway_socket import GatewaySocket
        from yeoman_shared.config import loader
        if 'cli' not in self.runners:
            # The worker directory is short enough for AF_UNIX and serial within each worker.
            server = GatewaySocket(self.root.parent.parent / 't11-read.sock')
            async def read(args):
                context = TrustedReadContext(self.author, 'whatsapp', GROUP, frozenset({self.author}),
                    None, self.knowledge.policy_revision, 'reply', MS+1000)
                return await read_history_export(self.projector, context=context, **args)
            server.history_read_handler = read
            await server.start()
            self.runners['cli'] = server
        server = self.runners['cli']
        # Only the address comes from a fixture; CLI, peer check, IPC and export are real.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(loader, 'load_config', lambda: SimpleNamespace(
                ipc=SimpleNamespace(gateway_socket_path=str(server.path))))
            result = await asyncio.to_thread(CliRunner().invoke, history_app,
                ['read', '--chat', GROUP, '--after-ms', '0', '--limit', '10', '--content'])
        assert result.exit_code == 0, (result.output, result.exception)
        return json.loads(result.stdout)

    async def run_a2a_recipient(self):
        from yeoman_gateway.ipc.a2a_invoke import resolve_whatsapp_recipient

        from tests.gateway.test_a2a_invoke import _AliasPolicy, _input, _invoke
        policy = _AliasPolicy()
        policy.evaluate = lambda _: SimpleNamespace(accept_message=True, should_respond=True, allowed_tools={'message'})
        def resolver(kind, alias):
            return resolve_whatsapp_recipient(kind, alias, policy_adapter=policy, knowledge=self.knowledge)
        result, _, _, effects = await _invoke(input=_input(recipient={'type': 'contact', 'alias': self.initial_name}),
            policy=policy, resolver=resolver, history_projector=self.projector, history_knowledge=self.knowledge)
        self.effects.extend(effects.calls)
        return result

    async def run_judge(self):
        from tests.gateway.test_participation_integration import COMMENT, _opportunity, _runtime
        runtime, judge, builder, log = _runtime(self.root / 'judge', decision=COMMENT)
        runtime._history_projector = self.projector
        runtime._history_knowledge = self.knowledge
        try:
            result = await runtime.evaluate_participation(_opportunity())
            assert result == {'status': 'skipped', 'reason': 'history_paused'}
            assert judge.calls == builder.calls == runtime._submission.calls == 0
            assert runtime._reactor.calls == []
            raise HistoryPaused('judge paused')
        finally:
            log.close()

    async def run_reply(self):
        from yeoman_gateway.bus.queue import MessageBus
        from yeoman_gateway.core.orchestrator import Orchestrator
        from yeoman_gateway.core.pipeline import Pipeline
        calls = []
        async def provider(ctx, next):
            calls.append(ctx)
            raise AssertionError('model reached during pause')
        orchestrator = object.__new__(Orchestrator)
        orchestrator._history_reply_selected = True
        orchestrator._pipeline = Pipeline([provider])
        bus = MessageBus()
        service = OrchestratorService(bus=bus, orchestrator=orchestrator, typing_adapter=AsyncMock(),
            telemetry=SimpleNamespace(), memory=None, history_projector=self.projector, history_selected=True)
        await service._process_message(InboundEvent(channel='whatsapp', chat_id=GROUP,
            sender_id=PHONE, content='Synthetic pause reply'))
        assert calls == [] and bus.outbound_size == 0
        raise HistoryPaused('reply paused')

    async def queue_pending(self):
        with self.snapshot() as snapshot:
            self.p.run_due(snapshot, now_ms=MS+1000)
        assert self.knowledge._store.scalar("SELECT count(*) FROM knowledge_jobs WHERE state='queued'") > 0

    def pause(self, status):
        self.projector._status = status
        if status == 'backlog':
            from dataclasses import replace
            original = self.archive.status
            self.archive.status = lambda: replace(original(), spooled=1)

    async def run_paused(self, family):
        try:
            result = await getattr(self, 'run_' + family)()
        except HistoryPaused:
            return 'history_paused'
        if family == 'admission':
            assert result is None
            return 'history_paused'
        if family == 'cli_overseer':
            assert result == {'status': 'paused', 'code': 'HISTORY_READ_UNAVAILABLE'}
            return 'history_paused'
        if family == 'a2a_recipient':
            assert result['status'] == 'rejected'
            return 'rejected'
        raise AssertionError(f'{family} produced context during pause: {result}')

    async def accept_observation(self):
        assert self.archive.append_durable(RawEvent('whatsapp', 'message', 'in', {
            'type': 'message', 'payload': {'chatJid': GROUP, 'messageId': 'pause-observation',
            'senderId': PHONE, 'text': 'Synthetic accepted while paused', 'timestamp': MS+2000}},
            chat_id=GROUP, account='default', received_ms=MS+2000))
        self.observations += 1

    async def close(self):
        if 'cli' in self.runners:
            await self.runners['cli'].stop()
        if 'reply_tools' in self.runners:
            self.runners['reply_tools'][0].operational_store.close()
        if 'admission' in self.runners:
            self.runners['admission'][2].close()
        await super().close()


@pytest.fixture
async def consumer_case(tmp_path, capture_case):
    case = ConsumerCase(tmp_path / 'consumers', capture_case)
    await case.start()
    try:
        await case.initialize()
        yield case
    finally:
        await case.close()

class ParityCase(IntegratedCase):
    async def compare(self):
        from yeoman_gateway.adapters.reply_archive_history import (
            HistoryReplyArchiveAdapter,
            archive_row,
            history_text,
        )
        from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
        from yeoman_gateway.agent.tools.media_history import MediaHistoryTool
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
        from yeoman_gateway.media.document_cache import DocumentCache
        from yeoman_gateway.processing.tool_context import (
            ToolInvocationContext,
            reset_tool_context,
            set_tool_context,
        )
        from yeoman_gateway.session.manager import SessionManager
        from yeoman_gateway.session.operational import OperationalSessions
        from yeoman_gateway.storage.inbound_archive import InboundArchive
        self.checked, self.differences = [], []
        archive = InboundArchive(self.root / 'legacy-inbound.db', retention_days=None)
        legacy = SessionManager(self.root, sessions_dir=self.root / 'legacy-sessions')
        operational = OperationalSessions(self.root / 'operational.db')
        selected = SessionManager(self.root, sessions_dir=self.root / 'selected-sessions',
                                  history_selected=True, operational_store=operational)
        cache = DocumentCache(self.root / 'media.db')
        old_media = MediaHistoryTool(cache=cache, processor=None)
        new_media = MediaHistoryTool(cache=cache, processor=None)
        old_media.set_context('whatsapp', GROUP)
        new_media.set_context('whatsapp', GROUP)
        conversations = {GROUP: [('p1', 'Synthetic alpha'), ('p2', 'Synthetic literal %_needle'),
                                 ('p3', 'Synthetic image')],
                         PHONE: [('d1', 'Synthetic direct one'), ('d2', 'Synthetic direct two')]}
        try:
            for chat, pairs in conversations.items():
                session = legacy.get_or_create(f'whatsapp:{chat}')
                for i, (mid, text) in enumerate(pairs):
                    payload = {'media': {'kind': 'image'}} if mid == 'p3' else {}
                    await self.add(mid, chat=chat, ms=MS+i*1000, text=text, **payload)
                    archive.record_inbound(channel='whatsapp', chat_id=chat, message_id=mid,
                        participant=PHONE, sender_id=PHONE, sender_name='Synthetic original',
                        text=text, timestamp=(MS+i*1000)//1000)
                    session.add_message('user', text, message_id=mid, timestamp=MS+i*1000, sender_id=PHONE)
            cache.record_media_item(channel='whatsapp', chat_id=GROUP, message_id='p3',
                sender_id=PHONE, sender_name='Synthetic original', kind='image', mime_type='image/png',
                file_name='synthetic.png', local_path=self.root / 'synthetic.png', size_bytes=10,
                timestamp=(MS+2000)//1000)
            before, until = datetime.fromtimestamp((MS-1)/1000, UTC), datetime.fromtimestamp((MS+5000)/1000, UTC)
            old = SqliteReplyArchiveAdapter(archive)
            async with history_turn(self.projector) as snapshot:
                q = HistoryQueries(snapshot)
                new = HistoryReplyArchiveAdapter(q)
                def fields(rows):
                    return [{key: row[key] for key in ('message_id', 'text', 'sender_id', 'sender_name', 'timestamp')}
                            for row in rows]
                legacy_rows = archive.lookup_messages_in_range('whatsapp', GROUP, before, until)
                assert fields(new.lookup_messages_in_range('whatsapp', GROUP, before, until)) == fields(legacy_rows)
                self.checked.extend(('recent', 'ambient'))
                assert [x.message_id for x in new.lookup_messages_before('whatsapp', GROUP, 'p2', limit=10)] == [
                    x.message_id for x in old.lookup_messages_before('whatsapp', GROUP, 'p2', limit=10)]
                assert [r['native_message_id'] for r in q.reply_window(chat_id=GROUP, native_id='p2', before=1, after=1)] == [r['message_id'] for r in legacy_rows]
                self.checked.append('reply_before_after')
                for chat in (GROUP, PHONE):
                    expected = legacy.get_or_create(f'whatsapp:{chat}').get_history()
                    actual = selected.recent_history(channel='whatsapp', chat_id=chat, snapshot=snapshot, limit=50)
                    assert actual == expected, {'chat': chat, 'expected': expected, 'actual': actual}
                self.checked.append('dm')
                thread = selected.get_or_create('synthetic-thread', channel='whatsapp', chat_id=PHONE,
                                               thread_id='thread', history_snapshot=snapshot).get_history()
                assert len(thread) == 1 and thread[0]['role'] == 'system'
                for _, text in conversations[PHONE]:
                    assert f'user: {text}' in thread[0]['content']
                from yeoman_gateway.processing.responder import ThreadActorResponder
                wrapper = object.__new__(ThreadActorResponder)
                wrapper._inner = SimpleNamespace(sessions=legacy)
                wrapper._ensure_legacy_context(channel='whatsapp', chat_id=PHONE, session_key='synthetic-thread')
                assert thread == [{key: row[key] for key in ('role', 'content')}
                                  for row in legacy.get_or_create('synthetic-thread').get_history()]
                self.checked.append('thread')
                old_tool, new_tool = RecallConversationTool(legacy), RecallConversationTool(selected)
                old_tool.set_context('whatsapp', GROUP)
                new_tool.set_context('whatsapp', GROUP)
                old_text = await old_tool.execute(query='%_needle')
                token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=GROUP, history_snapshot=snapshot))
                try:
                    new_text = await new_tool.execute(query='%_needle')
                finally:
                    reset_tool_context(token)
                assert old_text == new_text
                self.checked.append('literal_search')
                assert archive_row(q, q.native_message(chat_id=GROUP, native_id='p1'))['sender_name'] == legacy_rows[0]['sender_name']
                self.checked.append('speaker')
                assert [r['native_message_id'] for r in q.media(chat_id=GROUP, limit=10)] == [
                    row['message_id'] for row in legacy_rows if row['text'] == 'Synthetic image']
                old_media_text = await old_media.execute()
                token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=GROUP, history_snapshot=snapshot))
                try:
                    assert await new_media.execute() == old_media_text
                finally:
                    reset_tool_context(token)
                self.checked.append('media')
            self.append('edit', 'p1', ms=MS+6000, text='Synthetic latest edited')
            self.append('delete', 'p2', ms=MS+7000)
            await self.add('passive', text='Synthetic passive', ms=MS+8000)
            self.archive.append_media_transcript({'kind': 'media_transcript', 'channel': 'whatsapp',
                'native_message_id': 'p3', 'chat_id': GROUP, 'text': 'Synthetic transcript', 'generated_ms': MS+9000})
            self.append('membership_change', 'later-roster', ms=MS+10000, action='add', participants=[LATER_PHONE])
            await self.settle()
            async with history_turn(self.projector) as snapshot:
                q = HistoryQueries(snapshot)
                new = HistoryReplyArchiveAdapter(q)
                assert new.lookup_message('whatsapp', GROUP, 'p1').text == 'Synthetic latest edited'
                assert old.lookup_message('whatsapp', GROUP, 'p1').text == 'Synthetic alpha'
                self.differences.append('latest_edit')
                assert new.lookup_message('whatsapp', GROUP, 'p2') is None
                assert old.lookup_message('whatsapp', GROUP, 'p2') is not None
                self.differences.append('delete_purge')
                row = q.native_message(chat_id=GROUP, native_id='p1')
                assert row['sender_contact_id'] != old.lookup_message('whatsapp', GROUP, 'p1').sender_id
                assert q.terminal(row['sender_contact_id']) == row['sender_contact_id']
                self.differences.append('typed_terminal_identity')
                assert q.audience(row['message_id']).members == frozenset({self.author, self.member})
                assert self.later in q.members(chat_id=GROUP, at_ms=MS+11000).members
                self.differences.append('evidence_audience')
                assert '[Derived transcript]: Synthetic transcript' in history_text(q.native_message(chat_id=GROUP, native_id='p3'))
                assert 'transcript' not in old.lookup_message('whatsapp', GROUP, 'p3').text
                self.differences.append('derived_label')
                assert new.lookup_message_any_chat('whatsapp', 'd1', preferred_chat_id=GROUP) is None
                assert old.lookup_message_any_chat('whatsapp', 'd1', preferred_chat_id=GROUP) is not None
                self.differences.append('authorized_cross_chat')
                assert new.lookup_message('whatsapp', GROUP, 'passive') is not None
                assert not any(r.get('message_id') == 'passive' for r in legacy.get_or_create(f'whatsapp:{GROUP}').messages)
                self.differences.append('passive')
                operational.set_boundary(channel='whatsapp', chat_id=GROUP, at_ms=MS+8000)
                legacy.get_or_create(f'whatsapp:{GROUP}').add_boundary()
                assert selected.recent_history(channel='whatsapp', chat_id=GROUP, snapshot=snapshot, limit=50) == []
                assert legacy.get_or_create(f'whatsapp:{GROUP}').get_history() == []
                assert q.search(chat_ids=(GROUP,), query='latest', limit=10)
                self.differences.append('new_boundary')
            failed = _raw('outbound_request', 'send_text', {'to': GROUP, 'text': 'Synthetic failed answer'},
                          direction='out', received=MS+12000, corr='failed')
            result = _raw('outbound_result', 'send_text', {},
                          direction='out', received=MS+12000, corr='failed')
            result['native']['result'] = {'ok': False, 'error': 'synthetic transport refusal'}
            for row in (failed, result):
                row['chat_id'] = GROUP
                append_line(self.native_path, dumps(row))
            session = legacy.get_or_create(f'whatsapp:{GROUP}')
            session.add_message('assistant', 'Synthetic failed answer')
            session.add_message('tool', 'Synthetic tool transcript')
            await self.settle()
            async with history_turn(self.projector) as snapshot:
                text = str(selected.recent_history(channel='whatsapp', chat_id=GROUP, snapshot=snapshot, limit=50))
                assert 'failed answer' not in text and 'tool transcript' not in text
                assert 'failed answer' in str(session.get_history())
                self.differences.append('failed_assistant_and_tools')
            from yeoman_shared.raw_archive.purge import PurgeSelector, purge
            await self.projector.rebuild(reason='synthetic parity purge', mutation=lambda fd: purge(
                self.raw, PurgeSelector('whatsapp', chat_id=GROUP, native_id='p3'), operator='synthetic', projection_owner_fd=fd))
            with self.snapshot() as snapshot:
                assert HistoryQueries(snapshot).media(chat_id=GROUP, limit=10) == []
                assert old.lookup_message('whatsapp', GROUP, 'p3') is not None
                token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=GROUP, history_snapshot=snapshot))
                try:
                    assert 'No cached media' in await new_media.execute()
                finally:
                    reset_tool_context(token)
                assert 'message_id=p3' in await old_media.execute()
        finally:
            archive.close()
            operational.close()


@pytest.fixture
async def parity_case(tmp_path, capture_case):
    case = ParityCase(tmp_path / 'parity', capture_case)
    await case.start()
    try:
        yield case
    finally:
        await case.close()


@pytest.fixture
def refusal_case(continuity_case):
    """A fresh function-scoped history/store, used only by the refusal witness."""
    return continuity_case
