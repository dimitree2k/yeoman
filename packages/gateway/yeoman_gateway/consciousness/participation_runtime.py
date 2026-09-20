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

import json
import time
from collections.abc import Callable, Mapping
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

    def mark_considered(
        self,
        *,
        channel: str,
        chat_id: str,
        observed_revision: int,
        lane: str,
    ) -> None:
        """Persist a watermark only after the scheduler accepted an opportunity."""
        marker = getattr(self._store, "mark_material_considered_sync", None)
        if marker is None:
            return
        marker(
            channel=channel,
            chat_id=chat_id,
            observed_revision=int(observed_revision),
            lane=str(lane),
        )

    def release_claim(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_id: str,
        lane: str,
    ) -> None:
        """Undo only a participation claim made before a rejected queue offer."""
        releaser = getattr(self._store, "release_source_claim_sync", None)
        if releaser is None:
            return
        releaser(
            channel=channel,
            chat_id=chat_id,
            source_event_id=source_event_id,
            lane=str(lane),
            owner=OWNER_PARTICIPATION,
        )


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
        activation_epoch: int = 1,
        lane: str = LANE_PRODUCTION,
        is_enabled: Any | None = None,
        activation_provider: Callable[[str, str], object | None] | None = None,
        considered_revision_provider: Callable[..., int] | None = None,
        direct_work_active: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._source_owner = source_owner
        self._activation_epoch = int(activation_epoch)
        self._lane = str(lane)
        self._is_enabled = is_enabled or (lambda channel, chat_id: True)
        self._activation_provider = activation_provider
        self._considered_revision_provider = considered_revision_provider
        self._direct_work_active = direct_work_active
        self._hydrated_chats: set[tuple[str, str, str]] = set()

    @property
    def scheduler(self) -> OpportunityScheduler:
        return self._scheduler

    @property
    def activation_epoch(self) -> int:
        return self._activation_epoch

    def set_activation_epoch(self, epoch: int) -> None:
        self._activation_epoch = int(epoch)

    def set_activation_provider(
        self, provider: Callable[[str, str], object | None] | None
    ) -> None:
        """Replace the synchronous activation source used before each offer."""
        self._activation_provider = provider

    def current_activation(self, channel: str, chat_id: str) -> object | None:
        """Return the caller-owned current snapshot, if one was configured."""
        if self._activation_provider is None:
            return None
        return self._activation_provider(str(channel), str(chat_id))

    @staticmethod
    def _snapshot_value(snapshot: object, name: str, default: object = None) -> object:
        if isinstance(snapshot, Mapping):
            return snapshot.get(name, default)
        return getattr(snapshot, name, default)

    def _offer_activation(self, channel: str, chat_id: str) -> tuple[int, str, bool]:
        """Resolve epoch/lane/readiness once, immediately before claiming sources."""
        if self._direct_work_active is not None:
            try:
                if bool(self._direct_work_active(str(channel), str(chat_id))):
                    return self._activation_epoch, self._lane, False
            except Exception:  # noqa: BLE001 - a failed fence must not admit work
                return self._activation_epoch, self._lane, False
        snapshot = self.current_activation(channel, chat_id)
        if self._activation_provider is None:
            return self._activation_epoch, self._lane, bool(
                self._is_enabled(channel, chat_id)
            )
        if snapshot is None:
            return self._activation_epoch, self._lane, False

        try:
            epoch = int(
                str(self._snapshot_value(snapshot, "activation_epoch", self._activation_epoch))
            )
        except (TypeError, ValueError):
            return self._activation_epoch, self._lane, False
        shadow = bool(self._snapshot_value(snapshot, "shadow", self._lane == LANE_SHADOW))
        lane_value = self._snapshot_value(snapshot, "lane", None)
        lane = str(lane_value or (LANE_SHADOW if shadow else LANE_PRODUCTION))
        if lane not in {LANE_PRODUCTION, LANE_SHADOW}:
            return epoch, self._lane, False
        live = self._snapshot_value(snapshot, "live", None)
        observing = self._snapshot_value(snapshot, "observing", None)
        if live is None and observing is None:
            valid = bool(self._snapshot_value(snapshot, "valid", True))
            enabled = bool(self._snapshot_value(snapshot, "enabled", False))
            opted_in = bool(self._snapshot_value(snapshot, "opted_in", True))
            ready = valid and enabled and opted_in
        else:
            ready = bool(live) or bool(observing)
        ready = ready and bool(self._is_enabled(channel, chat_id))
        return epoch, lane, ready

    def _hydrate_considered_revision(self, channel: str, chat_id: str, lane: str) -> None:
        key = (str(channel), str(chat_id), str(lane))
        if key in self._hydrated_chats or self._considered_revision_provider is None:
            return
        try:
            revision = int(
                self._considered_revision_provider(
                    channel=channel, chat_id=chat_id, lane=str(lane)
                )
            )
        except Exception as exc:  # noqa: BLE001 - retry on the next bounded offer
            logger.warning(
                "participation watermark load failed chat={} error_type={}",
                chat_id,
                type(exc).__name__,
            )
            return
        self._hydrated_chats.add(key)
        if revision <= 0:
            return
        marker = getattr(self._scheduler, "mark_considered", None)
        if marker is not None:
            try:
                marker(channel, chat_id, observed_revision=revision, lane=str(lane))
            except TypeError:
                # Older test doubles expose the production-only marker signature.
                marker(channel, chat_id, observed_revision=revision)

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
        source_ids = tuple(
            dict.fromkeys(
                token
                for token in (str(item or "").strip() for item in source_event_ids)
                if token and not token.startswith("observed:")
            )
        )
        if not source_ids:
            return False
        activation_epoch, lane, ready = self._offer_activation(channel, chat_id)
        if not ready:
            return False
        self._hydrate_considered_revision(channel, chat_id, lane)
        claimed: list[str] = []
        newly_claimed: list[str] = []
        for source_id in source_ids:
            decision = self._source_owner.claim(
                channel=channel,
                chat_id=chat_id,
                source_event_id=source_id,
                activation_epoch=activation_epoch,
                owner=OWNER_PARTICIPATION,
                lane=lane,
            )
            if decision.granted:
                claimed.append(str(source_id))
                if decision.reason == "claimed":
                    newly_claimed.append(str(source_id))
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
                activation_epoch=activation_epoch,
                lane=lane,
                source_event_ids=tuple(claimed),
                observed_revision=int(observed_revision),
            ),
            channel=str(channel),
            chat_id=str(chat_id),
            trigger=trigger,
            source_event_ids=tuple(claimed),
            observed_revision=int(observed_revision),
            activation_epoch=activation_epoch,
            created_at_ms=moment,
            lane=lane,
        )
        # Keep the legacy inspection properties useful for callers without making them
        # the source of truth when a provider is configured.
        self._activation_epoch = activation_epoch
        self._lane = lane
        offer_with_result = getattr(self._scheduler, "offer_with_result", None)
        if callable(offer_with_result):
            offer_result = offer_with_result(opportunity)
            offered = bool(getattr(offer_result, "accepted", False))
            dropped_source_ids = tuple(
                str(item)
                for item in getattr(offer_result, "dropped_source_event_ids", ())
            )
        else:
            offered = bool(self._scheduler.offer(opportunity))
            dropped_source_ids = ()
        to_release = set(dropped_source_ids)
        if not offered:
            to_release.update(newly_claimed)
        for source_id in to_release:
            self._source_owner.release_claim(
                channel=channel,
                chat_id=chat_id,
                source_event_id=source_id,
                lane=lane,
            )
        if not offered:
            return False
        if offered:
            self._source_owner.mark_considered(
                channel=channel,
                chat_id=chat_id,
                observed_revision=int(observed_revision),
                lane=lane,
            )
        return True


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
    "ACTIVATION_INPUTS",
    "ActivationEpochTracker",
    "activation_fingerprint",
    "ParticipationIngress",
    "ParticipationRuntime",
    "SourceOwner",
]


