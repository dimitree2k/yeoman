import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from history_turn_fixtures import FileProjector, Projector
from test_a2a_invoke import _AliasPolicy, _input, _invoke
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.ipc.a2a_invoke import resolve_whatsapp_recipient
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


async def test_concurrent_tool_turns_keep_snapshot_and_chat_separate():
    from yeoman_gateway.history.context import history_turn
    p = Projector()
    p.add('a', text='synthetic alpha')
    p.add('b', chat='b@g.us', text='synthetic beta')
    tool = RecallConversationTool(SimpleNamespace(get_or_create=Mock(side_effect=AssertionError('legacy'))))
    both = asyncio.Event()
    entered = 0
    async def run(chat):
        nonlocal entered
        async with history_turn(p) as snapshot:
            token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=chat, history_snapshot=snapshot))
            entered += 1
            if entered == 2:
                both.set()
            try:
                await asyncio.wait_for(both.wait(), 30)
                return id(snapshot), await tool.execute(query='synthetic')
            finally:
                reset_tool_context(token)
    a, b = await asyncio.gather(run('a@g.us'), run('b@g.us'))
    assert a[0] != b[0]
    assert 'alpha' in a[1] and 'beta' not in a[1]
    assert 'beta' in b[1] and 'alpha' not in b[1]
    assert p.live_snapshot_count == 0
    p.close()


def _person_lookup_projector():
    import json

    from yeoman_gateway.history.schema import FTS_ROWS

    p = Projector()
    owner_phone = '491000000001@s.whatsapp.net'
    person_phone = '491000000002@s.whatsapp.net'
    p.db.execute("INSERT INTO contacts VALUES ('owner','person','owner','Owner','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO contacts VALUES ('alex','person',NULL,'Alexandra','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO contacts VALUES ('alex-alias','person',NULL,'Alex','confirmed','alex','[]')")
    for contact, phone in (('owner', owner_phone), ('alex', person_phone)):
        p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                      " VALUES (?,'whatsapp','pn_jid',?,'strong','owner_attested','[]')", (contact, phone))
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                  " VALUES ('alex-alias','whatsapp','push_name','Lex','weak','observed','[]')")
    p.db.execute("INSERT INTO message_events(event_id,kind,channel,chat_id,occurred_ms,time_certainty,"
                  "payload_json,provenance,actor_basis,source_refs) VALUES "
                  "('members','member_snapshot','whatsapp','family@g.us',1700000000000,'native',?,'native','unknown','[]')",
                  # Alex has left; historical authored messages remain resolvable because
                  # the owner is still a proven current member of this authorized chat.
                  (json.dumps({'complete': True, 'participants': [[owner_phone]]}),))
    p.add('quoted', chat='family@g.us', text="Alex' Aktien wurden erwähnt.")
    p.db.execute("UPDATE messages SET sender_contact_id='owner',sender_basis='native_identifier',sender_identifier=? WHERE message_id='quoted'", (owner_phone,))
    p.add('authored', chat='family@g.us', text='synthetic author note')
    p.db.execute("UPDATE messages SET sender_contact_id='alex',sender_basis='native_identifier',sender_identifier=? WHERE message_id='authored'", (person_phone,))
    p.db.execute('DELETE FROM messages_fts')
    p.db.execute('INSERT INTO messages_fts(message_id,chat_id,text) ' + FTS_ROWS)
    p.db.commit()
    return p, owner_phone


def _owner_test_chat(p, phone, form):
    aliases = {'lid': '990000000001@lid', 'second_phone': '491000000009@s.whatsapp.net'}
    for kind, value in (('lid', aliases['lid']), ('pn_jid', aliases['second_phone'])):
        p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                     " VALUES ('owner','whatsapp',?,?,'strong','owner_attested','[]')", (kind, value))
    p.db.commit()
    return phone if form == 'phone' else aliases[form]


OWNER_PRINCIPAL_FORMS = (
    'whatsapp:491000000001', 'whatsapp:491000000001@s.whatsapp.net',
    'whatsapp:990000000001@lid', '491000000001', '990000000001',
    '491000000001@s.whatsapp.net', '990000000001@lid',
)


