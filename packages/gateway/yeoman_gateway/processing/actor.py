"""Thread actor: serialised state, a bounded postbox and immutable generations.

Two different things are deliberately not confused here:

* the **state lock** guards short, synchronous mutations (freeze a snapshot, write a
  postbox row, bump a revision, evaluate a drain) and is never held across an ``await``,
* the **generation slot** (``asyncio.Semaphore``) is held across the provider call on
  purpose, because it bounds concurrency, not state access.

That is what lets a follow-up arrive while a provider request is in flight - the existing
responder holds its session lock across the whole call, so a follow-up currently waits
until the request finished.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from yeoman_gateway.processing.models import (
    CanonicalEvent,
    GenerationSnapshot,
    PendingInput,
    ProcessingError,
    SourceRef,
    StoredTurn,
    TurnRef,
    UpdateEffect,
    canonical_hash,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)
from yeoman_gateway.processing.threads import ThreadPolicy, TurnAuthority, classify_update

#: Spec section 4: at most two additional generations per turn, then remaining inputs
#: continue as the next turn of the same thread instead of restarting forever.
MAX_ADDITIONAL_GENERATIONS = 2


@dataclass(frozen=True, slots=True)
class Admission:
    """Result of accepting one follow-up into the postbox."""

    state: str  # accepted | deferred | observed
    input_id: str | None = None
    thread_id: str | None = None
    waiting: int = 0

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """What the postbox evaluation decided after the provider call returned."""

    state: str  # send | restart | superseded | error
    text: str | None = None
    generation_id: str | None = None
    detail: str | None = None
    pending: tuple[PendingInput, ...] = ()
    context_version: int | None = None
    revision: int | None = None
    followup_turn_id: str | None = None


@dataclass
class _ActorState:
    additional_generations: int = 0
    last_snapshot: GenerationSnapshot | None = None
    last_text: str | None = None
    pending_seen: list[str] = field(default_factory=list)


class ThreadActor:
    """Serialises one thread's state while keeping provider awaits lock-free."""

    def __init__(
        self,
        *,
        store: Any,
        thread_id: str,
        cap: int = 32,
        authority: TurnAuthority | None = None,
        generation_slots: asyncio.Semaphore | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._thread_id = thread_id
        self._cap = max(1, int(cap))
        self._authority = authority or TurnAuthority()
        self._slots = generation_slots
        self._clock = clock or _now_ms
        self._state_lock = threading.RLock()
        self._state = _ActorState()
        self._lock_owner: int | None = None
        self._generation_in_flight = False

    # -- diagnostics -------------------------------------------------------------------

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def state_lock_busy(self) -> bool:
        """True while a synchronous state mutation holds the lock (tests and logs)."""
        acquired = self._state_lock.acquire(blocking=False)
        if acquired:
            self._state_lock.release()
            return False
        return True

    @property
    def additional_generations(self) -> int:
        return self._state.additional_generations

    @property
    def generation_in_flight(self) -> bool:
        '''True while a provider call is running for this thread.'''
        return self._generation_in_flight

    # -- postbox -----------------------------------------------------------------------

    def accept(
        self,
        event: CanonicalEvent | None = None,
        *,
        input_id: str | None = None,
        event_id: str | None = None,
        principal: str = "",
        kind: str = "message",
        decision_id: str | None = None,
        explicit_correction: bool = False,
        authorized: bool = False,
        relevance: str = "unknown",
        turn_id: str | None = None,
    ) -> Admission:
        """Accept one follow-up. Returns immediately; never awaits."""
        resolved_event_id = event_id or (event.event_id if event is not None else None)
        if not resolved_event_id:
            raise ProcessingError("accept requires an event id")
        principal = principal or (event.principal if event is not None else "")
        kind = kind or (event.kind if event is not None else "message")

        effect = classify_update(
            kind=kind, explicit_correction=explicit_correction, authorized=authorized
        )
        with self._state_lock:
            if effect is UpdateEffect.OBSERVE and not authorized:
                # Another participant's hint is context, never a queued order.
                return Admission(state="observed", thread_id=self._thread_id)
            state = self._store.enqueue_pending_input(
                input_id=input_id or uuid.uuid4().hex,
                thread_id=self._thread_id,
                event_id=resolved_event_id,
                principal=principal,
                now_ms=self._clock(),
                turn_id=turn_id,
                kind=kind,
                decision_id=decision_id,
                relevance=relevance,
                cap=self._cap,
            )
            if state is None:
                return Admission(
                    state="observed",
                    thread_id=self._thread_id,
                    waiting=self._store.count_pending(thread_id=self._thread_id),
                )
            waiting = self._store.count_pending(thread_id=self._thread_id)
        # The row state is "waiting"; the admission surfaces it as accepted.
        return Admission(
            state="accepted" if state == "waiting" else state,
            thread_id=self._thread_id,
            waiting=waiting,
        )

    def pending(self) -> tuple[PendingInput, ...]:
        return self._store.pending_inputs(self._thread_id)

    # -- generations -------------------------------------------------------------------

    def freeze_snapshot(self, *, now_ms: int | None = None) -> GenerationSnapshot | None:
        """Freeze what a generation sees, before any provider call."""
        moment = self._clock() if now_ms is None else int(now_ms)
        with self._state_lock:
            turn = self._store.active_turn(self._thread_id)
            if turn is None:
                return None
            sources = self._store.turn_sources(turn.turn_id)
            active = tuple(
                SourceRef(
                    event_id=source.event_id,
                    source_message_id=source.source_message_id,
                    role=source.role,
                    revision_at_join=source.revision_at_join,
                )
                for source in sources
                if source.removed_ms is None
            )
            snapshot = GenerationSnapshot(
                generation_id=uuid.uuid4().hex,
                turn_id=turn.turn_id,
                thread_id=self._thread_id,
                revision=turn.revision,
                context_version=turn.context_version,
                source_refs=active,
                snapshot_hash=canonical_hash(
                    {
                        "turn_id": turn.turn_id,
                        "revision": turn.revision,
                        "context_version": turn.context_version,
                        "source_refs": [
                            [source.event_id, source.source_message_id, source.revision_at_join]
                            for source in active
                        ],
                    }
                ),
                created_ms=moment,
            )
            self._store.record_generation(snapshot)
            self._state.last_snapshot = snapshot
        return snapshot

    async def run_generation(
        self,
        snapshot: GenerationSnapshot,
        call: Callable[[GenerationSnapshot], Awaitable[str | None]],
    ) -> GenerationOutcome:
        """Run one provider generation without holding the state lock across the await."""
        self._generation_in_flight = True
        try:
            if self._slots is not None:
                async with self._slots:
                    text = await call(snapshot)
            else:
                text = await call(snapshot)
        except asyncio.CancelledError:
            self._store.finish_generation(
                snapshot.generation_id, now_ms=self._clock(), outcome="cancelled"
            )
            raise
        except Exception as exc:
            self._store.finish_generation(
                snapshot.generation_id,
                now_ms=self._clock(),
                outcome="error",
                detail=type(exc).__name__,
            )
            # A failed generation repeats no confirmed tool effect and sends nothing.
            return GenerationOutcome(
                state="error",
                generation_id=snapshot.generation_id,
                detail=type(exc).__name__,
            )
        finally:
            self._generation_in_flight = False

        with self._state_lock:
            return self._evaluate_postbox(snapshot, text)

    def _evaluate_postbox(
        self, snapshot: GenerationSnapshot, text: str | None
    ) -> GenerationOutcome:
        pending = self._store.drain_pending_inputs(
            self._thread_id, now_ms=self._clock(), limit=self._cap
        )
        self._state.pending_seen.extend(item.event_id for item in pending)
        turn = self._store.get_turn(snapshot.turn_id)

        superseding = [item for item in pending if item.kind in {"delete", "edit"}]
        if superseding or (turn is not None and turn.revision != snapshot.revision):
            revision = turn.revision if turn is not None else snapshot.revision
            if turn is not None and superseding:
                revision = self._bump_revision(turn, reason="supersede")
            self._store.cancel_stale_effects(
                snapshot.turn_id,
                current_revision=revision,
                now_ms=self._clock(),
                reason="superseded",
            )
            self._store.finish_generation(
                snapshot.generation_id, now_ms=self._clock(), outcome="superseded"
            )
            return GenerationOutcome(
                state="superseded",
                generation_id=snapshot.generation_id,
                pending=pending,
                revision=revision,
                detail="revision_superseded",
            )

        if pending:
            additional = self._state.additional_generations
            if additional < MAX_ADDITIONAL_GENERATIONS:
                self._state.additional_generations = additional + 1
                context_version = self._store.bump_context_version(
                    snapshot.turn_id, now_ms=self._clock()
                )
                self._store.finish_generation(
                    snapshot.generation_id, now_ms=self._clock(), outcome="restart"
                )
                return GenerationOutcome(
                    state="restart",
                    generation_id=snapshot.generation_id,
                    pending=pending,
                    context_version=context_version,
                    detail="new_context_available",
                )
            # Too many restarts: the rest continues as the next turn of this thread.
            followup_turn = self._open_followup_turn(pending)
            self._store.finish_generation(
                snapshot.generation_id,
                now_ms=self._clock(),
                outcome="handed_over",
                detail="max_additional_generations",
            )
            return GenerationOutcome(
                state="send",
                text=text,
                generation_id=snapshot.generation_id,
                pending=pending,
                followup_turn_id=followup_turn,
                detail="remaining_inputs_deferred_to_followup_turn",
            )

        self._store.finish_generation(
            snapshot.generation_id, now_ms=self._clock(), outcome="send"
        )
        return GenerationOutcome(state="send", text=text, generation_id=snapshot.generation_id)

    def _bump_revision(self, turn: StoredTurn, *, reason: str) -> int:
        ref = self._store.bump_turn_revision(
            turn.turn_id,
            expected_revision=turn.revision,
            now_ms=self._clock(),
            reason=reason,
        )
        return ref.revision

    def _open_followup_turn(self, pending: tuple[PendingInput, ...]) -> str | None:
        if not pending:
            return None
        followup = pending[0]
        turn_id = self._store.open_turn(
            thread_id=self._thread_id,
            principal=followup.principal,
            trigger_event_id=followup.event_id,
            now_ms=self._clock(),
        )
        for item in pending:
            self._store.add_turn_source(
                turn_id=turn_id,
                event_id=item.event_id,
                role="context",
                revision_at_join=1,
                now_ms=self._clock(),
            )
        self._store.promote_deferred(
            self._thread_id, now_ms=self._clock(), capacity=self._cap
        )
        return turn_id

    # -- authority ---------------------------------------------------------------------

    def correct_turn(
        self, turn_id: str, principal: str, *, channel: str = "", chat_id: str = "", reason: str = ""
    ) -> TurnRef:
        """Raise the revision of a turn after an authorised correction."""
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise ProcessingError(f"unknown turn: {turn_id}")
        allowed, why = self._authority.may_modify(
            turn, principal, channel=channel, chat_id=chat_id
        )
        if not allowed:
            raise ProcessingError(f"principal may not modify this turn ({why})")
        with self._state_lock:
            ref = self._store.bump_turn_revision(
                turn_id,
                expected_revision=turn.revision,
                now_ms=self._clock(),
                reason=reason or why,
            )
            self._store.cancel_stale_effects(
                turn_id,
                current_revision=ref.revision,
                now_ms=self._clock(),
                reason="superseded",
            )
        return ref

    def stop_turn(
        self, turn_id: str, principal: str, *, channel: str = "", chat_id: str = ""
    ) -> TurnRef:
        return self.correct_turn(
            turn_id, principal, channel=channel, chat_id=chat_id, reason="stop"
        )


class ThreadActorRegistry:
    """Builds actors on demand so the postbox survives a restart."""

    def __init__(
        self,
        *,
        store: Any,
        config: Any = None,
        authority: TurnAuthority | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._authority = authority
        self._clock = clock or _now_ms
        self._actors: dict[str, ThreadActor] = {}
        self._global_slots: asyncio.Semaphore | None = None
        self._policy = ThreadPolicy.from_config(config) if config is not None else ThreadPolicy()
        threads = getattr(config, "threads", None) or getattr(config, "Threads", None)
        self._max_global = int(getattr(threads, "max_generations_global", 1) or 1)
        self._cap = self._policy.pending_inputs_per_thread

    def actor_for(self, thread_id: str) -> ThreadActor:
        actor = self._actors.get(thread_id)
        if actor is None:
            if self._global_slots is None:
                self._global_slots = asyncio.Semaphore(max(1, self._max_global))
            actor = ThreadActor(
                store=self._store,
                thread_id=thread_id,
                cap=self._cap,
                authority=self._authority,
                generation_slots=self._global_slots,
                clock=self._clock,
            )
            self._actors[thread_id] = actor
        return actor

    def active_turn(self, channel: str, chat_id: str) -> TurnRef | None:
        """Turn provider for the dispatch line."""
        for thread in self._store.list_threads(chat_id=chat_id, state="open"):
            if thread.channel != channel:
                continue
            turn = self._store.active_turn(thread.thread_id)
            if turn is not None:
                return turn.to_ref(channel=channel, chat_id=chat_id)
        return None

    def tick(self, now_ms: int) -> tuple[str, ...]:
        """Close idle threads; called by the maintenance loop."""
        closed = self._store.close_idle_threads(now_ms, self._policy.idle_ms)
        for thread_id in closed:
            self._actors.pop(thread_id, None)
        return closed

    def forget(self, thread_id: str) -> None:
        self._actors.pop(thread_id, None)


def thread_session_key(channel: str, chat_id: str, thread_id: str) -> str:
    """Thread-scoped session key (spec R03: thread context is primary)."""
    return f"{channel}:{chat_id}:thread:{thread_id}"


__all__ = [
    "Admission",
    "GenerationOutcome",
    "MAX_ADDITIONAL_GENERATIONS",
    "ThreadActor",
    "ThreadActorRegistry",
    "thread_session_key",
]
