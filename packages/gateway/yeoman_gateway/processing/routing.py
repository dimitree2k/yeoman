"""Structural continuity signal for thread routing.

An unquoted message joins its sender's single active thread only when the chat itself shows
the link: the message right before it belongs to that thread and is either the sender's own
message or the bot's message in that thread. Wording never decides: keyword lists and
shared-term overlap attached unrelated questions to old threads, and recency alone is no
proof either. Nothing here reads session history, the ambient window or long-term memory.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Verdicts. Only ``positive`` permits attaching to an existing thread.
VERDICT_POSITIVE = "positive"
VERDICT_NONE = "none"
VERDICT_UNKNOWN = "unknown"

SIGNAL_ADJACENT_SAME_SENDER = "adjacent_same_sender"
SIGNAL_ADJACENT_BOT_REPLY = "adjacent_bot_reply"
SIGNAL_NONE = "none"


@dataclass(frozen=True, slots=True)
class PreviousMessage:
    """The chat message right before the current one, as the processing journal saw it."""

    message_id: str
    from_bot: bool
    principals: frozenset[str] = frozenset()
    thread_ids: frozenset[str] = frozenset()
    at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ContinuitySignal:
    """What the chat structure says about the current message and one candidate thread."""

    kind: str = SIGNAL_NONE
    verdict: str = VERDICT_NONE
    evidence_ids: tuple[str, ...] = ()
    detail: str = ""

    @property
    def positive(self) -> bool:
        return self.verdict == VERDICT_POSITIVE


def continuity_signal(
    *,
    candidate_thread_id: str,
    principal: str,
    previous: PreviousMessage | None,
    now_ms: int | None = None,
    same_sender_gap_ms: int | None = None,
    mentioned_bot: bool = False,
) -> ContinuitySignal:
    """Positive only when the directly preceding chat message belongs to the candidate.

    A sender's own previous message counts only within ``same_sender_gap_ms``: a request
    split over several messages arrives in quick succession, while a new question minutes
    later is a new request. After the bot's message the whole follow-up window applies.
    A message that addresses the bot afresh by mention is a new request unless the bot
    spoke last in the thread.
    """
    if previous is None:
        return ContinuitySignal(detail="no_previous_message")
    if candidate_thread_id not in previous.thread_ids:
        return ContinuitySignal(detail="previous_in_other_thread")
    if previous.from_bot:
        return ContinuitySignal(
            kind=SIGNAL_ADJACENT_BOT_REPLY,
            verdict=VERDICT_POSITIVE,
            evidence_ids=(previous.message_id,),
            detail="bot_spoke_last_in_thread",
        )
    if mentioned_bot:
        return ContinuitySignal(detail="mention_starts_new_request")
    if principal and principal in previous.principals:
        if (same_sender_gap_ms is not None and now_ms is not None
                and previous.at_ms is not None and now_ms - previous.at_ms > same_sender_gap_ms):
            return ContinuitySignal(detail="sender_gap_too_long")
        return ContinuitySignal(
            kind=SIGNAL_ADJACENT_SAME_SENDER,
            verdict=VERDICT_POSITIVE,
            evidence_ids=(previous.message_id,),
            detail="sender_spoke_last_in_thread",
        )
    return ContinuitySignal(detail="other_sender_in_between")


__all__ = [
    "SIGNAL_ADJACENT_BOT_REPLY",
    "SIGNAL_ADJACENT_SAME_SENDER",
    "SIGNAL_NONE",
    "VERDICT_NONE",
    "VERDICT_POSITIVE",
    "VERDICT_UNKNOWN",
    "ContinuitySignal",
    "PreviousMessage",
    "continuity_signal",
]
