"""Send budgets: a sliding window per chat, a soft deferral per thread (Plan 06, R08).

Two rules shape this module:

* **Sliding, not bucketed.** Capacity returns 60 s after each individual send, so a chat
  cannot burst six messages at 12:59 and six more at 13:00.
* **Attempt-idempotent.** A reservation is keyed by the attempt, so retrying the
  reservation for the same attempt does not spend twice, while a genuinely new attempt
  counts again. Consumption is persisted, so a process restart does not hand back
  capacity - including for effects whose outcome is still unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from yeoman_gateway.processing.models import DAY_MS  # noqa: F401  (re-exported for callers)

#: Reaction effects expire quickly; a normal reply may wait longer.
REACTION_TTL_MS = 30_000
REPLY_TTL_MS = 120_000

#: Default start values (spec R08).
CHAT_HARD_UNITS = 6
CHAT_HARD_WINDOW_MS = 60_000
THREAD_SOFT_UNITS = 2
THREAD_SOFT_WINDOW_MS = 10_000
OUTBOX_WAITING_PER_CHAT = 20


def available_units(
    attempts: Iterable[tuple[int, int]],
    now_ms: int,
    limit: int = CHAT_HARD_UNITS,
    window_ms: int = CHAT_HARD_WINDOW_MS,
) -> int:
    """Units still available in the sliding window. Pure time-window logic.

    ``attempts`` are ``(at_ms, units)`` pairs; only attempts inside
    ``(now_ms - window_ms, now_ms]`` count, so capacity returns gradually rather than
    at a minute boundary.
    """
    spent = sum(
        units for at_ms, units in attempts if now_ms - window_ms < at_ms <= now_ms
    )
    return max(0, int(limit) - spent)


def next_ready_ms(
    attempts: Sequence[tuple[int, int]],
    now_ms: int,
    limit: int = CHAT_HARD_UNITS,
    window_ms: int = CHAT_HARD_WINDOW_MS,
) -> int:
    """When the next unit becomes available: deterministic, or ``now_ms`` if free now."""
    ordered = sorted(
        (int(at_ms), int(units))
        for at_ms, units in attempts
        if now_ms - window_ms < at_ms <= now_ms
    )
    if available_units(ordered, now_ms, limit, window_ms) > 0:
        return int(now_ms)
    spent = 0
    for at_ms, units in ordered:
        spent += units
        if spent >= limit:
            return int(at_ms) + int(window_ms)
    return int(now_ms) + int(window_ms)


def effect_ttl_ms(capability: str) -> int:
    """Deadline for a queued effect of this capability."""
    return REACTION_TTL_MS if str(capability) == "reaction" else REPLY_TTL_MS


def within_deadline(*, queued_ms: int, now_ms: int, ttl_ms: int) -> bool:
    return int(now_ms) - int(queued_ms) < int(ttl_ms)


@dataclass(slots=True)
class BudgetDecision:
    """Outcome of a reservation attempt: allowed, or defer until a known time."""

    allowed: bool
    ready_at_ms: int = 0
    reason: str = ""

    @property
    def deferred(self) -> bool:
        return not self.allowed


class ChatBudget:
    """Hard per-chat budget backed by the processing store (persisted, attempt-keyed)."""

    def __init__(
        self,
        store: object,
        *,
        limit: int = CHAT_HARD_UNITS,
        window_ms: int = CHAT_HARD_WINDOW_MS,
        waiting_cap: int = OUTBOX_WAITING_PER_CHAT,
    ) -> None:
        self._store = store
        self._limit = int(limit)
        self._window_ms = int(window_ms)
        self._waiting_cap = int(waiting_cap)

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_ms(self) -> int:
        return self._window_ms

    @property
    def waiting_cap(self) -> int:
        return self._waiting_cap

    def spent(self, *, channel: str, chat_id: str, now_ms: int) -> int:
        attempts = self._attempts(channel=channel, chat_id=chat_id, now_ms=now_ms)
        return self._limit - available_units(attempts, now_ms, self._limit, self._window_ms)

    def available(self, *, channel: str, chat_id: str, now_ms: int) -> int:
        return available_units(
            self._attempts(channel=channel, chat_id=chat_id, now_ms=now_ms),
            now_ms,
            self._limit,
            self._window_ms,
        )

    def next_ready_ms(self, *, channel: str, chat_id: str, now_ms: int) -> int:
        return next_ready_ms(
            self._attempts(channel=channel, chat_id=chat_id, now_ms=now_ms),
            now_ms,
            self._limit,
            self._window_ms,
        )

    def reserve(
        self,
        *,
        channel: str,
        chat_id: str,
        effect_id: str,
        attempt_id: str,
        units: int = 1,
        now_ms: int,
    ) -> BudgetDecision:
        """Atomically reserve capacity. The same attempt never spends twice."""
        wanted = max(1, int(units))
        allowed = self._store.reserve_send_budget(
            attempt_id=str(attempt_id),
            channel=str(channel),
            chat_id=str(chat_id),
            effect_id=str(effect_id),
            units=wanted,
            now_ms=int(now_ms),
            limit=self._limit,
            window_ms=self._window_ms,
        )
        if allowed:
            return BudgetDecision(True)
        return BudgetDecision(
            False,
            ready_at_ms=self.next_ready_ms(channel=channel, chat_id=chat_id, now_ms=now_ms),
            reason="budget_exhausted",
        )

    def _attempts(
        self, *, channel: str, chat_id: str, now_ms: int
    ) -> list[tuple[int, int]]:
        return self._store.send_budget_attempts(
            channel=str(channel),
            chat_id=str(chat_id),
            since_ms=int(now_ms) - self._window_ms,
        )


@dataclass(slots=True)
class ThreadBudget:
    """Soft per-thread limit: it defers a thread, it never authorises or forbids."""

    limit: int = THREAD_SOFT_UNITS
    window_ms: int = THREAD_SOFT_WINDOW_MS
    clock: Callable[[], int] | None = None
    _recent: dict[str, list[int]] = field(default_factory=dict, init=False, repr=False)

    def note_send(self, *, thread_id: str, now_ms: int) -> None:
        stamps = [stamp for stamp in self._recent.get(str(thread_id), []) if now_ms - stamp < self.window_ms]
        stamps.append(int(now_ms))
        self._recent[str(thread_id)] = stamps

    def defer_until(self, *, thread_id: str, now_ms: int) -> int:
        """``now_ms`` when the thread may speak again, otherwise the earliest such time."""
        stamps = sorted(
            stamp
            for stamp in self._recent.get(str(thread_id), [])
            if now_ms - self.window_ms < stamp <= now_ms
        )
        if len(stamps) < self.limit:
            return int(now_ms)
        return stamps[len(stamps) - self.limit] + self.window_ms

    def round_robin(self, thread_ids: Sequence[str], *, now_ms: int) -> list[str]:
        """Waiting threads, those deferred longest first. Simple fairness, no priority."""
        ready = [tid for tid in thread_ids if self.defer_until(thread_id=tid, now_ms=now_ms) <= now_ms]
        waiting = [tid for tid in thread_ids if tid not in ready]
        waiting.sort(key=lambda tid: self.defer_until(thread_id=tid, now_ms=now_ms))
        return ready + waiting
