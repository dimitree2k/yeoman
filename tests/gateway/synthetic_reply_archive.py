"""Small in-memory ReplyArchivePort fake for consumer unit tests."""
from __future__ import annotations

from datetime import UTC, datetime


class SyntheticReplyArchive:
    def __init__(self, *args, **kwargs):
        self._rows: list[dict[str, object]] = []

    def record_inbound(self, **values) -> None:
        row = dict(values)
        row.setdefault('event_id', row.get('message_id'))
        row.setdefault('created_at', '')
        self._rows.append(row)

    def lookup_message(self, channel: str, chat_id: str, message_id: str):
        return next((dict(row) for row in reversed(self._rows)
                     if row.get('channel') == channel and row.get('chat_id') == chat_id
                     and row.get('message_id') == message_id), None)

    row = lookup_message

    def lookup_message_any_chat(self, channel: str, message_id: str, *, preferred_chat_id=None):
        if preferred_chat_id is None:
            return None
        return self.lookup_message(channel, preferred_chat_id, message_id)

    def lookup_messages_in_range(self, channel: str, chat_id: str, since: datetime,
                                 until: datetime | None = None, *, limit=300, latest=False):
        low = since.timestamp()
        high = (until or datetime.now(UTC)).timestamp()
        rows = [dict(row) for row in self._rows if row.get('channel') == channel
                and row.get('chat_id') == chat_id
                and low <= float(row.get('timestamp') or 0) <= high]
        rows.sort(key=lambda row: (float(row.get('timestamp') or 0), str(row.get('message_id') or '')))
        if latest:
            rows = rows[-max(0, int(limit)):]
        else:
            rows = rows[:max(0, int(limit))]
        return rows

    def lookup_messages_before(self, channel: str, chat_id: str, anchor_message_id: str, *, limit: int):
        anchor = self.lookup_message(channel, chat_id, anchor_message_id)
        if anchor is None:
            return []
        before = float(anchor.get('timestamp') or 0)
        rows = [dict(row) for row in self._rows if row.get('channel') == channel
                and row.get('chat_id') == chat_id and float(row.get('timestamp') or 0) < before]
        rows.sort(key=lambda row: (float(row.get('timestamp') or 0), str(row.get('message_id') or '')))
        return rows[-max(0, int(limit)):]

    def close(self) -> None:
        self._rows.clear()