#: Inputs whose change is an activation transition (spec section 3.1).
ACTIVATION_INPUTS: tuple[str, ...] = (
    "enabled",
    "shadow",
    "judge_route",
    "action_cap",
    "writer_route",
    "writer_profile",
)


def _canonical_activation_value(value: object) -> object:
    """Make mappings and set-like sequences stable without changing scalar values."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_activation_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset, list, tuple)):
        values = [_canonical_activation_value(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def activation_fingerprint(
    *,
    enabled: bool | None = None,
    shadow: bool | None = None,
    judge_route: str | None = None,
    activation_state: Mapping[str, object] | None = None,
    processing_enabled: bool | None = None,
    managed_targets: object = (),
    shadow_targets: object = (),
    chat_opt_ins: object = (),
    active_pauses: object = (),
) -> str:
    """Return one canonical fingerprint for the complete activation state.

    The three scalar arguments remain accepted for the original tracker callers. New
    callers pass one complete mapping so no per-chat fingerprint is persisted.
    """
    state: dict[str, object]
    if activation_state is not None:
        state = dict(activation_state)
        global_state = state.get("global")
        if isinstance(global_state, Mapping):
            enabled = bool(global_state.get("enabled", enabled))
            shadow = bool(global_state.get("shadow", shadow))
            judge_route = str(global_state.get("judge_route", judge_route or ""))
    else:
        state = {}

    prefix = [
        f"enabled={bool(enabled)}",
        f"shadow={bool(shadow)}",
        f"route={str(judge_route or '')}",
    ]
    if activation_state is None:
        if processing_enabled is not None:
            state["processing_enabled"] = bool(processing_enabled)
        state["managed_targets"] = managed_targets
        state["shadow_targets"] = shadow_targets
        state["chat_opt_ins"] = chat_opt_ins
        state["active_pauses"] = active_pauses
    else:
        state.setdefault(
            "global",
            {
                "enabled": bool(enabled),
                "shadow": bool(shadow),
                "judge_route": str(judge_route or ""),
            },
        )
    normalized = _canonical_activation_value(state)
    if not normalized:
        return "|".join(prefix)
    return "|".join(prefix) + "|state=" + json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class ActivationEpochTracker:
    """Advances the persisted activation epoch when activation settings change.

    The epoch is the fence that makes stale workers harmless: it advances on
    enable/disable, shadow/live transitions and activation-affecting reloads, is
    persisted so a restart preserves it, and is never advanced just because the
    process restarted (spec section 3.1).
    """

    def __init__(
        self,
        *,
        store: object,
        scope: str = "participation",
        activation_state_provider: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self._store = store
        self._scope = str(scope)
        self._activation_state_provider = activation_state_provider

    def set_activation_state_provider(
        self, provider: Callable[[], Mapping[str, object]] | None
    ) -> None:
        """Attach the existing policy/processing composition as the state source."""
        self._activation_state_provider = provider

    def refresh_activation_sync(
        self,
        activation_state: Mapping[str, object] | None = None,
        *,
        fingerprint: str | None = None,
        now_ms: int | None = None,
    ) -> int:
        """Refresh the one persisted global epoch from one complete state snapshot."""
        state = activation_state
        if fingerprint is None:
            if state is None and self._activation_state_provider is not None:
                state = self._activation_state_provider()
            if state is not None:
                fingerprint = activation_fingerprint(activation_state=state)
            else:
                # Without a provider or explicit state there is no safe transition to
                # invent; the persisted epoch remains the source of truth.
                return self.current()
        refresh = getattr(self._store, "refresh_activation_sync", None)
        if callable(refresh):
            return int(
                refresh(
                    self._scope,
                    fingerprint=str(fingerprint),
                    now_ms=now_ms,
                )
            )

        # Compatibility with a pre-refresh test double/store. The real SpeakupLog uses
        # the atomic method above; this fallback retains the previous durable API.
        persisted = str(
            self._store.activation_fingerprint_sync(self._scope)  # type: ignore[attr-defined]
        )
        current = int(self._store.activation_epoch_sync(self._scope))  # type: ignore[attr-defined]
        if not persisted:
            self._store.activation_epoch_sync(  # type: ignore[attr-defined]
                self._scope, fingerprint=str(fingerprint)
            )
            return current
        if persisted == str(fingerprint):
            return current
        return int(
            self._store.advance_activation_epoch_sync(  # type: ignore[attr-defined]
                self._scope,
                fingerprint=str(fingerprint),
                now_ms=now_ms,
            )
        )

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
        fingerprint = activation_fingerprint(
            enabled=enabled, shadow=shadow, judge_route=judge_route
        )
        return self.refresh_activation_sync(fingerprint=fingerprint, now_ms=now_ms)

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
        is_direct: Callable[[object], bool] | None = None,
        material_provider: Callable[
            [str, str, tuple[str, ...] | None], tuple[tuple[str, ...], int]
        ]
        | None = None,
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._is_active = is_active or (lambda channel, chat_id: True)
        # This callback must consult the canonical processing entry.  Observer metadata
        # alone is intentionally not treated as direct authority.
        self._is_direct = is_direct
        self._material_provider = material_provider

    def handle_event(self, event: object) -> bool:
        """Offer one observed inbound message. Returns whether it was admitted."""
        channel = str(getattr(event, "channel", "") or "").strip()
        chat_id = str(getattr(event, "chat_id", "") or "").strip()
        if not channel or not chat_id:
            return False
        if not self._is_active(channel, chat_id):
            return False
        if self._is_direct is not None:
            try:
                if bool(self._is_direct(event)):
                    return False
            except Exception:  # noqa: BLE001 - failed validation cannot create work
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
        raw_source_ids = getattr(event, "source_event_ids", None)
        if not raw_source_ids:
            raw_source_ids = metadata.get("source_event_ids")
        source_ids: tuple[str, ...]
        if isinstance(raw_source_ids, str):
            source_ids = (raw_source_ids,)
        elif isinstance(raw_source_ids, (list, tuple, set, frozenset)):
            source_ids = tuple(str(item) for item in raw_source_ids)
        else:
            source_id = str(
                getattr(event, "message_id", "") or metadata.get("message_id") or ""
            ).strip()
            source_ids = (source_id,) if source_id else ()
        source_ids = tuple(
            token
            for token in dict.fromkeys(str(item).strip() for item in source_ids)
            if token and not token.startswith("observed:")
        )
        if not source_ids:
            # Without a durable source identity there is nothing to admit later.
            return False
        if self._material_provider is not None:
            try:
                material, revision = self._material_provider(
                    channel, chat_id, tuple(source_ids)
                )
            except Exception:  # noqa: BLE001 - an unreadable archive cannot create work
                return False
            source_ids = tuple(str(item).strip() for item in material if str(item).strip())
            revision = int(revision)
        else:
            # Production must supply archive authorization plus ledger sequencing. A
            # missing composition fails closed instead of inventing a source revision.
            return False
        if not source_ids:
            return False
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
            source_event_ids=source_ids,
            observed_revision=revision,
            trigger="inbound",
            created_at_ms=created_ms,
        )
