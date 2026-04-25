"""Delayed outcome classification for proactive speakups."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.storage.inbound_archive import InboundArchive

OutcomeLabel = str
OutcomeClassifier = Callable[[str], dict[str, Any] | str | Awaitable[dict[str, Any] | str]]

VALID_OUTCOMES = {
    "replied",
    "reacted",
    "silence",
    "topic_changed",
    "pushback",
    "mixed",
}


class OutcomeEnricher:
    """Classify what happened after sent speakups."""

    def __init__(
        self,
        *,
        log: SpeakupLog,
        inbound_archive: InboundArchive,
        classifier: OutcomeClassifier,
        delay: timedelta = timedelta(minutes=30),
        window: timedelta = timedelta(hours=2),
    ) -> None:
        self._log = log
        self._archive = inbound_archive
        self._classifier = classifier
        self._delay = delay
        self._window = window

    async def run_once(self, *, now: datetime | None = None, limit: int = 20) -> dict[str, int]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        before = (current - self._delay).timestamp()
        rows = await self._log.pending_outcome_rows(before=before, limit=limit)
        classified = 0
        for row in rows:
            outcome = await self._classify_row(row)
            if outcome is None:
                continue
            await self._log.mark_outcome(
                str(row["id"]),
                outcome=outcome,
                now=current.timestamp(),
            )
            classified += 1
        return {"classified": classified}

    async def _classify_row(self, row: dict[str, Any]) -> OutcomeLabel | None:
        committed_at = float(row["committed_at"])
        since = datetime.fromtimestamp(committed_at, UTC)
        until = since + self._window
        after = self._archive.lookup_messages_in_range(
            str(row["channel"]),
            str(row["chat_id"]),
            since,
            until,
            limit=50,
        )
        payload = {
            "instruction": (
                "Return JSON with one field: outcome. Allowed values are "
                + ", ".join(sorted(VALID_OUTCOMES))
                + ". Classify the chat response after the speakup."
            ),
            "speakup": {
                "id": row["id"],
                "message": row["message"],
                "action_type": row["action_type"],
                "profile": row["profile"],
            },
            "post_speakup_window": after,
        }
        raw = self._classifier(json.dumps(payload, default=str, ensure_ascii=False))
        if inspect.isawaitable(raw):
            raw = await raw
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return None
        outcome = str(parsed.get("outcome") or "").strip()
        return outcome if outcome in VALID_OUTCOMES else None
