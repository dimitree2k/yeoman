"""Annoyance alerts: confident labeler evidence only, at most one alert per chat per day."""

from __future__ import annotations

from collections.abc import Iterable

from yeoman_gateway.reception.store import ALERT_WINDOW_MS, LabelRow

ANNOYED_STANCES = frozenset({"annoyed", "hostile"})
REMOVE_SIGNALS = frozenset({"asks_to_remove_or_mute"})
#: A label counts as alert evidence only at or above this confidence.
MIN_ALERT_CONFIDENCE = 0.75


def is_alert_evidence(item: LabelRow, *, min_confidence: float = MIN_ALERT_CONFIDENCE) -> bool:
    """A confident, annoyed or hostile reply to Arvid; anything weaker never alerts."""
    return (
        item.responds_to_arvid == "yes"
        and item.stance in ANNOYED_STANCES
        and item.confidence >= min_confidence
    )


def asks_to_remove(item: LabelRow, *, min_confidence: float = MIN_ALERT_CONFIDENCE) -> bool:
    return is_alert_evidence(item, min_confidence=min_confidence) and bool(
        REMOVE_SIGNALS.intersection(item.signals)
    )


def alert_reason(
    labels: Iterable[LabelRow],
    *,
    now_ms: int,
    threshold: int = 3,
    window_ms: int = ALERT_WINDOW_MS,
    min_confidence: float = MIN_ALERT_CONFIDENCE,
) -> str | None:
    """A confident remove/mute request or a cluster of annoyed messages, else None.

    Evidence is counted per human message: one message paired with several Arvid
    messages yields several labels but is still one piece of evidence.
    """
    recent = [
        item for item in labels
        if int(now_ms) - item.labeled_at_ms <= window_ms
        and is_alert_evidence(item, min_confidence=min_confidence)
    ]
    if any(REMOVE_SIGNALS.intersection(item.signals) for item in recent):
        return "asks_to_remove_or_mute"
    annoyed = len({item.message_id for item in recent})
    if annoyed >= max(1, int(threshold)):
        return f"annoyed_cluster_{annoyed}"
    return None


def alert_message(*, chat_label: str, reason: str) -> str:
    """One owner-facing alert. It only informs; it never changes a decision."""
    if reason == "asks_to_remove_or_mute":
        detail = "jemand bittet darum, Arvid zu entfernen oder stummzuschalten"
    else:
        detail = "mehrere genervte oder feindselige Antworten in kurzer Zeit"
    return (
        f"Reception-Alarm in {chat_label}: {detail}. "
        "Nur ein Hinweis - es wurde nichts geändert."
    )


__all__ = [
    "ANNOYED_STANCES",
    "MIN_ALERT_CONFIDENCE",
    "REMOVE_SIGNALS",
    "alert_message",
    "alert_reason",
    "asks_to_remove",
    "is_alert_evidence",
]
