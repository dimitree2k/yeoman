"""Annoyance alerts: labeler evidence only, at most one alert per chat per day."""

from __future__ import annotations

from collections.abc import Iterable

from yeoman_gateway.reception.store import ALERT_WINDOW_MS, LabelRow

ANNOYED_STANCES = frozenset({"annoyed", "hostile"})
REMOVE_SIGNALS = frozenset({"asks_to_remove_or_mute"})


def alert_reason(
    labels: Iterable[LabelRow],
    *,
    now_ms: int,
    threshold: int = 3,
    window_ms: int = ALERT_WINDOW_MS,
) -> str | None:
    """``asks_to_remove_or_mute`` or a cluster of annoyed/hostile labels, else None."""
    recent = [item for item in labels if int(now_ms) - item.labeled_at_ms <= window_ms]
    if any(REMOVE_SIGNALS.intersection(item.signals) for item in recent):
        return "asks_to_remove_or_mute"
    annoyed = sum(1 for item in recent if item.stance in ANNOYED_STANCES)
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


__all__ = ["ANNOYED_STANCES", "REMOVE_SIGNALS", "alert_message", "alert_reason"]
