"""Bounded, coalesced opportunity scheduling beside the processing coordinator.

Observers must never await model work. A trigger callback hands a
:class:`ParticipationOpportunity` to :class:`OpportunityScheduler`, which merges it
into one bounded pending record per chat and returns immediately. A fixed worker
pool then calls the injected handler.

What this module deliberately is *not*: a second generation actor, a distributed
broker, a thread engine or a debounce replacement. The channel already debounced the
batch; the scheduler only coalesces. One chat never has two concurrent handlers, and
no global mutex is held across a handler call.

Bounds are part of the contract (spec section 7): at most ``max_pending_chats``
pending chats, ``max_pending_source_refs`` and ``max_pending_source_bytes`` per
pending record, ``ttl_seconds`` from the oldest trigger, and a fixed number of
workers. Every overflow, expiry, merge and cancellation is reported through the
disposition callback instead of silently disappearing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.processing.participation import (
    ParticipationOpportunity,
)

#: Bumped when the opportunity identity scheme changes; it is part of the hash.
OPPORTUNITY_ID_VERSION = "p1"

DispositionCallback = Callable[[str, ParticipationOpportunity, dict[str, Any]], None]


class OpportunityHandler(Protocol):
    async def __call__(self, opportunity: ParticipationOpportunity) -> Any: ...


def opportunity_id_for(
    *,
    channel: str,
    chat_id: str,
    activation_epoch: int,
    lane: str,
    source_event_ids: Sequence[str],
    observed_revision: int,
) -> str:
    """Deterministic identity for one retained source set.

    Trigger type is deliberately excluded: the same sources must not run once as
    ``inbound`` and again as ``burst``/``lull`` (spec section 7.1).
    """
    material = "|".join(
        [
            OPPORTUNITY_ID_VERSION,
            str(channel),
            str(chat_id),
            str(int(activation_epoch)),
            str(lane),
            ",".join(sorted(str(item) for item in source_event_ids)),
            str(int(observed_revision)),
        ]
    )
    return "opp-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class _PendingChat:
    """One bounded pending record per chat: source references, never copied bodies.

    ``source_event_ids`` is every retained reference for the current evaluation;
    ``reported`` marks the references a handler has already been given. A trigger
    that adds no new material is a duplicate, and a trigger that arrives while a
    handler runs stays pending for the next evaluation instead of being lost.
    """

    channel: str
    chat_id: str
    activation_epoch: int
    lane: str
    source_event_ids: list[str] = field(default_factory=list)
    source_bytes: int = 0
    observed_revision: int = 0
    trigger: str = "inbound"
    first_trigger_ms: int = 0
    last_trigger_ms: int = 0
    dropped_refs: int = 0
    queued: bool = False
    reported: set[str] = field(default_factory=set)

    def key(self) -> tuple[str, str]:
        return (self.channel, self.chat_id)

    def unreported(self) -> list[str]:
        return [item for item in self.source_event_ids if item not in self.reported]


@dataclass(frozen=True, slots=True)
class OfferResult:
    """Synchronous admission details for the caller that owns source claims.

    Dropped IDs are transient result data, not scheduler state. The pending record
    retains only its bounded sources and an aggregate drop count.
    """

    accepted: bool
    retained_source_event_ids: tuple[str, ...] = ()
    dropped_source_event_ids: tuple[str, ...] = ()
    dropped_source_count: int = 0


class OpportunityScheduler:
    """A ready queue, a per-chat pending map, an active-chat set, fixed workers."""

    def __init__(
        self,
        *,
        handle: OpportunityHandler,
        max_pending_chats: int = 64,
        max_concurrent_decisions: int = 2,
        max_pending_source_refs: int = 64,
        max_pending_source_bytes: int = 16_384,
        ttl_seconds: int = 120,
        on_disposition: DispositionCallback | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_pending_chats < 1:
            raise ValueError("max_pending_chats must be positive")
        if max_concurrent_decisions < 1:
            raise ValueError("max_concurrent_decisions must be positive")
        if max_pending_source_refs < 1:
            raise ValueError("max_pending_source_refs must be positive")
        if max_pending_source_bytes < 1:
            raise ValueError("max_pending_source_bytes must be positive")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._handle = handle
        self._max_pending_chats = int(max_pending_chats)
        self._max_concurrent = int(max_concurrent_decisions)
        self._max_refs = int(max_pending_source_refs)
        self._max_bytes = int(max_pending_source_bytes)
        self._ttl_ms = int(ttl_seconds) * 1000
        self._on_disposition = on_disposition
        self._clock_ms = clock_ms or _default_clock_ms
        self._pending: dict[tuple[str, str], _PendingChat] = {}
        self._ready: deque[tuple[str, str]] = deque()
        self._active: set[tuple[str, str]] = set()
        #: Highest revision already handed to the handler, per lane and chat. It is
        #: the in-process duplicate watermark; the ledger holds the durable one.
        self._evaluated_revision: dict[tuple[str, str, str], int] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._running = False
        self._cancelled: set[tuple[str, str]] = set()
        self._counters: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def counters(self) -> dict[str, int]:
        """Disposition counters, for operational traces and tests."""
        return dict(self._counters)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(index)) for index in range(self._max_concurrent)
        ]

    async def stop(self) -> None:
        """Stop accepting work, cancel workers and wait for them to finish."""
        self._running = False
        self._wake.set()
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
        self._workers = []
        self._pending.clear()
        self._ready.clear()
        self._active.clear()
        self._cancelled.clear()

    # -- producer side -----------------------------------------------------------------

    def offer(self, opportunity: ParticipationOpportunity) -> bool:
        """Merge one opportunity and return immediately without model work."""
        return self.offer_with_result(opportunity).accepted

    def offer_with_result(self, opportunity: ParticipationOpportunity) -> OfferResult:
        """Merge one opportunity and return immediately. Never awaits model work.

        Returns ``False`` when the offer was dropped (queue full, cancelled chat or a
        revision already pending), after recording the reason. The detailed result is
        used by the source owner to release only unreported references truncated by a
        bound.
        """
        key = (str(opportunity.channel), str(opportunity.chat_id))
        lane_key = (*key, str(opportunity.lane))
        if key in self._cancelled:
            self._dispose("cancelled", opportunity, {"stage": "offer"})
            return OfferResult(False)
        record = self._pending.get(key)
        if record is None:
            if not any(
                str(item or "").strip() for item in opportunity.source_event_ids
            ):
                self._dispose("empty", opportunity, {"reason": "no_source_refs"})
                return OfferResult(False)
            if int(opportunity.observed_revision) <= self._evaluated_revision.get(lane_key, -1):
                # Same watermark as the evaluation that already ran: no new material.
                self._dispose("duplicate", opportunity, {"reason": "revision_considered"})
                return OfferResult(False)
            if len(self._pending) >= self._max_pending_chats:
                self._dispose("queue_full", opportunity, {"reason": "max_pending_chats"})
                return OfferResult(False)
            record = _PendingChat(
                channel=key[0],
                chat_id=key[1],
                activation_epoch=int(opportunity.activation_epoch),
                lane=str(opportunity.lane),
                first_trigger_ms=int(opportunity.created_at_ms),
                last_trigger_ms=int(opportunity.created_at_ms),
            )
            self._pending[key] = record
        elif int(opportunity.activation_epoch) != record.activation_epoch:
            # A different epoch is a different owner generation: replace, never merge.
            self._dispose(
                "cancelled",
                opportunity,
                {"reason": "epoch_changed", "previous": record.activation_epoch},
            )
            record.source_event_ids = []
            record.source_bytes = 0
            record.observed_revision = 0
            record.dropped_refs = 0
            record.activation_epoch = int(opportunity.activation_epoch)
            record.lane = str(opportunity.lane)
            record.first_trigger_ms = int(opportunity.created_at_ms)

        offered = [
            str(item or "").strip() for item in opportunity.source_event_ids if str(item or "").strip()
        ]
        if not offered:
            # Trigger labels are metadata; a trigger without material is not work.
            self._pending.pop(key, None)
            self._dispose("empty", opportunity, {"reason": "no_source_refs"})
            return OfferResult(False)
        added, dropped_source_ids, dropped_source_count = self._merge_sources(record, offered)
        if not added:
            # Repeated trigger over the same watermark: no new material, no new decision.
            self._dispose(
                "duplicate",
                opportunity,
                {"reason": "no_new_sources", "dropped_source_count": dropped_source_count},
            )
            if key not in self._active and not record.unreported():
                self._pending.pop(key, None)
            return OfferResult(
                False,
                tuple(record.source_event_ids),
                tuple(dropped_source_ids),
                dropped_source_count,
            )
        record.observed_revision = max(record.observed_revision, int(opportunity.observed_revision))
        record.last_trigger_ms = int(opportunity.created_at_ms)
        record.trigger = str(opportunity.trigger)
        if not record.queued:
            record.queued = True
            self._ready.append(key)
            self._wake.set()
            self._counters["coalesced"] = self._counters.get("coalesced", 0) + 1
        return OfferResult(
            True,
            tuple(record.source_event_ids),
            tuple(dropped_source_ids),
            dropped_source_count,
        )

    def cancel_chat(self, channel: str, chat_id: str, *, reason: str = "cancelled") -> bool:
        """Cancel pending speculation for one chat (a direct request supersedes it).

        A handler that is already running is not killed here; the final effect
        authorizer rejects its stale work at transport time.
        """
        key = (str(channel), str(chat_id))
        record = self._pending.pop(key, None)
        self._cancelled.add(key)
        if record is not None:
            self._dispose(
                "cancelled",
                self._as_opportunity(record),
                {"reason": reason, "stage": "pending"},
            )
            return True
        return False

    def release_chat(self, channel: str, chat_id: str) -> None:
        """Allow a chat to be scheduled again after a direct request finished."""
        self._cancelled.discard((str(channel), str(chat_id)))

    def mark_considered(
        self, channel: str, chat_id: str, *, observed_revision: int, lane: str = "production"
    ) -> None:
        """Record the durable watermark after a restart (ledger-backed by callers)."""
        key = (str(channel), str(chat_id), str(lane))
        self._evaluated_revision[key] = max(
            self._evaluated_revision.get(key, -1), int(observed_revision)
        )

    def _merge_sources(
        self, record: _PendingChat, source_event_ids: Iterable[str]
    ) -> tuple[int, tuple[str, ...], int]:
        """Merge bounded source references, newest kept.

        The returned IDs are only the unreported references dropped by this merge;
        ``dropped_source_count`` includes every dropped reference for disposition
        accounting, including references already handed to a handler.
        """
        seen = set(record.source_event_ids)
        added = 0
        dropped_source_ids: list[str] = []
        dropped_source_count = 0
        for raw in source_event_ids:
            token = str(raw or "").strip()
            if not token or token in seen:
                continue
            size = len(token.encode("utf-8"))
            if len(record.source_event_ids) >= self._max_refs:
                dropped, was_reported = self._drop_oldest(record)
                if dropped is not None and not was_reported:
                    dropped_source_ids.append(dropped)
                record.dropped_refs += 1
                dropped_source_count += 1
            if record.source_bytes + size > self._max_bytes:
                while record.source_bytes + size > self._max_bytes and record.source_event_ids:
                    dropped, was_reported = self._drop_oldest(record)
                    if dropped is not None and not was_reported:
                        dropped_source_ids.append(dropped)
                    record.dropped_refs += 1
                    dropped_source_count += 1
                if record.source_bytes + size > self._max_bytes:
                    dropped_source_ids.append(token)
                    record.dropped_refs += 1
                    dropped_source_count += 1
                    continue
            record.source_event_ids.append(token)
            record.source_bytes += size
            seen.add(token)
            added += 1
        return added, tuple(dropped_source_ids), dropped_source_count

    @staticmethod
    def _drop_oldest(record: _PendingChat) -> tuple[str | None, bool]:
        if not record.source_event_ids:
            return None, False
        oldest = record.source_event_ids.pop(0)
        was_reported = oldest in record.reported
        record.reported.discard(oldest)
        record.source_bytes = max(0, record.source_bytes - len(oldest.encode("utf-8")))
        return oldest, was_reported

    # -- worker side -------------------------------------------------------------------

    async def _next_ready(self) -> tuple[str, str] | None:
        """Wait for ready work without busy polling and without holding a global lock."""
        while self._running:
            if self._ready:
                return self._ready.popleft()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.25)
            except TimeoutError:
                continue
        return None

    async def _worker(self, index: int) -> None:
        del index  # worker identity is only diagnostic
        while self._running:
            key = await self._next_ready()
            if key is None:
                return
            record = self._pending.get(key)
            if record is None:
                continue
            record.queued = False
            if key in self._active:
                # The active handler's _retire() queues its merged follow-up.
                continue
            self._active.add(key)
            try:
                await self._run_one(record)
            finally:
                self._active.discard(key)
                lane_key = (record.channel, record.chat_id, str(record.lane))
                self._evaluated_revision[lane_key] = max(
                    self._evaluated_revision.get(lane_key, -1), int(record.observed_revision)
                )
                self._retire(record)

    def _retire(self, record: _PendingChat) -> None:
        """Drop a finished record, or requeue the material that arrived meanwhile."""
        key = record.key()
        if self._pending.get(key) is not record:
            return
        if record.unreported():
            if not record.queued:
                record.queued = True
                self._ready.append(key)
                self._wake.set()
            return
        self._pending.pop(key, None)

    async def _run_one(self, record: _PendingChat) -> None:
        opportunity = self._as_opportunity(record)
        record.reported.update(record.source_event_ids)
        now = int(self._clock_ms())
        if now - record.first_trigger_ms > self._ttl_ms:
            self._dispose(
                "expired",
                opportunity,
                {"age_ms": now - record.first_trigger_ms, "ttl_ms": self._ttl_ms},
            )
            return
        if self._on_disposition is not None:
            self._on_disposition(
                "started",
                opportunity,
                {
                    "trigger": record.trigger,
                    "dropped_source_count": record.dropped_refs,
                    "source_high_watermark": int(record.observed_revision),
                },
            )
        try:
            await self._handle(opportunity)
        except asyncio.CancelledError:
            self._dispose("cancelled", opportunity, {"stage": "handler"})
            raise
        except Exception as exc:  # noqa: BLE001 - one bad chat must not stop the pool
            logger.warning(
                "opportunity_handler_failed chat={} error_type={}",
                record.chat_id,
                type(exc).__name__,
            )
            self._dispose(
                "handler_failed", opportunity, {"error_type": type(exc).__name__}
            )

    def _as_opportunity(self, record: _PendingChat) -> ParticipationOpportunity:
        # Only the material this evaluation has not seen yet: the previous call
        # already considered the rest, and re-reporting it would double-charge the
        # attempt budget for unchanged activity.
        retained = record.unreported() or list(record.source_event_ids)
        return ParticipationOpportunity(
            opportunity_id=opportunity_id_for(
                channel=record.channel,
                chat_id=record.chat_id,
                activation_epoch=record.activation_epoch,
                lane=record.lane,
                source_event_ids=retained,
                observed_revision=record.observed_revision,
            ),
            channel=record.channel,
            chat_id=record.chat_id,
            trigger=record.trigger,
            source_event_ids=tuple(retained),
            observed_revision=int(record.observed_revision),
            activation_epoch=int(record.activation_epoch),
            created_at_ms=int(record.first_trigger_ms),
            lane=str(record.lane),
        )

    def _dispose(
        self, disposition: str, opportunity: ParticipationOpportunity, detail: dict[str, Any]
    ) -> None:
        self._counters[disposition] = self._counters.get(disposition, 0) + 1
        if self._on_disposition is None:
            return
        try:
            self._on_disposition(disposition, opportunity, detail)
        except Exception as exc:  # noqa: BLE001 - recording must not break scheduling
            logger.warning("opportunity_disposition_failed error_type={}", type(exc).__name__)


def _default_clock_ms() -> int:
    import time

    return int(time.time() * 1000)


__all__ = [
    "OPPORTUNITY_ID_VERSION",
    "OfferResult",
    "OpportunityScheduler",
    "opportunity_id_for",
]
