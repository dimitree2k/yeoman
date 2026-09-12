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
from yeoman_gateway.processing.dispatch import CURRENT_TURN
from yeoman_gateway.processing.models import TurnBinding
from yeoman_gateway.processing.models import now_ms as _now_ms
from yeoman_gateway.processing.threads import TurnAuthority

#: Marked, bounded carry-over of the chat-scoped DM history into a thread session.
#: The chat session itself is never modified, so the change is reversible by key only.
LEGACY_CONTEXT_MARKER = "[legacy chat context - not thread-bound]"
LEGACY_CONTEXT_TURNS = 20
LEGACY_CONTEXT_MAX_CHARS = 6000


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
        router: Any | None = None,
    ) -> None:
        self._inner = inner
        self._actors = actors
        self._store = store
        self._authority = authority or TurnAuthority()
        self._clock = clock or _now_ms
        self._router = router

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

        try:
            return await self._run_loop(actor, event, decision)
        except Exception as exc:
            # The actor must never cost a reply: degrade to the plain path and say so.
            logger.warning(
                "threads_degraded thread_id={} error_type={}",
                thread_id,
                type(exc).__name__,
            )
            return await self._inner.generate_reply(event, decision)

    # -- loop --------------------------------------------------------------------------

    async def _run_loop(self, actor: Any, event: Any, decision: Any) -> str | None:
        session_key = self.session_key_for(event, thread_id=actor.thread_id)
        self._ensure_legacy_context(
            channel=str(getattr(event, "channel", "") or ""),
            chat_id=str(getattr(event, "chat_id", "") or ""),
            session_key=session_key,
        )
        for _attempt in range(MAX_ADDITIONAL_GENERATIONS + 1):
            snapshot = actor.freeze_snapshot()
            if snapshot is None:
                return await self._inner.generate_reply(
                    event, decision, session_key=session_key
                )

            # Tool-produced effects of this generation must carry the *frozen* turn and
            # revision, so a correction during the call invalidates them instead of a
            # later turn silently authorising them.
            turn = self._store.get_turn(snapshot.turn_id)
            binding = (
                TurnBinding(turn=turn, trace_id=snapshot.turn_id, generation_id=snapshot.generation_id)
                if turn is not None
                else None
            )

            async def _call(_snapshot: Any) -> str | None:
                # Review F04: a restart must actually put the follow-up into the request.
                # Re-sending the original event made the actor consume the pending input
                # without the provider ever seeing it.
                request_event = self._request_event(event, _snapshot)
                token = CURRENT_TURN.set(binding)
                try:
                    return await self._inner.generate_reply(
                        request_event, decision, session_key=session_key
                    )
                finally:
                    CURRENT_TURN.reset(token)

            outcome = await actor.run_generation(snapshot, _call)
            if binding is not None and getattr(self, "_router", None) is not None:
                # The final reply is dispatched by the orchestrator *after* this scope, so
                # remember the frozen turn against the source message (review F02).
                self._router.remember_turn_for_source(self._event_id(event), binding)
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

    def _request_event(self, event: Any, snapshot: Any) -> Any:
        """The event a generation should see: the turn's sources, in order.

        On a restart the snapshot carries the follow-up sources as well, so the request is
        rebuilt from them. Without journal text for any source the original event is kept -
        an unreadable source must not silently empty the request.
        """
        refs = tuple(getattr(snapshot, "source_refs", ()) or ())
        if not refs:
            return event
        texts: list[str] = []
        for ref in refs:
            event_id = str(getattr(ref, "event_id", "") or "")
            if not event_id:
                continue
            source = self._store.get_event(event_id)
            payload = getattr(source, "payload", None)
            text = ""
            if isinstance(payload, dict):
                text = str(payload.get("text") or "").strip()
            if text:
                texts.append(text)
        if not texts:
            return event
        content = "\n".join(texts)
        original = str(getattr(event, "content", "") or "")
        if (
            len(refs) == 1
            and str(getattr(refs[0], "event_id", "") or "") == self._event_id(event)
            and original
            and content
            and original.endswith(content)
        ):
            return event
        if content == original:
            return event
        try:
            from dataclasses import replace as dataclass_replace

            return dataclass_replace(event, content=content)
        except Exception:  # pragma: no cover - event shapes without dataclass semantics
            logger.debug("request event could not be rebuilt; keeping the original")
            return event

    # -- helpers -----------------------------------------------------------------------

    def thread_for_event(self, event: Any) -> str | None:
        """The thread the fast gate assigned to this event, if any."""
        event_id = self._event_id(event)
        if not event_id:
            return None
        try:
            assignment = self._store.event_assignment(event_id)
        except Exception as exc:
            logger.warning(
                "assignment_unavailable event_id={} error_type={}", event_id, type(exc).__name__
            )
            return None
        if assignment is None:
            return None
        thread_id = assignment[0]
        if not thread_id:
            logger.debug("assignment_unavailable event_id={} reason=no_thread", event_id)
            return None
        thread = self._store.get_thread(thread_id)
        if thread is None:
            logger.warning(
                "assignment_unavailable event_id={} thread_id={} reason=unknown_thread",
                event_id,
                thread_id,
            )
            return None
        return thread_id

    def session_key_for(self, event: Any, *, thread_id: str) -> str:
        """Thread-scoped session key, for groups and DMs alike (spec R03)."""
        channel = str(getattr(event, "channel", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        if not channel or not chat_id:
            return f"{channel}:{chat_id}"
        return thread_session_key(channel, chat_id, thread_id)

    def _ensure_legacy_context(
        self, *, channel: str, chat_id: str, session_key: str
    ) -> None:
        """Copy the chat-scoped history once into a thread session, clearly marked.

        The old chat session stays untouched, the copy is bounded in turns and characters,
        and a marker makes it idempotent. Only the same principal's DM is carried over, so
        no rights are mixed; groups never use this path.
        """
        sessions = getattr(self._inner, "sessions", None)
        if sessions is None or not channel or not chat_id:
            return
        chat_key = f"{channel}:{chat_id}"
        if chat_key == session_key or str(chat_id).endswith("@g.us"):
            return
        try:
            thread_session = sessions.get_or_create(session_key)
            if any(
                LEGACY_CONTEXT_MARKER in str(message.get("content") or "")
                for message in thread_session.messages
            ):
                return
            chat_session = sessions.get_or_create(chat_key)
            history = [
                message
                for message in chat_session.messages
                if str(message.get("content") or "").strip()
            ][-LEGACY_CONTEXT_TURNS:]
            if not history:
                return
            lines: list[str] = []
            total = 0
            for message in reversed(history):
                text = " ".join(str(message.get("content") or "").split())[:400]
                if not text:
                    continue
                line = f"{message.get('role')}: {text}"
                if total + len(line) > LEGACY_CONTEXT_MAX_CHARS:
                    break
                lines.append(line)
                total += len(line)
            if not lines:
                return
            thread_session.add_message(
                "system", LEGACY_CONTEXT_MARKER + "\n" + "\n".join(reversed(lines))
            )
            sessions.save(thread_session)
        except Exception as exc:  # continuity is best effort, never fatal
            logger.warning(
                "legacy_context_carryover_failed chat={} error_type={}",
                chat_id,
                type(exc).__name__,
            )

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
