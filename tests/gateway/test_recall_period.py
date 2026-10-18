"""Owner-DM recall by time span: list a group's day, the assistant's turns and their reactions."""
import json
import re
from datetime import datetime, timedelta

import pytest
from test_history_tools import _owner_test_chat, _person_lookup_projector
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool, period_bounds
from yeoman_gateway.history.schema import FTS_ROWS
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def test_period_bounds_follow_local_days_and_relative_spans():
    now = datetime(2026, 10, 18, 14, 30)
    now_ms = _ms(now)
    midnight = _ms(datetime(2026, 10, 18))
    assert period_bounds('today', now_ms) == (midnight, now_ms + 1)
    assert period_bounds('Yesterday', now_ms) == (_ms(datetime(2026, 10, 17)), midnight)
    assert period_bounds('2026-10-01', now_ms) == (
        _ms(datetime(2026, 10, 1)), _ms(datetime(2026, 10, 2)))
    assert period_bounds('6h', now_ms) == (_ms(now - timedelta(hours=6)), now_ms + 1)
    assert period_bounds('last_3d', now_ms) == (_ms(now - timedelta(days=3)), now_ms + 1)
    for invalid in ('0h', 'last week', '2026-13-01', ''):
        assert period_bounds(invalid, now_ms) is None


def _day_in_group():
    """A group day with no shared keyword: the assistant's turns, replies and reactions."""
    p, owner_phone = _person_lookup_projector()
    owner_chat = _owner_test_chat(p, owner_phone, 'lid')
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [
        ('t1', 'in', 'Achtung, das ist ein Test', midnight + (now - midnight) * 0.2),
        ('t2', 'out', 'Verstanden, Chef', midnight + (now - midnight) * 0.4),
        ('t3', 'in', 'Hahaha einfach beleidigt', midnight + (now - midnight) * 0.6),
    ]
    rows = [('y1', 'in', 'gestern war Ruhe', midnight - timedelta(hours=3)), *today]
    for mid, direction, text, moment in rows:
        p.add(mid, chat='family@g.us', text=text, direction=direction)
        p.db.execute('UPDATE messages SET sent_ms=? WHERE message_id=?', (_ms(moment), mid))
    for event_id, emoji, removed, offset in (('r1', '😂', False, 1), ('r2', '🙏', False, 2),
                                             ('r3', '🙏', True, 3)):
        actor = 'alex' if event_id == 'r1' else 'owner'
        p.db.execute(
            "INSERT INTO message_events(event_id,kind,channel,chat_id,target_message_id,"
            "actor_contact_id,occurred_ms,time_certainty,payload_json,provenance,actor_basis,"
            "source_refs) VALUES (?,'reaction','whatsapp','family@g.us','t2',?,?,'native',?,"
            "'native','native_identifier','[]')",
            (event_id, actor, _ms(today[1][3]) + offset,
             json.dumps({'emoji': emoji, 'removed': removed})))
    p.db.execute('DELETE FROM messages_fts')
    p.db.execute('INSERT INTO messages_fts(message_id,chat_id,text) ' + FTS_ROWS)
    p.db.commit()
    return p, owner_chat


async def _run(p, owner_chat, **kwargs):
    from yeoman_gateway.history.context import history_turn

    tool = RecallConversationTool(None, group_resolver=lambda ref: (
        {'family': 'family@g.us'}.get(ref), None))
    async with history_turn(p) as snapshot:
        token = set_tool_context(ToolInvocationContext(
            channel='whatsapp', chat_id=owner_chat, canonical_user_id='whatsapp:491000000001',
            is_owner=True, history_snapshot=snapshot))
        try:
            return await tool.execute(**kwargs)
        finally:
            reset_tool_context(token)


@pytest.mark.skipif(datetime.now().hour == 0 and datetime.now().minute < 5,
                    reason='needs a few minutes of today behind the clock')
async def test_group_period_lists_the_day_in_order_with_assistant_turns_and_reactions():
    p, owner_chat = _day_in_group()
    try:
        result = await _run(p, owner_chat, group='family', period='today')
    finally:
        p.close()
    lines = result.splitlines()
    assert lines[0] == "Found 3 message(s) for period 'today', oldest first:"
    assert [line.split(': ', 1)[1] for line in lines[1:]] == [
        'Achtung, das ist ein Test',
        # The owner took back the 🙏; only Alex's 😂 is current.
        'Verstanden, Chef [reactions: 😂]',
        'Hahaha einfach beleidigt',
    ]
    assert '[assistant]' in lines[2]
    assert 'gestern' not in result
    assert datetime.now().strftime('%Y-%m-%d') in lines[1]
    assert not re.search(r'\d{13}', result)


async def test_period_narrows_a_keyword_search_and_needs_query_or_period():
    p, owner_chat = _day_in_group()
    try:
        both = await _run(p, owner_chat, group='family', query='gestern', period='today')
        anytime = await _run(p, owner_chat, group='family', query='gestern')
        missing = await _run(p, owner_chat, group='family')
        invalid = await _run(p, owner_chat, group='family', period='last week')
    finally:
        p.close()
    assert both == "No matching messages found for 'gestern' in period 'today'."
    assert 'gestern war Ruhe' in anytime
    assert missing == 'Error: supply a query, a period, or both.'
    assert invalid.startswith('Error: period must be')
