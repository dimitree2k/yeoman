"""Thread assignment: the six join rules from spec R03 as one testable table.

The module is deliberately split in two halves:

* pure decision functions (``JoinInput`` + ``JoinView`` -> ``JoinDecision``) that hold the
  rule order and can be tested without a database,
* :class:`ThreadRegistry`, which builds the read view from the durable store and persists
  the decision it made.

Nothing here reads model output. ``explicit_correction`` and ``topic_break`` are explicit
canonical inputs; a text heuristic must never invalidate a valid answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from yeoman_gateway.processing.models import (
    CanonicalEvent,
    TurnRef,
    UpdateEffect,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)

if TYPE_CHECKING:
    from yeoman_gateway.processing.store import ProcessingStore


class JoinRule(StrEnum):
    """The six rules of spec R03, in binding priority order."""

    REPLY_KNOWN = "reply_known"  # R03.1
    EXPLICIT_CORRECTION = "explicit_correction"  # R03.2
    FOLLOWUP_SINGLE_ACTIVE = "followup_single_active"  # R03.3
    MENTION_NO_REFERENCE = "mention_new_thread"  # R03.4
    DM_LAST_ACTIVE = "dm_last_active"  # R03.5
    AMBIENT = "ambient"  # R03.6


JOIN_RULES: tuple[JoinRule, ...] = (
    JoinRule.REPLY_KNOWN,
    JoinRule.EXPLICIT_CORRECTION,
    JoinRule.FOLLOWUP_SINGLE_ACTIVE,
    JoinRule.MENTION_NO_REFERENCE,
    JoinRule.DM_LAST_ACTIVE,
    JoinRule.AMBIENT,
)


def classify_update(
    *, kind: str, explicit_correction: bool = False, authorized: bool = False
) -> UpdateEffect:
    """What an incoming update does to the running turn.

    A reaction only observes; an unauthorised participant only observes; a material change
    (delete/edit) or an explicit correction by an authorised principal supersedes; anything
    else appends context.
    """
    if kind == "reaction":
        return UpdateEffect.OBSERVE
    if not authorized:
        return UpdateEffect.OBSERVE
    if kind in {"delete", "edit"} or explicit_correction:
        return UpdateEffect.SUPERSEDE
    return UpdateEffect.APPEND


@dataclass(frozen=True, slots=True)
class JoinInput:
    """Canonical base data of one inbound event, nothing derived from a model."""

    event_id: str
    channel: str
    chat_id: str
    principal: str
    kind: str = "message"
    is_group: bool = False
    mentioned_bot: bool = False
    reply_to_message_id: str | None = None
    target_message_id: str | None = None
    explicit_correction: bool = False
    topic_break: bool = False
    occurred_ms: int | None = None


@dataclass(frozen=True, slots=True)
class JoinView:
    """Read-only view the rules decide on."""

    reply_thread_id: str | None = None
    reply_turn_id: str | None = None
    reply_thread_state: str | None = None
    reply_reopenable: bool = False
    correction_targets: tuple[tuple[str, str], ...] = ()
    active_threads_for_principal: tuple[tuple[str, str], ...] = ()
    last_active_dm_thread: str | None = None
    last_active_dm_turn: str | None = None


@dataclass(frozen=True, slots=True)
class JoinDecision:
    """Result of one assignment; ``thread_id is None`` means no thread at all."""

    rule: JoinRule
    thread_id: str | None = None
    turn_id: str | None = None
    reason: str = ""
    new_thread: bool = False
    new_turn: bool = False
    supersedes: bool = False
    observes_only: bool = False
    needs_clarification: bool = False
    ambiguous_targets: tuple[str, ...] = ()
    quote_ref: str | None = None
    quote_allowed: bool = False


@dataclass(frozen=True, slots=True)
class ThreadPolicy:
    """The v1 thread defaults; read from config, never extended here."""

    followup_window_ms: int = 15_000
    idle_ms: int = 1_800_000
    reopen_window_ms: int = 604_800_000
    pending_inputs_per_thread: int = 32

    @classmethod
    def from_config(cls, config: Any) -> ThreadPolicy:
        threads = getattr(config, "threads", None) or getattr(config, "processing", None)
        threads = getattr(threads, "threads", threads)
        if threads is None:
            return cls()
        return cls(
            followup_window_ms=int(getattr(threads, "followup_window_seconds", 15)) * 1000,
            idle_ms=int(getattr(threads, "idle_seconds", 1800)) * 1000,
            reopen_window_ms=int(getattr(threads, "reopen_window_seconds", 604800)) * 1000,
            pending_inputs_per_thread=int(getattr(threads, "pending_inputs_per_thread", 32)),
        )


def _rule_reply_known(
    data: JoinInput, view: JoinView, policy: ThreadPolicy
) -> JoinDecision | None:
    if not view.reply_thread_id:
        return None
    reopenable = view.reply_thread_state in (None, "open") or view.reply_reopenable
    return JoinDecision(
        rule=JoinRule.REPLY_KNOWN,
        thread_id=view.reply_thread_id,
        turn_id=view.reply_turn_id if not reopenable else None,
        reason="reply_to_known_thread",
        new_turn=view.reply_thread_state in ("idle", "closed"),
        quote_ref=data.reply_to_message_id,
        quote_allowed=reopenable,
    )


def _rule_explicit_correction(
    data: JoinInput, view: JoinView, policy: ThreadPolicy
) -> JoinDecision | None:
    if not (data.explicit_correction or data.kind in {"delete", "edit"}):
        return None
    targets = view.correction_targets
    if len(targets) == 1:
        thread_id, turn_id = targets[0]
        return JoinDecision(
            rule=JoinRule.EXPLICIT_CORRECTION,
            thread_id=thread_id,
            turn_id=turn_id,
            reason="explicit_correction",
            supersedes=True,
        )
    if len(targets) > 1:
        return JoinDecision(
            rule=JoinRule.EXPLICIT_CORRECTION,
            thread_id=None,
            turn_id=None,
            reason="ambiguous_reference",
            needs_clarification=True,
            ambiguous_targets=tuple(turn for _thread, turn in targets),
        )
    return None


def _rule_followup_single_active(
    data: JoinInput, view: JoinView, policy: ThreadPolicy
) -> JoinDecision | None:
    if data.topic_break:
        return None
    if len(view.active_threads_for_principal) != 1:
        return None
    thread_id, turn_id = view.active_threads_for_principal[0]
    return JoinDecision(
        rule=JoinRule.FOLLOWUP_SINGLE_ACTIVE,
        thread_id=thread_id,
        turn_id=turn_id,
        reason="followup_single_active_thread",
        new_turn=turn_id is None,
    )


def _rule_mention_no_reference(
    data: JoinInput, view: JoinView, policy: ThreadPolicy
) -> JoinDecision | None:
    if not data.mentioned_bot:
        return None
    return JoinDecision(
        rule=JoinRule.MENTION_NO_REFERENCE, reason="mention_without_reference", new_thread=True
    )


def _rule_dm_last_active(
    data: JoinInput, view: JoinView, policy: ThreadPolicy
) -> JoinDecision | None:
    if data.is_group or data.topic_break:
        return None
    if not view.last_active_dm_thread:
        return None
    return JoinDecision(
        rule=JoinRule.DM_LAST_ACTIVE,
        thread_id=view.last_active_dm_thread,
        turn_id=None,
        reason="last_active_dm_thread",
        new_turn=True,
    )


def _rule_ambient(data: JoinInput, view: JoinView, policy: ThreadPolicy) -> JoinDecision | None:
    return JoinDecision(rule=JoinRule.AMBIENT, reason="ambient")


_RULES: Mapping[JoinRule, Callable[[JoinInput, JoinView, ThreadPolicy], JoinDecision | None]] = {
    JoinRule.REPLY_KNOWN: _rule_reply_known,
    JoinRule.EXPLICIT_CORRECTION: _rule_explicit_correction,
    JoinRule.FOLLOWUP_SINGLE_ACTIVE: _rule_followup_single_active,
    JoinRule.MENTION_NO_REFERENCE: _rule_mention_no_reference,
    JoinRule.DM_LAST_ACTIVE: _rule_dm_last_active,
    JoinRule.AMBIENT: _rule_ambient,
}


class ThreadRegistry:
    """Applies the join rules and persists the outcome in the processing store."""

    def __init__(
        self,
        *,
        store: "ProcessingStore",
        config: Any = None,
        policy: ThreadPolicy | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or (ThreadPolicy.from_config(config) if config else ThreadPolicy())

    @property
    def policy(self) -> ThreadPolicy:
        return self._policy

    # -- decision ----------------------------------------------------------------------

    def decide(self, data: JoinInput, view: JoinView) -> JoinDecision:
        """First rule that answers wins; the ambient fallback always answers."""
        for rule in JOIN_RULES:
            decision = _RULES[rule](data, view, self._policy)
            if decision is not None:
                return decision
        return JoinDecision(rule=JoinRule.AMBIENT, reason="ambient")

    def view_for(self, data: JoinInput, *, now_ms: int) -> JoinView:
        return JoinView(
            reply_thread_id=self._reply_thread(data),
            reply_turn_id=self._reply_turn(data),
            reply_thread_state=self._reply_thread_state(data),
            reply_reopenable=self._reply_reopenable(data, now_ms=now_ms),
            correction_targets=self._correction_targets(data),
            active_threads_for_principal=self._active_threads(data, now_ms=now_ms),
            last_active_dm_thread=self._last_dm_thread(data),
            last_active_dm_turn=None,
        )

    def assign(
        self,
        event: CanonicalEvent,
        *,
        now_ms: int | None = None,
        explicit_correction: bool | None = None,
        topic_break: bool | None = None,
        allow_turn: bool = True,
    ) -> JoinDecision:
        """Assign one canonical event; idempotent per triggering event.

        ``allow_turn=False`` records lineage only: the event is attributed to a thread but
        opens no turn and no mailbox entry (spec R03: ambient is background, not an order).
        """
        moment = int(now_ms) if now_ms is not None else _now_ms()
        data = self.input_from_event(
            event, explicit_correction=explicit_correction, topic_break=topic_break
        )

        stored = self._store.event_assignment(event.event_id)
        if stored is not None and stored[0] is not None:
            return replace(
                self.decide(data, self.view_for(data, now_ms=moment)),
                thread_id=stored[0],
                turn_id=stored[1],
                new_thread=False,
                new_turn=False,
            )

        decision = self.decide(data, self.view_for(data, now_ms=moment))
        if event.kind == "reaction":
            decision = replace(
                decision,
                observes_only=True,
                turn_id=None,
                new_turn=False,
                reason=decision.reason or "reaction_signal",
            )
        if decision.thread_id is None and decision.rule is JoinRule.AMBIENT:
            # Ambient is background: it is neither a thread nor a turn.
            if decision.needs_clarification:
                self._store.attach_event_assignment(
                    event_id=event.event_id, thread_id=None, turn_id=None, now_ms=moment
                )
            return decision
        if decision.needs_clarification:
            self._store.attach_event_assignment(
                event_id=event.event_id, thread_id=None, turn_id=None, now_ms=moment
            )
            return decision
        return self._persist(event, data, decision, now_ms=moment, allow_turn=allow_turn)

    # -- persistence -------------------------------------------------------------------

    def _persist(
        self,
        event: CanonicalEvent,
        data: JoinInput,
        decision: JoinDecision,
        *,
        now_ms: int,
        allow_turn: bool = True,
    ) -> JoinDecision:
        thread_id = decision.thread_id
        opened = False
        if thread_id is None:
            thread_id = self._store.open_thread(
                channel=event.channel,
                chat_id=event.chat_id,
                root_principal=event.principal,
                kind="group" if data.is_group else "dm",
                trigger_event_id=event.event_id,
                now_ms=now_ms,
            )
            opened = True
        elif decision.new_turn and self._thread_needs_reopen(thread_id, now_ms=now_ms):
            self._store.reopen_thread(thread_id, now_ms)
        else:
            self._store.touch_thread(thread_id, now_ms)

        turn_id = decision.turn_id
        if turn_id is None and allow_turn:
            turn_id = self._store.open_turn(
                thread_id=thread_id,
                principal=event.principal,
                trigger_event_id=event.event_id,
                now_ms=now_ms,
            )
        if turn_id is not None:
            self._store.add_turn_source(
                turn_id=turn_id,
                event_id=event.event_id,
                source_message_id=event.source_message_id,
                role="trigger" if decision.new_turn or opened else "context",
                revision_at_join=1,
                now_ms=now_ms,
            )
        self._store.attach_event_assignment(
            event_id=event.event_id, thread_id=thread_id, turn_id=turn_id, now_ms=now_ms
        )
        return replace(
            decision,
            thread_id=thread_id,
            turn_id=None if decision.observes_only else turn_id,
            new_thread=opened,
        )

    # -- view helpers ------------------------------------------------------------------

    def input_from_event(
        self,
        event: CanonicalEvent,
        *,
        explicit_correction: bool | None = None,
        topic_break: bool | None = None,
    ) -> JoinInput:
        payload = dict(event.payload or {})
        is_group = bool(payload.get("is_group")) or str(event.chat_id).endswith("@g.us")
        correction = (
            bool(payload.get("explicit_correction"))
            if explicit_correction is None
            else explicit_correction
        )
        return JoinInput(
            event_id=event.event_id,
            channel=event.channel,
            chat_id=event.chat_id,
            principal=event.principal,
            kind=event.kind,
            is_group=is_group,
            mentioned_bot=bool(payload.get("mentioned_bot")),
            reply_to_message_id=_opt(payload.get("reply_to_message_id"))
            or _opt(payload.get("reply_to")),
            target_message_id=_opt(payload.get("target_message_id")),
            explicit_correction=correction or event.kind in {"delete", "edit"},
            topic_break=bool(payload.get("topic_break")) if topic_break is None else topic_break,
            occurred_ms=event.occurred_ms,
        )

    def _reply_thread(self, data: JoinInput) -> str | None:
        hit = self._store.resolve_reference(data.reply_to_message_id)
        return hit[0] if hit else None

    def _reply_turn(self, data: JoinInput) -> str | None:
        hit = self._store.resolve_reference(data.reply_to_message_id)
        return hit[1] if hit else None

    def _reply_thread_state(self, data: JoinInput) -> str | None:
        thread_id = self._reply_thread(data)
        if thread_id is None:
            return None
        thread = self._store.get_thread(thread_id)
        return thread.state if thread else None

    def _reply_reopenable(self, data: JoinInput, *, now_ms: int) -> bool:
        thread_id = self._reply_thread(data)
        if thread_id is None:
            return False
        thread = self._store.get_thread(thread_id)
        if thread is None:
            return False
        if now_ms - thread.last_activity_ms > self._policy.reopen_window_ms:
            return False
        return self._store.thread_sources_available(thread_id)

    def _correction_targets(self, data: JoinInput) -> tuple[tuple[str, str], ...]:
        if not data.explicit_correction and data.kind not in {"delete", "edit"}:
            return ()
        ref = data.target_message_id or data.reply_to_message_id
        if not ref:
            return ()
        hit = self._store.resolve_reference(ref)
        if hit is None or hit[0] is None or hit[1] is None:
            return ()
        return ((hit[0], hit[1]),)

    def _active_threads(self, data: JoinInput, *, now_ms: int) -> tuple[tuple[str, str], ...]:
        threads = self._store.list_threads(
            chat_id=data.chat_id, principal=data.principal, state="open"
        )
        active = [
            (thread.thread_id, self._active_turn_id(thread.thread_id))
            for thread in threads
            if now_ms - thread.last_activity_ms <= self._policy.followup_window_ms
        ]
        return tuple(active)

    def _active_turn_id(self, thread_id: str) -> str | None:
        turn = self._store.active_turn(thread_id)
        return turn.turn_id if turn else None

    def _last_dm_thread(self, data: JoinInput) -> str | None:
        threads = self._store.list_threads(
            chat_id=data.chat_id, principal=data.principal, state="open"
        )
        if not threads:
            return None
        return max(threads, key=lambda thread: thread.last_activity_ms).thread_id

    def _thread_needs_reopen(self, thread_id: str, *, now_ms: int) -> bool:
        """A sleeping thread may reopen inside the window while its sources still exist."""
        thread = self._store.get_thread(thread_id)
        if thread is None or thread.state == "open":
            return False
        if now_ms - thread.last_activity_ms > self._policy.reopen_window_ms:
            return False
        return self._store.thread_sources_available(thread_id)

    # -- effect core seam --------------------------------------------------------------

    def turn_lookup(self, turn_id: str) -> TurnRef | None:
        """Usable directly as ``SnapshotEffectAuthorizer(turn_lookup=...)``."""
        turn = self._store.get_turn(turn_id)
        if turn is None:
            return None
        thread = self._store.get_thread(turn.thread_id)
        if thread is None:
            return None
        return turn.to_ref(channel=thread.channel, chat_id=thread.chat_id)

    def active_turn(self, channel: str, chat_id: str) -> TurnRef | None:
        """The turn a producer in this chat is currently working for."""
        for thread in self._store.list_threads(chat_id=chat_id, state="open"):
            if thread.channel != channel:
                continue
            turn = self._store.active_turn(thread.thread_id)
            if turn is not None:
                return turn.to_ref(channel=channel, chat_id=chat_id)
        return None


def _opt(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = [
    "JOIN_RULES",
    "JoinDecision",
    "JoinInput",
    "JoinRule",
    "JoinView",
    "ThreadPolicy",
    "ThreadRegistry",
    "classify_update",
    "UpdateEffect",
]
