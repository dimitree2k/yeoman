"""SQLite inbound archive adapter for the typed reply archive port."""

from __future__ import annotations

from typing import override

from nanobot.core.models import ArchivedMessage, InboundEvent
from nanobot.core.ports import ReplyArchivePort
from nanobot.storage.inbound_archive import InboundArchive


class SqliteReplyArchiveAdapter(ReplyArchivePort):
    """Adapter around legacy `InboundArchive` with typed return models."""

    def __init__(self, archive: InboundArchive) -> None:
        self._archive = archive

    @override
    def record_inbound(self, event: InboundEvent) -> None:
        if not event.message_id:
            return
        self._archive.record_inbound(
            channel=event.channel,
            chat_id=event.chat_id,
            message_id=event.message_id,
            participant=event.participant,
            sender_id=event.sender_id,
            text=event.content,
            timestamp=int(event.timestamp.timestamp()),
        )

    @override
    def lookup_message(self, channel: str, chat_id: str, message_id: str) -> ArchivedMessage | None:
        row = self._archive.lookup_message(channel, chat_id, message_id)
        if row is None:
            return None
        return self._to_archived(row)

    @override
    def lookup_message_any_chat(
        self,
        channel: str,
        message_id: str,
        *,
        preferred_chat_id: str | None = None,
    ) -> ArchivedMessage | None:
        row = self._archive.lookup_message_any_chat(
            channel,
            message_id,
            preferred_chat_id=preferred_chat_id,
        )
        if row is None:
            return None
        return self._to_archived(row)

    @override
    def lookup_messages_before(
        self,
        channel: str,
        chat_id: str,
        anchor_message_id: str,
        *,
        limit: int,
    ) -> list[ArchivedMessage]:
        rows = self._archive.lookup_messages_before(
            channel,
            chat_id,
            anchor_message_id,
            limit=limit,
        )
        return [self._to_archived(row) for row in rows]

    def _to_archived(self, row: dict[str, object]) -> ArchivedMessage:
        raw_timestamp = row.get("timestamp")
        timestamp: int | None
        if isinstance(raw_timestamp, int):
            timestamp = raw_timestamp
        elif isinstance(raw_timestamp, float):
            timestamp = int(raw_timestamp)
        else:
            timestamp = None

        return ArchivedMessage(
            channel=str(row.get("channel") or ""),
            chat_id=str(row.get("chat_id") or ""),
            message_id=str(row.get("message_id") or ""),
            participant=str(row.get("participant")) if row.get("participant") else None,
            sender_id=str(row.get("sender_id")) if row.get("sender_id") else None,
            text=str(row.get("text") or ""),
            timestamp=timestamp,
            created_at=str(row.get("created_at") or ""),
        )
