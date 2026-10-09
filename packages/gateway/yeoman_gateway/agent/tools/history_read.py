"""In-process history export; never acquire a child lease."""
from __future__ import annotations

import json
import time
from typing import Any

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.history.context import history_effect_metadata
from yeoman_gateway.history.export import read_history_turn, validate_read_args
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedReadContext
from yeoman_gateway.processing.tool_context import current_tool_context


class HistoryReadTool(Tool):
    def __init__(self, knowledge: Any):
        self.knowledge = knowledge

    @property
    def name(self) -> str:
        return 'history_read'

    @property
    def description(self) -> str:
        return 'Read policy-authorized recent WhatsApp data from this turn. Defaults to aggregate counts.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'chat_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 50},
            'after_ms': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500},
            'aggregate': {'type': 'boolean', 'default': True}}, 'required': ['chat_ids', 'after_ms', 'limit']}

    async def execute(self, **kwargs: Any) -> str:
        try:
            validate_read_args(kwargs)
            turn = current_tool_context()
            if turn is None or turn.channel not in {'whatsapp', 'cli', 'system'} or turn.history_snapshot is None:
                raise HistoryPaused('tool_history_scope_required')
            history_effect_metadata()
            if self.knowledge is None:
                raise HistoryPaused('authorized_knowledge_scope_required')
            from yeoman_gateway.knowledge._history_identity import current_history_scope
            from yeoman_gateway.knowledge._history_sources import principal_identifier
            scope = current_history_scope(self.knowledge._store)
            if scope is None or scope.queries.snapshot is not turn.history_snapshot:
                raise HistoryPaused('authorized_knowledge_scope_required')
            principal = turn.canonical_user_id
            if not principal:
                raise KnowledgeError('unauthorized', 'originating principal required')
            owner = scope.identity._policy.admin_actor()
            owner_only = principal == owner and bool(owner) and (
                (turn.channel == 'cli' and turn.chat_id == 'direct')
                or (turn.channel in {'whatsapp', 'system'} and turn.chat_id == principal_identifier(owner)))
            if owner_only:
                context = self.knowledge.owner_read_context(channel='whatsapp', chat_id=turn.chat_id, purpose='reply')
            else:
                now = int(time.time() * 1000)
                direct = not turn.chat_id.endswith('@g.us')
                members = scope.queries.members(chat_id=turn.chat_id, at_ms=now)
                if not direct and members.status != 'known':
                    raise KnowledgeError('unauthorized', 'current recipients unknown')
                context = TrustedReadContext(principal, 'whatsapp', turn.chat_id,
                    frozenset({principal}) if direct else members.members,
                    members.snapshot_id, self.knowledge.policy_revision, 'reply', now,
                    is_direct=direct)
            return json.dumps(read_history_turn(turn.history_snapshot, context=context,
                chat_ids=tuple(kwargs['chat_ids']), after_ms=kwargs['after_ms'], limit=kwargs['limit'],
                aggregate=kwargs.get('aggregate', True)))
        except (HistoryPaused, KnowledgeError, ValueError) as exc:
            return json.dumps({'status': 'paused' if isinstance(exc, HistoryPaused) else 'rejected',
                               'reason': str(exc)})