@pytest.mark.parametrize('principal', OWNER_PRINCIPAL_FORMS)
@pytest.mark.parametrize('chat,is_owner,channel,allowed', [
    ('990000000001@lid', True, 'whatsapp', True),
    ('491000000009@s.whatsapp.net', True, 'whatsapp', True),
    ('990000000002@lid', True, 'whatsapp', False),
    ('family@g.us', True, 'whatsapp', False),
    ('990000000099@lid', True, 'whatsapp', False),
    ('990000000001@lid', False, 'whatsapp', False),
    ('990000000001@lid', True, 'telegram', False),
    ('990000000001@newsletter', True, 'whatsapp', False),
    ('status@broadcast', True, 'whatsapp', False),
])
async def test_owner_direct_chat_uses_temporal_terminal_identity(principal, chat, is_owner, channel, allowed):
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, phone = _person_lookup_projector()
    _owner_test_chat(p, phone, 'phone')
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                 " VALUES ('alex','whatsapp','lid','990000000002@lid','strong','owner_attested','[]')")
    p.db.commit()
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        assert queries.is_owner_direct_chat(principal, chat, 1700000000100,
                                           is_owner=is_owner, channel=channel) is allowed
        if allowed:
            assert queries.owner_member_chat_ids(principal, 1700000000100) == ('family@g.us',)
    p.close()


@pytest.mark.parametrize('principal', OWNER_PRINCIPAL_FORMS)
@pytest.mark.parametrize('chat,is_owner,allowed', [
    ('990000000001@lid', True, True),
    ('491000000009@s.whatsapp.net', True, True),
    ('990000000002@lid', True, False),
    ('family@g.us', True, False),
    ('990000000099@lid', True, False),
    ('990000000001@lid', False, False),
])
async def test_recall_owner_alias_direct_turns(principal, chat, is_owner, allowed):
    from yeoman_gateway.history.context import history_turn

    p, phone = _person_lookup_projector()
    _owner_test_chat(p, phone, 'phone')
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                 " VALUES ('alex','whatsapp','lid','990000000002@lid','strong','owner_attested','[]')")
    p.db.commit()
    tool = RecallConversationTool(None)
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=chat,
            canonical_user_id=principal, is_owner=is_owner, history_snapshot=snapshot))
        try:
            text = await tool.execute(person='Alex', query='Aktien Portfolio')
            if allowed:
                assert "Alex' Aktien" in text
            else:
                assert 'only in the owner' in text
        finally:
            reset_tool_context(token)
    p.close()


async def test_owner_direct_chat_refuses_expired_or_ambiguous_identifiers():
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, phone = _person_lookup_projector()
    lid = _owner_test_chat(p, phone, 'lid')
    p.db.execute("UPDATE identifier_history SET valid_until_ms=1700000000200 WHERE value=?", (lid,))
    p.db.commit()
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        assert queries.is_owner_direct_chat('whatsapp:491000000001', lid, 1700000000100, is_owner=True)
        assert not queries.is_owner_direct_chat('whatsapp:491000000001', lid, 1700000000300, is_owner=True)
        snapshot.connection.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                                    " VALUES ('alex','whatsapp','lid',?,'strong','owner_attested','[]')", (lid,))
        assert not queries.is_owner_direct_chat('whatsapp:491000000001', lid, 1700000000100, is_owner=True)
    p.close()


@pytest.mark.parametrize('principal', ['491000000001', 'whatsapp:491000000001'])
@pytest.mark.parametrize('lid_contact', ['owner', 'alex'])
async def test_bare_principal_requires_one_terminal_contact(principal, lid_contact):
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, phone = _person_lookup_projector()
    chat = _owner_test_chat(p, phone, 'lid')
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                 " VALUES (?,'whatsapp','lid','491000000001@lid','strong','owner_attested','[]')", (lid_contact,))
    p.db.commit()
    async with history_turn(p) as snapshot:
        q = HistoryQueries(snapshot)
        assert q.principal_contact(principal, 1700000000100) == ('owner' if lid_contact == 'owner' else None)
        assert q.is_owner_direct_chat(principal, chat, 1700000000100, is_owner=True) is (lid_contact == 'owner')
        assert q.is_owner_direct_chat(phone, chat, 1700000000100, is_owner=True)
    p.close()


