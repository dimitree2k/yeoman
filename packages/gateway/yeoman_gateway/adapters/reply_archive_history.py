"""Current history rows formatted for existing archive consumers, within a lease."""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime

from yeoman_gateway.core.models import ArchivedMessage, InboundEvent
from yeoman_gateway.core.ports import ReplyArchivePort
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries, Row


def history_text(row: Row) -> str:
    """Keep derived media observations labelled separately from authored text."""
    text = row['current_text'] or ''
    media = json.loads(row['media_json']) if row.get('media_json') else {}
    for key in ('transcript', 'description', 'ocr_text'):
        observation = media.get(key)
        value = observation.get('text') if isinstance(observation, dict) else None
        if value:
            text += f'\n[Derived {key}]: {value}'
    return text


def archive_row(queries: HistoryQueries, row: Row) -> Row:
    contact = queries.contact(row['sender_contact_id']) if row['sender_contact_id'] else None
    return {**row, 'message_id': row['native_message_id'], 'event_id': row['message_id'],
            'sender_id': row['sender_identifier'], 'participant': row['sender_identifier'],
            'sender_name': contact['display_name'] if contact else None,
            'text': history_text(row),
            'timestamp': row['sent_ms'] // 1000 if row['sent_ms'] is not None else None,
            'reply_to_message_id': row['reply_to_native_id'], 'created_at': ''}


class HistoryReplyArchiveAdapter(ReplyArchivePort):
    def __init__(self, queries: HistoryQueries):
        self.queries = queries

    def record_inbound(self, event: InboundEvent) -> None:
        raise HistoryPaused('history_archive_read_only')

    def lookup_message(self, channel: str, chat_id: str, message_id: str) -> ArchivedMessage | None:
        row = self.row(channel, chat_id, message_id)
        return self._typed(row) if row else None

    def row(self, channel: str, chat_id: str, message_id: str) -> Row | None:
        if channel != 'whatsapp':
            return None
        row = self.queries.native_message(chat_id=chat_id, native_id=message_id)
        if row is None:
            row = self.queries.message(message_id)
            if row is None or row['chat_id'] != chat_id:
                return None
        return archive_row(self.queries, row)

    def lookup_message_any_chat(self, channel: str, message_id: str, *, preferred_chat_id: str | None = None) -> ArchivedMessage | None:
        # A native ID alone carries no permission to read another chat.
        return self.lookup_message(channel, preferred_chat_id, message_id) if preferred_chat_id else None

    def lookup_messages_before(self, channel: str, chat_id: str, anchor_message_id: str, *, limit: int) -> list[ArchivedMessage]:
        anchor = self.row(channel, chat_id, anchor_message_id)
        if anchor is None:
            return []
        rows = self.queries.recent(chat_id=chat_id, before_id=anchor['event_id'], limit=min(500, limit))
        return [self._typed(archive_row(self.queries, row)) for row in reversed(rows)]

    def lookup_messages_in_range(self, channel: str, chat_id: str, since: datetime, until: datetime | None = None, *, limit: int = 300, latest: bool = False) -> list[Row]:
        if channel != 'whatsapp':
            return []
        after = int(since.timestamp() * 1000)
        before = int((until or datetime.now(UTC)).timestamp() * 1000)
        if latest:
            rows = self.queries.recent(chat_id=chat_id, after_ms=after - 1, limit=min(500, limit))
            rows = [row for row in rows if row['sent_ms'] <= before]
        else:
            rows = self.queries.window(chat_id=chat_id, after_ms=after - 1, before_ms=before + 1, limit=min(500, limit))
        return [archive_row(self.queries, row) for row in rows]

    @staticmethod
    def _typed(row: Row) -> ArchivedMessage:
        return ArchivedMessage(**{field.name: row.get(field.name) for field in fields(ArchivedMessage)})
