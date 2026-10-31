"""Weekly P1 computation and the Monday tick that fills ``weekly`` rows."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from loguru import logger

from yeoman_gateway.history.group_signals import (
    BASELINE_EVERY,
    BASELINE_MAX,
    DIRECT_WINDOW_MS,
    INDIRECT_WINDOW_MS,
    MIN_ARVID_MESSAGES,
    MIN_HUMAN_MESSAGES,
    SILENCE_WINDOW_MS,
    DigestWeek,
    iso_week,
    previous_week,
    read_reception_signals,
    week_moment_ms,
    week_of,
)
from yeoman_gateway.reception.store import SentimentStore
from yeoman_gateway.reception.store import now_ms as _now_ms

#: Monday after the existing digest tick (participation digest is 04:00 local).
WEEKLY_RUN_HOUR = 5


@dataclass(frozen=True, slots=True)
class ReceptionBounds:
    """Every deterministic knob, read from the shared config once."""

    direct_window_ms: int = DIRECT_WINDOW_MS
    indirect_window_ms: int = INDIRECT_WINDOW_MS
    silence_window_ms: int = SILENCE_WINDOW_MS
    min_arvid_messages: int = MIN_ARVID_MESSAGES
    min_human_messages: int = MIN_HUMAN_MESSAGES
    baseline_every: int = BASELINE_EVERY
    baseline_max: int = BASELINE_MAX

    @classmethod
    def from_config(cls, config: Any) -> "ReceptionBounds":
        reception = config.processing.reception
        return cls(
            direct_window_ms=int(reception.direct_window_minutes) * 60_000,
            indirect_window_ms=int(reception.indirect_window_minutes) * 60_000,
            silence_window_ms=int(reception.silence_window_minutes) * 60_000,
            min_arvid_messages=int(reception.min_arvid_messages),
            min_human_messages=int(reception.min_human_messages),
            baseline_every=int(reception.baseline_every),
            baseline_max=int(reception.baseline_max),
        )


def persona_sha(text: str | None) -> str:
    """Stable sha256 of persona content; the empty persona has a fixed sentinel."""
    if text is None:
        return "unknown"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reception_policy_snapshot(resolved: Any, *, persona_sha_value: str = "") -> dict[str, Any]:
    """Only reception-relevant policy fields; no member ids, no chat text."""
    participation = getattr(resolved, "participation", None)
    snapshot: dict[str, Any] = {
        "persona_file": str(getattr(resolved, "persona_file", "") or ""),
        "persona_sha": persona_sha_value,
        "when_to_reply": str(getattr(resolved, "when_to_reply_mode", "") or ""),
        "spontaneity": {
            "enabled": bool(getattr(resolved, "spontaneity_enabled", False)),
            "profile": str(getattr(resolved, "spontaneity_profile", "") or ""),
            "daily_cap": getattr(resolved, "spontaneity_daily_cap", None),
            "allowed_actions": list(
                getattr(resolved, "spontaneity_allowed_actions", None) or ()
            ),
            "preview": getattr(resolved, "spontaneity_preview", None),
        },
        "participation": {
            "enabled": bool(getattr(participation, "enabled", False)),
            "allow_initiation": bool(getattr(participation, "allow_initiation", False)),
            "allow_continuation": bool(getattr(participation, "allow_continuation", False)),
            "allow_reactions": bool(getattr(participation, "allow_reactions", False)),
            "max_unsolicited_comments_per_window": getattr(
                participation, "max_unsolicited_comments_per_window", 0
            ),
            "comment_window_minutes": getattr(participation, "comment_window_minutes", 0),
            "max_reactions_per_window": getattr(
                participation, "max_reactions_per_window", 0
            ),
        },
    }
    return snapshot


@dataclass(frozen=True, slots=True)
class WeeklyOutcome:
    chat_id: str
    week: str
    status: str
    arvid_messages: int
    human_messages: int


class ReceptionMetricsService:
    """Compute one chat-week in a history turn, then store the numbers."""

    def __init__(
        self,
        *,
        store: SentimentStore,
        history: Any,
        bounds: ReceptionBounds | Callable[[], ReceptionBounds],
        policy_context: Callable[[str], tuple[str, str, dict[str, Any]]] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._history = history
        self._bounds = bounds
        self._policy_context = policy_context
        self._tz = tz

    @property
    def tz(self) -> tzinfo | None:
        return self._tz

    @property
    def store(self) -> SentimentStore:
        return self._store

    async def compute_week(self, chat_id: str, week: DigestWeek) -> WeeklyOutcome:
        from yeoman_gateway.history.context import history_turn
        from yeoman_gateway.history.queries import HistoryQueries

        bounds = self._bounds() if callable(self._bounds) else self._bounds
        meta = self._store.outbound_meta(
            chat_id, since_ms=week.start_ms, until_ms=week.end_ms
        )
        async with history_turn(self._history) as snapshot:
            queries = HistoryQueries(snapshot)
            metrics = read_reception_signals(
                queries, chat_id, week, outbound_meta=meta,
                min_arvid_messages=bounds.min_arvid_messages,
                min_human_messages=bounds.min_human_messages,
                baseline_every=bounds.baseline_every,
                baseline_max=bounds.baseline_max,
                direct_window_ms=bounds.direct_window_ms,
                indirect_window_ms=bounds.indirect_window_ms,
                silence_window_ms=bounds.silence_window_ms,
            )
        persona_file, sha, snapshot_fields = (
            self._policy_context(chat_id)
            if self._policy_context is not None
            else ("", "unknown", {})
        )
        self._store.store_weekly(
            chat_id=chat_id,
            iso_week=week.label,
            computed_at_ms=_now_ms(),
            status=metrics.status,
            persona_file=persona_file,
            persona_sha=sha,
            policy_snapshot=snapshot_fields,
            metrics=metrics.metrics(),
        )
        if sha and sha != "unknown":
            self._store.note_persona(chat_id=chat_id, persona_sha=sha, now_ms=_now_ms())
        logger.info(
            "reception_week chat={} week={} status={} arvid={} human={}",
            str(chat_id)[:12], week.label, metrics.status,
            metrics.arvid_messages, metrics.human_messages,
        )
        return WeeklyOutcome(
            chat_id=chat_id, week=week.label, status=metrics.status,
            arvid_messages=metrics.arvid_messages,
            human_messages=metrics.human_messages,
        )

    async def backfill(
        self, *, chat_id: str, since_ms: int, until_ms: int | None = None
    ) -> list[WeeklyOutcome]:
        """Recompute every complete ISO week in ``[since, until)``, oldest first."""
        moment = int(until_ms if until_ms is not None else _now_ms())
        first = week_of(int(since_ms), tz=self._tz)
        last = week_of(moment, tz=self._tz)
        outcomes: list[WeeklyOutcome] = []
        week = first
        while week.start_ms < last.start_ms and week.start_ms < moment:
            outcomes.append(await self.compute_week(chat_id, week))
            week = week_of(week.end_ms, tz=self._tz)
        return outcomes


@dataclass(slots=True)
class WeeklyTickReport:
    computed: int = 0
    skipped: int = 0
    insufficient: int = 0


class ReceptionWeeklyJob:
    """Monday tick: compute the completed week for every enabled chat."""

    def __init__(
        self,
        *,
        service: ReceptionMetricsService,
        chats: Callable[[], Iterable[str]],
        clock_ms: Callable[[], int] | None = None,
        run_hour: int = WEEKLY_RUN_HOUR,
    ) -> None:
        self._service = service
        self._chats = chats
        self._clock_ms = clock_ms or _now_ms
        self._run_hour = int(run_hour)

    async def tick(self, *, now_ms: int | None = None) -> WeeklyTickReport:
        moment = int(now_ms if now_ms is not None else self._clock_ms())
        report = WeeklyTickReport()
        tz = self._service.tz
        current = week_of(moment, tz=tz)
        if moment < week_moment_ms(current.label, hours=self._run_hour, tz=tz):
            return report
        target = previous_week(moment, tz=tz)
        for chat_id in dict.fromkeys(str(item) for item in self._chats() if str(item)):
            if self._service.store.has_weekly(chat_id=chat_id, iso_week=target.label):
                report.skipped += 1
                continue
            outcome = await self._service.compute_week(chat_id, target)
            report.computed += 1
            if outcome.status != "ok":
                report.insufficient += 1
        return report


#: Re-exported for callers that only need the local weekday label.
def local_day(moment_ms: int, tz: tzinfo | None = None) -> str:
    moment = datetime.fromtimestamp(int(moment_ms) / 1000, tz)
    return (moment if tz is not None else moment.astimezone()).date().isoformat()


__all__ = [
    "WEEKLY_RUN_HOUR",
    "ReceptionBounds",
    "ReceptionMetricsService",
    "ReceptionWeeklyJob",
    "WeeklyOutcome",
    "WeeklyTickReport",
    "iso_week",
    "local_day",
    "persona_sha",
    "reception_policy_snapshot",
]