def _add_named_author(p, contact_id, display_name, phone):
    p.db.execute("INSERT INTO contacts VALUES (?, 'person',NULL,?,'confirmed',NULL,'[]')",
                 (contact_id, display_name))
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                 " VALUES (?,'whatsapp','pn_jid',?,'strong','owner_attested','[]')", (contact_id, phone))
    message_id = f'named-{contact_id}'
    p.add(message_id, chat='family@g.us', text=f'{display_name} synthetic history')
    p.db.execute("UPDATE messages SET sender_contact_id=?,sender_basis='native_identifier',sender_identifier=?"
                 " WHERE message_id=?", (contact_id, phone, message_id))
    p.db.execute("INSERT INTO message_events(event_id,kind,channel,chat_id,occurred_ms,time_certainty,"
                 "payload_json,provenance,actor_basis,source_refs) VALUES (?,?, 'whatsapp', ?,1700000000000,"
                 "'native',?,'native','unknown','[]')",
                 (f'subject-{contact_id}', 'group_subject', 'family@g.us',
                  __import__('json').dumps({'subject': 'Family'})))
    p.db.execute('DELETE FROM messages_fts')
    p.db.execute('INSERT INTO messages_fts(message_id,chat_id,text) ' + __import__(
        'yeoman_gateway.history.schema', fromlist=['FTS_ROWS']).FTS_ROWS)
    p.db.commit()


def test_person_name_normalization_splits_digits_and_drops_symbols():
    from yeoman_gateway.history.queries import _name_tokens

    assert _name_tokens('𝐀𝐋𝐄𝐗42😈') == ('alex', '42')
    assert _name_tokens('ＡＬＥＸ—Mu\u0308ller!!') == ('alex', 'müller')
    assert _name_tokens('😈!!!') == ()


