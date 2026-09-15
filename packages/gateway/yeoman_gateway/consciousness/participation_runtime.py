"""Who owns a source event, and how observers hand work to the scheduler.

Two things live here because they are the same decision seen from both sides:

* :class:`SourceOwner` is the durable, per-source arbitration between the legacy
  path and the participation lane. A production source is never actionable by both
  owners in one epoch (spec section 3.1).
* :class:`ParticipationRuntime` is the thin adapter an observer (burst, lull,
  channel) holds. Its ``offer_source`` does bounded local work and returns; it never
  runs the judge, the generator, maintenance or delivery.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
from yeoman_gateway.processing.participation import ParticipationOpportunity

#: Owner values persisted in the claim table.
OWNER_LEGACY = "legacy"
OWNER_PARTICIPATION = "participation"

#: Lanes. Only ``production`` may produce effects.
LANE_PRODUCTION = "production"
LANE_SHADOW = "shadow"


class ClaimStore(Protocol):
    """The persisted claim/watermark owner (``SpeakupLog`` in production)."""

    def claim_source_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_id: str,
        activation_epoch: int,
        lane: str,
        owner: str,
        now_ms: int,
    ) -> tuple[bool, str]: ...


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    """The outcome of one claim attempt, with a stable reason for logs and tests."""

    granted: bool
    owner: str
    reason: str

    @property
    def is_legacy(self) -> bool:
        return self.owner == OWNER_LEGACY


class SourceOwner:
    """Per-source exclusive ownership between the legacy and participation paths."""

    def __init__(self, *, store: ClaimStore) -> None:
        self._store = store

    def claim(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_id: str,
        activation_epoch: int,
        owner: str,
        lane: str = LANE_PRODUCTION,
        now_ms: int | None = None,
    ) -> ClaimDecision:
        """Claim one source for one owner. The first claim wins, forever.

        The claim is keyed by ``(channel, chat_id, source_event_id)``: a source that
        the legacy path already produced may not be replayed by participation in a
        later epoch, which is exactly the guarantee that prevents a cutover from
        double-processing retained material.
        """
        token = str(source_event_id or "").strip()
        if not token:
            return ClaimDecision(False, "", "missing_source_id")
        if owner not in {OWNER_LEGACY, OWNER_PARTICIPATION}:
            raise ValueError(f"unknown owner: {owner}")
        if lane not in {LANE_PRODUCTION, LANE_SHADOW}:
            raise ValueError(f"unknown lane: {lane}")
        moment = int(now_ms if now_ms is not None else time.time() * 1000)
        newly_claimed, winning_owner = self._store.claim_source_sync(
            channel=str(channel),
            chat_id=str(chat_id),
            source_event_id=token,
            activation_epoch=int(activation_epoch),
            lane=str(lane),
            owner=str(owner),
            now_ms=moment,
        )
        if winning_owner == str(owner):
            # ``newly_claimed`` distinguishes the first claim from an idempotent retry.
            return ClaimDecision(True, winning_owner, "claimed" if newly_claimed else "already_owned")
        return ClaimDecision(False, winning_owner or "unknown", "owned_by_other")


class ParticipationRuntime:
    """The observer-facing entry point: bounded admission, then a non-awaiting offer.

    Bound to an activation snapshot per chat by the caller; it does not resolve
    policy itself. ``offer_source`` is deliberately synchronous so an event-dispatch
    callback cannot block on scheduling.
    """

    def __init__(
        self,
        *,
        scheduler: OpportunityScheduler,
        source_owner: SourceOwner,
        activation_epoch: int,
        lane: str = LANE_PRODUCTION,
        is_enabled: Any | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._source_owner = source_owner
        self._activation_epoch = int(activation_epoch)
        self._lane = str(lane)
        self._is_enabled = is_enabled or (lambda channel, chat_id: True)

    @property
    def scheduler(self) -> OpportunityScheduler:
        return self._scheduler

    @property
    def activation_epoch(self) -> int:
        return self._activation_epoch

    def set_activation_epoch(self, epoch: int) -> None:
        self._activation_epoch = int(epoch)

    def offer_source(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_ids: tuple[str, ...],
        observed_revision: int,
        trigger: str,
        created_at_ms: int | None = None,
    ) -> bool:
        """Claim and offer one trigger. Returns immediately; never awaits the judge.

        A source already owned by the legacy path is dropped here, so an activated
        chat cannot produce the same material twice during or after a cutover.
        """
        if not self._is_enabled(channel, chat_id):
            return False
        claimed: list[str] = []
        for source_id in source_event_ids:
            decision = self._source_owner.claim(
                channel=channel,
                chat_id=chat_id,
                source_event_id=source_id,
                activation_epoch=self._activation_epoch,
                owner=OWNER_PARTICIPATION,
                lane=self._lane,
            )
            if decision.granted:
                claimed.append(str(source_id))
            else:
                logger.debug(
                    "participation_source_not_owned chat={} source_id={} owner={} reason={}",
                    chat_id,
                    str(source_id)[:32],
                    decision.owner,
                    decision.reason,
                )
        if not claimed:
            return False
        moment = int(created_at_ms if created_at_ms is not None else time.time() * 1000)
        opportunity = ParticipationOpportunity(
            opportunity_id=_opportunity_id(
                channel=channel,
                chat_id=chat_id,
                activation_epoch=self._activation_epoch,
                lane=self._lane,
                source_event_ids=tuple(claimed),
                observed_revision=int(observed_revision),
            ),
            channel=str(channel),
            chat_id=str(chat_id),
            trigger=trigger,  # type: ignore[arg-type]
            source_event_ids=tuple(claimed),
            observed_revision=int(observed_revision),
            activation_epoch=self._activation_epoch,
            created_at_ms=moment,
            lane=self._lane,
        )
        return self._scheduler.offer(opportunity)


def _opportunity_id(
    *,
    channel: str,
    chat_id: str,
    activation_epoch: int,
    lane: str,
    source_event_ids: tuple[str, ...],
    observed_revision: int,
) -> str:
    from yeoman_gateway.consciousness.opportunities import opportunity_id_for

    return opportunity_id_for(
        channel=channel,
        chat_id=chat_id,
        activation_epoch=activation_epoch,
        lane=lane,
        source_event_ids=source_event_ids,
        observed_revision=observed_revision,
    )


__all__ = [
    "LANE_PRODUCTION",
    "LANE_SHADOW",
    "OWNER_LEGACY",
    "OWNER_PARTICIPATION",
    "ClaimDecision",
    "ClaimStore",
    "ParticipationRuntime",
    "SourceOwner",
]


#: Inputs whose change is an activation transition (spec section 3.1).
ACTIVATION_INPUTS: tuple[str, ...] = ("enabled", "shadow", "judge_route")


def activation_fingerprint(
    *, enabled: bool, shadow: bool, judge_route: str
) -> str:
    """Stable fingerprint of the activation-affecting settings."""
    return "|".join(
        [f"enabled={bool(enabled)}", f"shadow={bool(shadow)}", f"route={str(judge_route)}"]
    )


class ActivationEpochTracker:
    """Advances the persisted activation epoch when activation settings change.

    The epoch is the fence that makes stale workers harmless: it advances on
    enable/disable, shadow/live transitions and activation-affecting reloads, is
    persisted so a restart preserves it, and is never advanced just because the
    process restarted (spec section 3.1).
    """

    def __init__(self, *, store: object, scope: str = "participation") -> None:
        self._store = store
        self._scope = str(scope)
        self._last: dict[str, str] = {}

    def observe(
        self,
        *,
        channel: str,
        chat_id: str,
        enabled: bool,
        shadow: bool,
        judge_route: str,
        now_ms: int | None = None,
    ) -> int:
        """Record the current activation inputs and advance the epoch on change."""
        key = f"{channel}:{chat_id}"
        fingerprint = activation_fingerprint(
            enabled=enabled, shadow=shadow, judge_route=judge_route
        )
        previous = self._last.get(key)
        self._last[key] = fingerprint
        # The comparison must survive a restart: read the fingerprint recorded with the
        # current epoch, so a shadow/live change made while the process was down still
        # advances the epoch instead of being mistaken for "no change seen yet".
        persisted = str(
            self._store.activation_fingerprint_sync(self._scope)  # type: ignore[attr-defined]
        )
        current = int(
            self._store.activation_epoch_sync(  # type: ignore[attr-defined]
                self._scope, fingerprint=fingerprint
            )
        )
        if not persisted:
            # No recorded inputs yet - a fresh database, or one whose row predates the
            # fingerprint column. Adopt the current state at the current epoch and record
            # it, rather than inventing a transition that never happened.
            self._last[key] = fingerprint
            return current
        if persisted == fingerprint:
            return current
        advanced = int(
            self._store.advance_activation_epoch_sync(  # type: ignore[attr-defined]
                self._scope, fingerprint=fingerprint
            )
        )
        logger.info(
            "participation activation epoch advanced chat={} previous={} current={} change={}->{}",
            chat_id,
            current,
            advanced,
            previous,
            fingerprint,
        )
        return advanced

    def current(self) -> int:
        return int(self._store.activation_epoch_sync(self._scope))  # type: ignore[attr-defined]


class ParticipationIngress:
    """Turns observed inbound messages into admitted opportunities.

    The scheduling half of the trigger contract: burst and lull supply activity
    *windows*, this supplies the new material inside them. It performs bounded local
    work only - claim the sources, compute the durable revision, offer - and returns
    without awaiting a judge, generator or transport.
    """

    def __init__(
        self,
        *,
        runtime: ParticipationRuntime,
        ledger: object,
        is_active: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._is_active = is_active or (lambda channel, chat_id: True)

    def handle_event(self, event: object) -> bool:
        """Offer one observed inbound message. Returns whether it was admitted."""
        channel = str(getattr(event, "channel", "") or "").strip()
        chat_id = str(getattr(event, "chat_id", "") or "").strip()
        if not channel or not chat_id:
            return False
        if not self._is_active(channel, chat_id):
            return False
        metadata = getattr(event, "metadata", None) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("participation") or metadata.get("spawned_by_tool"):
            # Our own output and tool-internal traffic are not new human material.
            return False
        principal = str(getattr(event, "sender_id", "") or "").strip()
        if not principal:
            return False
        source_id = str(
            getattr(event, "message_id", "") or metadata.get("message_id") or ""
        ).strip()
        if not source_id:
            # Without a durable source identity there is nothing to admit later.
            return False
        revision = int(
            self._ledger.next_source_revision_sync(  # type: ignore[attr-defined]
                channel=channel, chat_id=chat_id
            )
        )
        # Event timestamps come from the channel clock and may sit far from this
        # process's clock; the opportunity's lifetime is measured locally, so an
        # obviously unusable timestamp falls back to "now" instead of expiring the
        # candidate at dequeue.
        import time as _time

        now_ms = int(_time.time() * 1000)
        try:
            created_ms = int(float(getattr(event, "timestamp", 0) or 0) * 1000) or now_ms
        except (TypeError, ValueError):
            created_ms = now_ms
        if created_ms <= 0 or abs(now_ms - created_ms) > 86_400_000:
            created_ms = now_ms
        return self._runtime.offer_source(
            channel=channel,
            chat_id=chat_id,
            source_event_ids=(source_id,),
            observed_revision=revision,
            trigger="inbound",
            created_at_ms=created_ms,
        )
