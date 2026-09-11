"""Turn and fact invalidation for provider signals (review F05).

An edit or a deletion used to be journaled and then forgotten: the derived turn kept its
revision, already queued effects stayed valid, and facts extracted from the deleted text
stayed readable. Both helpers existed but were only ever called from tests.

Two rules keep this narrow:

* **Only the author's own signal invalidates.** A third party's edit or delete of someone
  else's message is journaled as evidence and refused as a command, never treated as
  authority over another principal's work.
* **Invalidation never guesses.** Without a journaled source event and a resolvable turn
  there is nothing to invalidate, and the refusal is recorded instead of approximated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from loguru import logger

from yeoman_gateway.processing.signals import WhatsAppSignalMapper

#: Kinds that can invalidate prior work. A reaction or receipt never can.
INVALIDATING_KINDS: tuple[str, ...] = ("edit", "delete")


@dataclass(slots=True)
class SignalInvalidation:
    """What one signal invalidated - or why it did nothing."""

    kind: str = ""
    source_event_ids: tuple[str, ...] = ()
    turn_id: str = ""
    turn_revision: int = 0
    effects_cancelled: tuple[str, ...] = ()
    facts_revoked: tuple[str, ...] = ()
    facts_superseded: tuple[str, ...] = ()
    jobs_cancelled: int = 0
    refused: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.source_event_ids) and not self.refused


class SignalInvalidator:
    """Turns an authorized edit/delete signal into invalidation of turns and facts."""

    def __init__(
        self,
        *,
        store: Any,
        actors: Any | None = None,
        memory: Any | None = None,
        mapper: WhatsAppSignalMapper | None = None,
        clock: Any = None,
    ) -> None:
        self._store = store
        self._actors = actors
        self._memory = memory
        self._mapper = mapper or WhatsAppSignalMapper()
        self._clock = clock

    def __call__(self, kind: str, payload: Mapping[str, Any]) -> SignalInvalidation:
        if str(kind) not in INVALIDATING_KINDS:
            return SignalInvalidation(kind=str(kind), refused="not_an_invalidating_signal")
        signal = self._mapper.map(payload, kind=kind)
        if signal is None:
            return SignalInvalidation(kind=str(kind), refused="unmappable_signal")

        target_message_id = str(signal.target_message_id or signal.source_message_id or "")
        if not target_message_id:
            return SignalInvalidation(kind=str(kind), refused="no_target_message")

        originals = [
            event
            for event in self._store.events_by_source_message(target_message_id)
            if str(getattr(event, "kind", "")) == "message"
        ]
        if not originals:
            return SignalInvalidation(kind=str(kind), refused="no_journaled_source")

        author = str(signal.principal or "")
        mine = [event for event in originals if str(getattr(event, "principal", "")) == author]
        if not author or not mine:
            # A third party may not edit or delete another principal's work. The signal is
            # still journaled as evidence by the sink; it just carries no authority.
            logger.info(
                "signal invalidation refused kind={} message={} sender_is_not_author",
                kind,
                target_message_id,
            )
            return SignalInvalidation(kind=str(kind), refused="sender_is_not_author")

        newest = mine[-1]
        now = int(self._clock()) if self._clock is not None else None
        result = SignalInvalidation(
            kind=str(kind), source_event_ids=tuple(str(event.event_id) for event in mine)
        )

        turn_id = str(getattr(newest, "turn_id", "") or "")
        thread_id = str(getattr(newest, "thread_id", "") or "")
        channel = str(getattr(newest, "channel", "") or "")
        chat_id = str(getattr(newest, "chat_id", "") or "")
        if turn_id:
            result.turn_id = turn_id
            result.effects_cancelled = self._invalidate_turn(
                turn_id=turn_id,
                thread_id=thread_id,
                principal=author,
                channel=channel,
                chat_id=chat_id,
                kind=str(kind),
            )
            result.turn_revision = self._turn_revision(turn_id)

        if self._memory is not None:
            try:
                report = self._memory.invalidate_sources(
                    list(result.source_event_ids),
                    now_ms=int(now if now is not None else 0),
                    kind="edit" if str(kind) == "edit" else "delete",
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("fact invalidation failed kind={} error={}", kind, exc)
            else:
                result.facts_revoked = tuple(getattr(report, "revoked", ()) or ())
                result.facts_superseded = tuple(getattr(report, "superseded", ()) or ())
                result.jobs_cancelled = int(getattr(report, "jobs_cancelled", 0) or 0)

        logger.info(
            "signal invalidation kind={} message={} turn={} revision={} cancelled={} "
            "revoked={} superseded={} jobs={}",
            kind,
            target_message_id,
            result.turn_id or "-",
            result.turn_revision,
            len(result.effects_cancelled),
            len(result.facts_revoked),
            len(result.facts_superseded),
            result.jobs_cancelled,
        )
        return result

    def _invalidate_turn(
        self,
        *,
        turn_id: str,
        thread_id: str,
        principal: str,
        channel: str,
        chat_id: str,
        kind: str,
    ) -> tuple[str, ...]:
        """Raise the revision and cancel effects that were queued for the old one."""
        before = self._queued_effect_ids(turn_id)
        if self._actors is not None and thread_id:
            try:
                actor = self._actors.actor_for(thread_id)
                actor.correct_turn(
                    turn_id,
                    principal,
                    channel=channel,
                    chat_id=chat_id,
                    reason="deleted" if kind == "delete" else "edited",
                )
                return tuple(before)
            except Exception as exc:
                # Authority refusals and unknown turns are expected outcomes, not crashes:
                # fall through to the store-level invalidation below.
                logger.debug("actor correction refused turn={} error={}", turn_id, exc)
        turn = self._store.get_turn(turn_id)
        if turn is None:
            return ()
        ref = self._store.bump_turn_revision(
            turn_id,
            expected_revision=turn.revision,
            now_ms=int(self._clock()) if self._clock is not None else 0,
            reason="deleted" if kind == "delete" else "edited",
        )
        self._store.cancel_stale_effects(
            turn_id,
            current_revision=ref.revision,
            now_ms=int(self._clock()) if self._clock is not None else 0,
            reason="superseded",
        )
        return tuple(before)

    def _queued_effect_ids(self, turn_id: str) -> list[str]:
        try:
            effects = self._store.list_effects(turn_id=turn_id)
        except TypeError:  # pragma: no cover - older signature
            effects = [
                effect
                for effect in self._store.list_effects(states=("queued", "executing"))
                if getattr(effect, "turn_id", "") == turn_id
            ]
        return [
            str(effect.effect_id)
            for effect in effects
            if str(getattr(effect, "state", "")) in ("queued", "executing", "planned")
        ]

    def _turn_revision(self, turn_id: str) -> int:
        turn = self._store.get_turn(turn_id)
        return int(getattr(turn, "revision", 0) or 0) if turn is not None else 0


__all__ = ["INVALIDATING_KINDS", "SignalInvalidation", "SignalInvalidator"]