async def test_person_resolver_prefix_ambiguity_exact_multword_and_unmatchable_names():
    import time

    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, _ = _person_lookup_projector()
    p.db.execute("UPDATE contacts SET display_name='Zed' WHERE contact_id IN ('alex','alex-alias')")
    p.db.commit()
    _add_named_author(p, 'alexgr', 'AlexGrrrrr😈', '491000000005@s.whatsapp.net')
    async with history_turn(p) as snapshot:
        result = HistoryQueries(snapshot).resolve_person('Alex', ('family@g.us',), time.time_ns() // 1_000_000)
        assert result.status == 'resolved' and result.person_id == 'alexgr'
    # Prefixes can be ambiguous while a distinctive multiword prefix still resolves.
    _add_named_author(p, 'alexander', 'Alexander Müller', '491000000003@s.whatsapp.net')
    _add_named_author(p, 'alexa', 'Alexa Weber', '491000000004@s.whatsapp.net')
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        now_ms = time.time_ns() // 1_000_000
        result = queries.resolve_person('Alex', ('family@g.us',), now_ms)
        assert result.status == 'ambiguous'
        assert {candidate.display_name for candidate in result.candidates} >= {
            'Alexander Müller', 'Alexa Weber', 'AlexGrrrrr😈'}
        assert all(candidate.group_names == ('Family',) for candidate in result.candidates)
        assert queries.resolve_person('Alex Müller', ('family@g.us',), now_ms).person_id == 'alexander'
        assert queries.resolve_person('Alexander Müller', ('family@g.us',), now_ms).person_id == 'alexander'
        assert queries.resolve_person('Alp', ('family@g.us',), now_ms).status == 'not_found'
        assert queries.resolve_person('😈', ('family@g.us',), now_ms).status == 'not_found'
        assert queries.resolve_person('!!!', ('family@g.us',), now_ms).status == 'not_found'
    _add_named_author(p, 'alex-mueller', 'Alex Müller', '491000000006@s.whatsapp.net')
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        now_ms = time.time_ns() // 1_000_000
        assert queries.resolve_person('Alex Müller', ('family@g.us',), now_ms).person_id == 'alex-mueller'
        assert queries.resolve_person('Al', ('family@g.us',), now_ms).status == 'not_found'
    _add_named_author(p, 'al', 'Al', '491000000007@s.whatsapp.net')
    async with history_turn(p) as snapshot:
        result = HistoryQueries(snapshot).resolve_person('Al', ('family@g.us',), time.time_ns() // 1_000_000)
        assert result.status == 'resolved' and result.person_id == 'al'
    p.close()


@pytest.mark.parametrize('owner_chat', ['phone', 'lid', 'second_phone'])
async def test_recall_person_scope_requires_owner_dm_and_current_membership(owner_chat):
    import time

    from yeoman_gateway.history.context import history_turn

    p, owner_phone = _person_lookup_projector()
    owner_phone = _owner_test_chat(p, owner_phone, owner_chat)
    tool = RecallConversationTool(None, group_resolver=lambda ref: (
        {'family': 'family@g.us', 'other': 'other@g.us'}.get(ref), None))
    async with history_turn(p) as snapshot:
        from yeoman_gateway.history.queries import HistoryQueries
        resolution = HistoryQueries(snapshot).resolve_person(
            'Lex', ('family@g.us',), time.time_ns() // 1_000_000)
        assert resolution.status == 'resolved' and resolution.person_id == 'alex'
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            result = await tool.execute(query='ignored', person='Alex', group='family')
            assert 'Alex\' Aktien' in result
            assert 'synthetic author note' in result
            assert 'quoted' not in result and 'authored' not in result
            assert await tool.execute(query='ignored', group='other') == (
                'That group is not in your current WhatsApp memberships.')
        finally:
            reset_tool_context(token)
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
            is_owner=False, history_snapshot=snapshot))
        try:
            assert 'only in the owner' in await tool.execute(query='ignored', person='Alex')
        finally:
            reset_tool_context(token)
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id='family@g.us', canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            assert 'only in the owner' in await tool.execute(query='ignored', person='Alex')
        finally:
            reset_tool_context(token)
    p.close()


async def test_recall_person_alias_ambiguity_and_non_whatsapp_refusal_do_not_use_legacy():
    from yeoman_gateway.history.context import history_turn

    p, owner_phone = _person_lookup_projector()
    person_phone = '491000000003@s.whatsapp.net'
    p.db.execute("INSERT INTO contacts VALUES ('other-person','person',NULL,'Alex','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs)"
                 " VALUES ('other-person','whatsapp','pn_jid',?,'strong','owner_attested','[]')", (person_phone,))
    p.db.execute("UPDATE message_events SET payload_json=? WHERE event_id='members'", (
        __import__('json').dumps({'complete': True, 'participants': [
            ['491000000001@s.whatsapp.net'], ['491000000002@s.whatsapp.net'], [person_phone]]}),))
    p.db.commit()
    tool = RecallConversationTool(SimpleNamespace(get_or_create=Mock(side_effect=AssertionError('legacy'))))
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            result = await tool.execute(query='topic', person='Alex')
            assert 'Several people match' in result
            assert 'Alex' in result and 'Please re-call with a fuller name' in result
            assert 'other-person' not in result and person_phone not in result
        finally:
            reset_tool_context(token)
    token = set_tool_context(ToolInvocationContext(
        channel='telegram', chat_id='telegram:synthetic', request_text='topic'))
    try:
        assert 'only in the owner' in await tool.execute(query='topic', person='Alex')
    finally:
        reset_tool_context(token)
    token = set_tool_context(ToolInvocationContext(
        channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
        is_owner=True))
    try:
        assert 'unavailable in this context' in await tool.execute(query='topic', person='Alex')
    finally:
        reset_tool_context(token)
    p.close()


async def test_history_person_search_keeps_older_indexed_match_past_unrelated_rows():
    import time

    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, _ = _person_lookup_projector()
    unrelated = [
        (f'unrelated-{index:05d}', 'whatsapp', 'family@g.us', f'unrelated-native-{index}',
         'owner', None, 'unknown', 'in', 1700000000000, 'native', 'unrelated archive entry',
         None, None, None, 'native', '[]')
        for index in range(5001)
    ]
    p.db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', unrelated)
    p.db.execute("INSERT INTO messages VALUES ('zz-old','whatsapp','family@g.us','zz-old','owner',NULL,"
                 "'unknown','in',1690000000000,'native',\"Alex's old holdings record\",NULL,NULL,NULL,'native','[]')")
    p.db.execute('DELETE FROM messages_fts')
    p.db.execute('INSERT INTO messages_fts(message_id,chat_id,text) ' + __import__(
        'yeoman_gateway.history.schema', fromlist=['FTS_ROWS']).FTS_ROWS)
    p.db.commit()
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        now_ms = time.time_ns() // 1_000_000
        person = queries.resolve_person('Alexandra', ('family@g.us',), now_ms)
        assert person.status == 'resolved' and person.person_id == 'alex'
        rows = queries.search_person(chat_ids=('family@g.us',), contact_id=person.person_id,
                                     at_ms=now_ms, limit=10, query='holdings')
        assert 'zz-old' in {row['message_id'] for row in rows}
    p.close()


@pytest.mark.parametrize('owner_chat', ['phone', 'lid', 'second_phone'])
async def test_person_recall_excludes_weakly_attributed_authorship(owner_chat):
    import time

    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, owner_phone = _person_lookup_projector()
    owner_phone = _owner_test_chat(p, owner_phone, owner_chat)
    p.add('weak-author', chat='family@g.us', text='impostor payload')
    p.db.execute("UPDATE messages SET sender_contact_id='alex',sender_basis='push_name' WHERE message_id='weak-author'")
    p.add('weak-mention', chat='family@g.us', text='Alex appears in this unattributed message')
    p.db.execute("UPDATE messages SET sender_contact_id='alex',sender_basis='push_name' WHERE message_id='weak-mention'")
    p.db.commit()
    tool = RecallConversationTool(None)
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            result = await tool.execute(query='topic', person='Alex')
            assert 'synthetic author note' in result
            assert 'impostor payload' not in result
            assert any('sender=Alexandra' in line and 'synthetic author note' in line for line in result.splitlines())
            assert any('sender=unverified' in line and 'unattributed message' in line for line in result.splitlines())
            snapshot.connection.execute("UPDATE messages SET sender_basis='push_name' WHERE message_id='authored'")
            assert HistoryQueries(snapshot).resolve_person(
                'Alex', ('family@g.us',), time.time_ns() // 1_000_000
            ).status == 'not_found'
        finally:
            reset_tool_context(token)
    p.close()


@pytest.mark.parametrize('owner_chat', ['phone', 'lid', 'second_phone'])
async def test_person_search_ranks_query_prefix_matches_and_warns_when_query_matches_none(owner_chat):
    import time

    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.history.queries import HistoryQueries

    p, owner_phone = _person_lookup_projector()
    owner_phone = _owner_test_chat(p, owner_phone, owner_chat)
    async with history_turn(p) as snapshot:
        queries = HistoryQueries(snapshot)
        now_ms = time.time_ns() // 1_000_000
        person = queries.resolve_person('Alex', ('family@g.us',), now_ms)
        assert person.status == 'resolved'
        rows = queries.search_person(chat_ids=('family@g.us',), contact_id=person.person_id,
                                     at_ms=now_ms, limit=10, query='Aktie')
        assert rows[0]['message_id'] == 'quoted'
        assert rows[0]['query_match_count'] == 1
    tool = RecallConversationTool(None)
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_phone, canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            result = await tool.execute(query='unmatched-term', person='Alex')
            assert result.startswith("No messages matched the query terms 'unmatched-term'.")
            assert 'synthetic author note' in result
        finally:
            reset_tool_context(token)
    p.close()


class ScopedKnowledge:
    def __init__(self, p):
        self.p = p
        self.released = True
        self.generations = []
        self.history_source_ledger = SimpleNamespace()
        self._legacy_authority = SimpleNamespace()

    @contextmanager
    def history_scope(self, queries, sources):
        assert sources.ledger is self.history_source_ledger
        self.queries = queries
        try:
            yield
        finally:
            self.queries = None

    def delivery_identifiers_for_alias(self, alias, *, channel, scope_key):
        from yeoman_gateway.history.context import current_history_snapshot
        from yeoman_gateway.knowledge.models import GLOBAL_SCOPE_KEY
        assert scope_key == GLOBAL_SCOPE_KEY and channel == 'whatsapp'
        assert current_history_snapshot() is self.queries.snapshot
        self.generations.append(current_history_snapshot().generation)
        if not self.released or alias != 'synthetic-contact':
            return ()
        contact = self.queries.contact('person')
        if not contact:
            return ()
        rows = self.queries.snapshot.connection.execute("SELECT value FROM identifier_history WHERE contact_id=? AND kind='pn_jid' AND ended_ms IS NULL", (contact['contact_id'],)).fetchall()
        return tuple(SimpleNamespace(value=row[0]) for row in rows)


def contact_case(tmp_path):
    p = FileProjector(tmp_path)
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp','pn_jid','491000000001@s.whatsapp.net','strong','owner_attested','[]')")
    p.db.commit()
    return p, ScopedKnowledge(p)


async def test_a2a_recipient_resolution_reopens_after_replace_and_rechecks_before_send(tmp_path):
    p, knowledge = contact_case(tmp_path)
    policy = _AliasPolicy()
    policy.evaluate = lambda _: SimpleNamespace(accept_message=True, should_respond=True, allowed_tools={'message'})
    def resolver(kind, alias):
        return resolve_whatsapp_recipient(kind, alias, policy_adapter=policy, knowledge=knowledge)
    kwargs = dict(input=_input(recipient={'type': 'contact', 'alias': 'synthetic-contact'}), policy=policy,
                  resolver=resolver, history_projector=p, history_knowledge=knowledge)
    result, _, _, effects = await _invoke(**kwargs)
    assert result['status'] == 'completed'
    assert effects.calls[-1]['chat_id'] == '491000000001@s.whatsapp.net'
    before_inode = p.path.stat().st_ino
    p.replace('491000000002@s.whatsapp.net')
    assert p.path.stat().st_ino != before_inode
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls[-1]['chat_id'] == '491000000002@s.whatsapp.net'
    assert knowledge.generations == [1, 2]
    def replace_before_send(kind, alias):
        target = resolver(kind, alias)
        p.replace('491000000003@s.whatsapp.net')
        return target
    result, _, _, effects = await _invoke(**{**kwargs, 'resolver': replace_before_send})
    assert effects.calls == []
    assert result['status'] == 'rejected'
    assert p.live_snapshot_count == 0
    p.close()


async def test_a2a_pause_and_retired_contacts_fail_closed(tmp_path):
    p, knowledge = contact_case(tmp_path)
    policy = _AliasPolicy()
    policy.evaluate = lambda _: SimpleNamespace(accept_message=True, should_respond=True, allowed_tools={'message'})
    legacy = SimpleNamespace(store=SimpleNamespace(search_by_alias=Mock(side_effect=AssertionError('legacy'))))
    def resolver(kind, alias):
        return resolve_whatsapp_recipient(kind, alias, policy_adapter=policy, knowledge=knowledge, contacts_service=legacy)
    kwargs = dict(input=_input(recipient={'type': 'contact', 'alias': 'synthetic-contact'}), policy=policy,
                  resolver=resolver, history_projector=p, history_knowledge=knowledge)
    p.status = 'rebuilding'
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls == [] and knowledge.generations == []
    p.status = 'ready'
    result, _, _, effects = await _invoke(**{**kwargs, 'peer': 'unauthorized'})
    assert effects.calls == [] and p.acquisitions == 0
    knowledge.released = False
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls == []
    legacy.store.search_by_alias.assert_not_called()
    assert set(tmp_path.iterdir()) == {p.path} and p.live_snapshot_count == 0
    p.close()


@pytest.mark.parametrize('kind', ['send_text', 'send_media'])
def test_history_mentions_ignore_bridge_cache_and_refuse_unresolved_lid(kind):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION, valid_history_mentions
    assert PROTOCOL_VERSION == 6
    assert valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['491000000001@s.whatsapp.net']})
    assert not valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['100000000001@lid']})
    assert not valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['100000000001']})
    assert not valid_history_mentions({'historyMentionsResolved': 'true'})
    assert valid_history_mentions({'mentions': ['100000000001@lid']})


