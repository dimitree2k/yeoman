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
