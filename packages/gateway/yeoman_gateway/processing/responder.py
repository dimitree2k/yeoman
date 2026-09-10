"""Responder wrapper that runs one thread's generation loop (Plan 03, task 3).

The wrapper exists for one reason: while a generation is in flight, a follow-up must be
accepted instead of starting a second answer path. The existing responder holds its session
lock across the whole provider call, so today a follow-up simply waits and then produces a
second reply - or worse, the first reply arrives stale.

Without an actor (processing disabled, unmanaged chat or an unknown thread) every call is
passed through unchanged, so the legacy path stays byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from yeoman_gateway.processing.actor import (
    MAX_ADDITIONAL_GENERATIONS,
    ThreadActorRegistry,
    thread_session_key,
)
from yeoman_gateway.processing.models import now_ms as _now_ms
from yeoman_gateway.processing.threads import TurnAuthority


class ThreadActorResponder:
    """Drives the generation loop for managed threads; a pass-through otherwise."""

    def __init__(
        self,
        *,
        inner: Any,
        actors: ThreadActorRegistry,
        store: Any,
        authority: TurnAuthority | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._inner = inner
        self._actors = actors
        self._store = store
        self._authority = authority or TurnAuthority()
        self._clock = clock or _now_ms

    def __getattr__(self, name: str) -> Any:
        """Everything else (tool access, close, telemetry) stays the inner responder's."""
        return getattr(self._inner, name)

    # -- port --------------------------------------------------------------------------

    async def generate_reply(self, event: Any, decision: Any) -> str | None:
        thread_id = self.thread_for_event(event)
        if thread_id is None:
            return await self._inner.generate_reply(event, decision)

        actor = self._actors.actor_for(thread_id)
        if actor.generation_in_flight:
            metadata = dict(getattr(event, "raw_metadata", {}) or {})
            admission = actor.accept(
                event_id=self._event_id(event),
                principal=str(getattr(event, "sender_id", "") or ""),
                kind=str(metadata.get("processing_kind") or "message"),
                explicit_correction=bool(metadata.get("explicit_correction")),
                authorized=self._is_orderer(actor, event),
            )
            logger.debug(
                "follow-up accepted during generation thread={} state={} waiting={}",
                thread_id,
                admission.state,
                admission.waiting,
            )
            # The running generation answers with the wider snapshot: no second answer path.
            return None

        return await self._run_loop(actor, event, decision)

    # -- loop --------------------------------------------------------------------------

    async def _run_loop(self, actor: Any, event: Any, decision: Any) -> str | None:
        session_key = self.session_key_for(event, thread_id=actor.thread_id)
        for _attempt in range(MAX_ADDITIONAL_GENERATIONS + 1):
            snapshot = actor.freeze_snapshot()
            if snapshot is None:
                return await self._inner.generate_reply(
                    event, decision, session_key=session_key
                )

            async def _call(_snapshot: Any) -> str | None:
                return await self._inner.generate_reply(
                    event, decision, session_key=session_key
                )

            outcome = await actor.run_generation(snapshot, _call)
            if outcome.state == "restart":
                continue
            if outcome.state in ("error", "superseded"):
                logger.debug(
                    "generation ended without a reply state={} turn={}",
                    outcome.state,
                    snapshot.turn_id,
                )
                return None
            return outcome.text
        return None

    # -- helpers -----------------------------------------------------------------------

    def thread_for_event(self, event: Any) -> str | None:
        """The thread the fast gate assigned to this event, if any."""
        event_id = self._event_id(event)
        if not event_id:
            return None
        try:
            assignment = self._store.event_assignment(event_id)
        except Exception:
            return None
        if assignment is None:
            return None
        thread_id = assignment[0]
        if not thread_id:
            return None
        thread = self._store.get_thread(thread_id)
        return thread_id if thread is not None else None

    def session_key_for(self, event: Any, *, thread_id: str) -> str:
        """Thread-scoped session key; the chat key stays for legacy callers.

        Group chats are strictly thread-scoped from now on. A DM keeps its chat-scoped
        history until the explicitly marked legacy carry-over exists, so the running pilot
        does not lose continuity (spec R03: thread context primary, no invented history).
        """
        channel = str(getattr(event, "channel", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        if not channel or not chat_id:
            return f"{channel}:{chat_id}"
        if str(chat_id).endswith("@g.us"):
            return thread_session_key(channel, chat_id, thread_id)
        return f"{channel}:{chat_id}"

    def _event_id(self, event: Any) -> str:
        return str(getattr(event, "message_id", "") or "")

    def _is_orderer(self, actor: Any, event: Any) -> bool:
        """Only the orderer or an authorised operator may supersede through a follow-up."""
        thread_id = getattr(actor, "thread_id", "")
        turn = self._store.active_turn(thread_id) if thread_id else None
        principal = str(getattr(event, "sender_id", "") or "")
        if turn is None:
            return False
        allowed, _reason = self._authority.may_modify(turn, principal)
        return allowed


__all__ = ["ThreadActorResponder"]