async def test_queued_history_generation_is_checked_after_transport_wait():
    from unittest.mock import AsyncMock

    from yeoman_gateway.channels.whatsapp import _SEND_HISTORY_GENERATION, WhatsAppChannel
    from yeoman_gateway.history.live import HistoryPaused
    p = Projector()
    channel = object.__new__(WhatsAppChannel)
    channel._history_projector = p
    channel._ws = SimpleNamespace(send=AsyncMock())
    channel.config = SimpleNamespace(max_payload_bytes=100000)
    channel._pending = {}
    channel._send_lock = asyncio.Lock()
    channel._require_token = lambda: 'synthetic-token'
    async def archive(*args, **kwargs):
        p.generation += 1
    channel._raw_archive_outbound = archive
    token = _SEND_HISTORY_GENERATION.set(p.generation)
    try:
        with pytest.raises(HistoryPaused):
            await channel._send_command('send_text', {'to': 'a@g.us', 'text': 'synthetic'}, 30)
        channel._ws.send.assert_not_called()
        assert not channel._pending
    finally:
        _SEND_HISTORY_GENERATION.reset(token)
        p.close()


async def test_background_subagent_clears_selected_lease_and_keeps_dormant_context():
    from contextvars import ContextVar

    from yeoman_gateway.agent.subagent import SubagentManager
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.processing.tool_context import current_tool_context
    marker = ContextVar('synthetic-marker', default=False)
    manager = object.__new__(SubagentManager)
    manager._running_tasks = {}
    observed = []
    async def run(*args):
        context = current_tool_context()
        observed.append((current_history_snapshot(), context.history_snapshot, marker.get()))
    manager._run_subagent = run
    p = Projector()
    token = marker.set(True)
    try:
        async with history_turn(p) as snapshot:
            tool_token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='a@g.us', history_snapshot=snapshot))
            try:
                await manager.spawn('synthetic task')
                tasks = tuple(manager._running_tasks.values())
            finally:
                reset_tool_context(tool_token)
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert observed == [(None, None, False)] and p.live_snapshot_count == 0
        tool_token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='a@g.us'))
        try:
            await manager.spawn('synthetic dormant task')
            tasks = tuple(manager._running_tasks.values())
        finally:
            reset_tool_context(tool_token)
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert observed[-1] == (None, None, True)
    finally:
        marker.reset(token)
        p.close()


