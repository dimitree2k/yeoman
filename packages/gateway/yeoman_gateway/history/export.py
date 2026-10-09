"""Authorized bounded reads; native turns borrow, standalone requests own a lease."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from yeoman_gateway.knowledge.models import KnowledgeError, TrustedReadContext

from .context import (
    current_history_snapshot,
    history_effect_metadata,
    history_knowledge_scope,
    history_turn,
)
from .live import HistoryPaused, HistoryProjector
from .queries import HistoryQueries
from .reader import HistorySnapshot


def validate_read_args(args: dict[str, Any]) -> None:
    if set(args) - {'chat_ids', 'after_ms', 'limit', 'aggregate'}:
        raise ValueError('unknown history read argument')
    chats = args.get('chat_ids')
    if not isinstance(chats, (list, tuple)) or not chats or len(chats) > 50 or any(not isinstance(c, str) or not c.strip() for c in chats):
        raise ValueError('explicit chat scope required')
    if type(args.get('after_ms')) is not int or args['after_ms'] < 0:
        raise ValueError('after_ms must be nonnegative integer milliseconds')
    if type(args.get('limit')) is not int or not 1 <= args['limit'] <= 500:
        raise ValueError('limit must be an integer from 1 to 500')
    if type(args.get('aggregate', True)) is not bool:
        raise ValueError('aggregate must be boolean')


def read_history_turn(snapshot: HistorySnapshot, *, context: TrustedReadContext,
                      chat_ids: tuple[str, ...], after_ms: int, limit: int,
                      aggregate: bool = True) -> dict[str, Any]:
    from yeoman_gateway.adapters.reply_archive_history import history_text
    from yeoman_gateway.knowledge._history_identity import current_history_scope
    started = time.monotonic()
    validate_read_args(dict(chat_ids=chat_ids, after_ms=after_ms, limit=limit, aggregate=aggregate))
    scope = current_history_scope()
    if scope is None or scope.queries.snapshot is not snapshot:
        raise HistoryPaused('authorized_knowledge_scope_required')
    if current_history_snapshot() is snapshot:
        history_effect_metadata()
    snapshot.assert_current(snapshot.generation)
    if not isinstance(context, TrustedReadContext) or context.channel != 'whatsapp':
        raise KnowledgeError('unauthorized', 'trusted WhatsApp scope required')
    policy = scope.identity._policy
    if context.policy_revision != policy.current_policy_revision():
        raise KnowledgeError('stale_revision', 'policy changed')
    owner = context.owner and hasattr(policy, 'admin_actor') and policy.admin_actor() == context.principal_id
    if context.owner and not owner:
        raise KnowledgeError('unauthorized', 'owner authorization required')
    if not owner and set(chat_ids) != {context.chat_id}:
        raise KnowledgeError('unauthorized', 'scope is outside the authorized chat')
    membership = policy.membership(context)
    if not owner and (membership is None or context.principal_id not in membership.members):
        raise KnowledgeError('unauthorized', 'current membership required')
    rows = []
    for chat_id in dict.fromkeys(chat_ids):
        for row in scope.queries.recent(chat_id=chat_id, after_ms=after_ms, limit=limit):
            audience = scope.queries.audience(row['message_id'])
            allowed = owner or (audience.status == 'known' and context.principal_id in audience.members
                                and context.recipient_principals is not None
                                and context.recipient_principals <= audience.members & membership.members)
            if not owner:
                from yeoman_gateway.knowledge._history_sources import _proof, principal_identifier
                if audience.status == 'author_only':
                    proof = _proof(scope.queries, row['message_id'])
                    allowed = proof is not None and proof[1] == context.principal_id and context.recipient_principals == frozenset({context.principal_id})
                for principal in context.recipient_principals or ():
                    value = principal_identifier(principal)
                    previous = scope.queries.resolve_identifier(value, at_ms=row['sent_ms'], time_basis='native') if value else None
                    current = scope.queries.resolve_identifier(value, at_ms=context.now_ms, time_basis='native') if value else None
                    if previous is None or previous != current:
                        allowed = False
            record = scope.sources.ledger.current(row['message_id'])
            if record is not None and record.revoked and record.content_fingerprint == scope.queries.content_fingerprint(row['message_id']):
                allowed = False
            if allowed:
                rows.append(row)
    rows.sort(key=lambda r: (r['sent_ms'] or 0, r['message_id']))
    rows = rows[-limit:]
    result: dict[str, Any] = {'generation': snapshot.generation, 'status': 'ready', 'count': len(rows),
                              'elapsed_ms': round((time.monotonic() - started) * 1000, 3)}
    if not aggregate:
        result['messages'] = [{'message_id': row['message_id'], 'chat_id': row['chat_id'],
                               'sent_ms': row['sent_ms'], 'text': history_text(row)} for row in rows]
    return result


async def read_history_export(projector: HistoryProjector, *, context: TrustedReadContext,
                              chat_ids: tuple[str, ...], after_ms: int, limit: int,
                              aggregate: bool = True) -> dict[str, Any]:
    if current_history_snapshot() is not None:
        raise HistoryPaused('standalone_read_forbidden_in_turn')
    validate_read_args(dict(chat_ids=chat_ids, after_ms=after_ms, limit=limit, aggregate=aggregate))
    knowledge = getattr(projector, 'history_knowledge', None)
    async with history_turn(projector) as snapshot:
        with history_knowledge_scope(snapshot, knowledge):
            return read_history_turn(snapshot, context=context, chat_ids=chat_ids,
                                     after_ms=after_ms, limit=limit, aggregate=aggregate)


def secondary_archive(legacy: Any, config: Any) -> Any:
    if config.live_projection_enabled and config.readers.secondary:
        from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
        snapshot = current_history_snapshot()
        if snapshot is None:
            raise HistoryPaused('secondary_history_scope_required')
        history_effect_metadata()
        return HistoryReplyArchiveAdapter(HistoryQueries(snapshot))
    if getattr(config, 'legacy_writers_disabled', False):
        raise HistoryPaused('secondary_reader_unselected')
    return legacy


def require_isolated_paths(*paths: Path) -> None:
    """Reject runtime roots and symlinked inputs/sidecars before opening anything."""
    from yeoman_shared.utils.helpers import get_operational_data_path
    homes = (Path('/home/dm/.yeoman'), Path.home() / '.yeoman')
    protected = (get_operational_data_path(), *(home / name for home in homes for name in ('data', 'var', 'run', 'workspace')))
    for path in paths:
        resolved = path.expanduser().resolve()
        for candidate in (resolved, *(Path(str(path) + suffix).resolve() for suffix in ('-wal', '-shm', '.lock'))):
            if any(candidate == root.resolve() or root.resolve() in candidate.parents for root in protected):
                raise ValueError('explicit isolated paths required; protected runtime path refused')


async def request_history_read(socket_path: Path, args: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get('YEOMAN_HISTORY_TOOL_TURN') == '1':
        raise HistoryPaused('standalone history read forbidden in selected tool turn; use history_read')
    validate_read_args(args)
    writer = None
    try:
        async with asyncio.timeout(30):
            reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=1024 * 1024)
            writer.write(json.dumps({'cmd': 'history_read', 'args': args}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await reader.readline())
            if not isinstance(result, dict):
                raise ValueError('invalid history response')
            return result
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def secondary_consumer(function: Any) -> Any:
    """The async consumer owns a root lease; its synchronous helpers borrow it."""
    from functools import wraps
    @wraps(function)
    async def scoped(self: Any, *args: Any, **kwargs: Any) -> Any:
        channel = kwargs.get('channel') or (getattr(args[0], 'channel', None) if args else None)
        if channel is not None and channel != 'whatsapp':
            return await function(self, *args, **kwargs)
        config = getattr(self, 'config', getattr(self, '_config', None))
        history = getattr(config, 'history', None)
        selected = history is not None and history.live_projection_enabled and history.readers.secondary
        if not selected:
            if history is not None and history.legacy_writers_disabled:
                raise HistoryPaused('secondary_reader_unselected')
            return await function(self, *args, **kwargs)
        projector = getattr(self, '_history_projector', None)
        if projector is None:
            raise HistoryPaused('secondary_history_projector_required')
        async with history_turn(projector) as snapshot:
            with history_knowledge_scope(snapshot, getattr(self, '_history_knowledge', None)):
                return await function(self, *args, **kwargs)
    return scoped


class SecondaryArchive:
    """Resolve an adapter on each call, retaining neither a connection nor rows."""
    def __init__(self, legacy: Any, config: Any):
        self.legacy, self.config = legacy, config

    def __getattr__(self, name: str) -> Any:
        def read(*args: Any, **kwargs: Any) -> Any:
            channel = args[0] if args else kwargs.get('channel')
            if channel != 'whatsapp':
                return getattr(self.legacy, name)(*args, **kwargs)
            adapter = secondary_archive(self.legacy, self.config)
            method = adapter.row if name == 'lookup_message' and adapter is not self.legacy else getattr(adapter, name)
            return method(*args, **kwargs)
        return read


def cli_secondary_request(command: str, args: dict[str, Any]) -> dict[str, Any]:
    """Owner-local bounded endpoints, never a production database fallback."""
    import typer
    from yeoman_shared.config.loader import load_config
    if command not in {'history_chats', 'persona_evolution_read', 'knowledge_read', 'knowledge_accounting', 'knowledge_statements'}:
        raise ValueError('unsupported secondary endpoint')
    if os.environ.get('YEOMAN_HISTORY_TOOL_TURN') == '1':
        raise HistoryPaused('standalone acquisition forbidden in selected tool turn')
    async def request() -> dict[str, Any]:
        writer = None
        try:
            async with asyncio.timeout(30):
                reader, writer = await asyncio.open_unix_connection(str(Path(load_config().ipc.gateway_socket_path).expanduser()), limit=1024 * 1024)
                writer.write(json.dumps({'cmd': command, 'args': args}).encode() + b'\n')
                await writer.drain()
                result = json.loads(await reader.readline())
                if not isinstance(result, dict) or result.get('status') != 'ready':
                    raise ValueError('Gateway secondary read unavailable')
                return result
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
    try:
        return asyncio.run(request())
    except (OSError, ValueError, TimeoutError):
        raise typer.BadParameter('Gateway authorized secondary read unavailable') from None


def read_history_frozen(db_path: Path, boundary: Any, *, knowledge: Any,
                        context: TrustedReadContext, chat_ids: tuple[str, ...],
                        after_ms: int, limit: int, aggregate: bool = True) -> dict[str, Any]:
    from yeoman_gateway.history.reader import HistoryReader
    require_isolated_paths(db_path)
    reader = HistoryReader(db_path)
    snapshot = None
    try:
        snapshot = reader.open_snapshot(boundary)
        with history_knowledge_scope(snapshot, knowledge):
            result = read_history_turn(snapshot, context=context, chat_ids=chat_ids,
                                       after_ms=after_ms, limit=limit, aggregate=aggregate)
            return dict(result, status='frozen/non-live')
    finally:
        if snapshot is not None:
            snapshot.close()
        reader.close()
