"""Weekly owner DM digest and the labeler's alert sender."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from yeoman_gateway.history.group_signals import previous_week
from yeoman_gateway.reception.alerts import alert_message
from yeoman_gateway.reception.report import weekly_digest
from yeoman_gateway.reception.store import SentimentStore

DIGEST_RUN_HOUR = 5


@dataclass(frozen=True, slots=True)
class OwnerDigestReport:
    sent: bool = False
    chats: int = 0


class OwnerDigestJob:
    """Monday after the weekly rows exist: one compact private message."""

    def __init__(
        self,
        *,
        store: SentimentStore,
        chats: Callable[[], Iterable[str]],
        chat_labels: Callable[[], Mapping[str, str]],
        send: Callable[[str], Awaitable[Any]],
        quotes: Callable[[str, str], Awaitable[list[str]]] | None = None,
        clock_ms: Callable[[], int] | None = None,
        run_hour: int = DIGEST_RUN_HOUR,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._chats = chats
        self._chat_labels = chat_labels
        self._send = send
        self._quotes = quotes
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._run_hour = int(run_hour)
        self._tz = tz
        self._last_week: str | None = None

    async def tick(self, *, now_ms: int | None = None) -> OwnerDigestReport:
        moment = int(now_ms if now_ms is not None else self._clock_ms())
        local = datetime.fromtimestamp(moment / 1000, self._tz)
        local = local if self._tz is not None else local.astimezone()
        if local.weekday() != 0 or local.hour < self._run_hour:
            return OwnerDigestReport()
        target = previous_week(moment, tz=self._tz)
        if self._last_week == target.label:
            return OwnerDigestReport()
        if not self._store.claim_digest(target.label):
            self._last_week = target.label
            return OwnerDigestReport()
        chat_ids = [str(item) for item in self._chats() if str(item)]
        quotes: dict[str, list[str]] = {}
        if self._quotes is not None:
            for chat_id in chat_ids:
                try:
                    quotes[chat_id] = list(await self._quotes(chat_id, target.label))
                except Exception:  # noqa: BLE001 - quotes are optional
                    quotes[chat_id] = []
        text, used = weekly_digest(
            self._store, chat_labels=dict(self._chat_labels()), chat_ids=chat_ids,
            now_ms=moment, quotes=quotes,
        )
        self._last_week = target.label
        if not text.strip():
            return OwnerDigestReport()
        await self._send(text)
        return OwnerDigestReport(sent=True, chats=len(used) or len(chat_ids))


def alert_sender(
    *, store: SentimentStore, chat_labels: Callable[[], Mapping[str, str]],
    send: Callable[[str], Awaitable[Any]],
) -> Callable[[str, str], Awaitable[None]]:
    """Build the labeler's alert callback; rate limiting is claimed by the job."""

    async def _send(chat_id: str, reason: str) -> None:
        label = dict(chat_labels()).get(chat_id, chat_id)
        await send(alert_message(chat_label=label, reason=reason))

    return _send


__all__ = ["DIGEST_RUN_HOUR", "OwnerDigestJob", "OwnerDigestReport", "alert_sender"]