@pytest.mark.parametrize('has_mention', [False, True])
async def test_scope_less_bus_send_resolves_mentions_and_closes_before_transport(has_mention):
    import json
    from unittest.mock import AsyncMock

    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_gateway.history.context import current_history_snapshot
    from yeoman_shared.config.schema import WhatsAppConfig
    p = Projector()
    phone, lid = '491000000001@s.whatsapp.net', '100000000001@lid'
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    for kind, value in (('pn_jid', phone), ('lid', lid)):
        p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp',?,?,'strong','owner_attested','[]')", (kind, value))
    p.db.execute("INSERT INTO message_events(event_id,channel,chat_id,kind,occurred_ms,time_certainty,payload_json,provenance,actor_basis,source_refs) VALUES ('roster','whatsapp','a@g.us','member_snapshot',1700000000000,'native',?,'native','unknown','[]')", (json.dumps({'complete': True, 'participants': [[phone, lid]]}),))
    p.db.commit()
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token='synthetic-token'), bus)
    channel._history_projector = p
    channel._history_mentions_selected = True
    channel._connected = True
    channel._stop_typing = AsyncMock()
    channel._raw_archive_outbound = AsyncMock()
    sent = []
    completed = asyncio.Event()
    original_send = channel.send
    async def send(message):
        try:
            assert current_history_snapshot() is None
            return await original_send(message)
        finally:
            completed.set()
    channel.send = send
    async def ws_send(encoded):
        assert p.live_snapshot_count == 0 and current_history_snapshot() is None
        frame = json.loads(encoded)
        sent.append(frame['payload'])
        channel._pending[frame['requestId']].set_result({'providerMessageId': 'synthetic-sent'})
    channel._ws = SimpleNamespace(send=ws_send)
    manager = object.__new__(ChannelManager)
    manager.bus, manager.channels = bus, {'whatsapp': channel}
    dispatcher = asyncio.create_task(manager._dispatch_outbound())
    text = 'synthetic @100000000001@lid' if has_mention else 'synthetic notice'
    message = OutboundMessage(channel='whatsapp', chat_id='a@g.us', content=text)
    try:
        await bus.publish_outbound(message)
        await asyncio.wait_for(completed.wait(), 30)
        assert len(sent) == 1
        assert sent[0]['historyMentionsResolved'] is True
        assert sent[0].get('mentions', []) == ([phone] if has_mention else [])
        assert sent[0]['text'] == ('synthetic @491000000001' if has_mention else text)
        assert p.acquisitions == 1 and p.live_snapshot_count == 0
        completed.clear()
        p.status = 'rebuilding'
        await bus.publish_outbound(message)
        await asyncio.wait_for(completed.wait(), 30)
        assert len(sent) == 1 and p.live_snapshot_count == 0
    finally:
        dispatcher.cancel()
        await dispatcher
        p.close()
