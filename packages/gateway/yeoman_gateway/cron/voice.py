"""Helpers for scheduled voice broadcast behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from yeoman_gateway.cron.types import CronPayload


@dataclass(frozen=True, slots=True)
class VoiceQuietDecision:
    """Decision for whether a quiet-gated voice broadcast may send now."""

    status: Literal["allowed", "defer", "skip"]
    reason: str = ""
    retry_at_ms: int | None = None
    recent_count: int = 0


def evaluate_voice_quiet_gate(
    *,
    payload: CronPayload,
    inbound_archive: object,
    channel: str,
    chat_id: str,
    now: datetime | None = None,
) -> VoiceQuietDecision:
    """Check if a voice broadcast should wait for chat silence before sending."""

    if not payload.voice_wait_for_quiet:
        return VoiceQuietDecision(status="allowed")
    if not channel or not chat_id:
        return VoiceQuietDecision(status="allowed")

    current = _coerce_aware(now or datetime.now(UTC))
    quiet_minutes = _positive_int(payload.voice_quiet_minutes, default=60)
    since = current - timedelta(minutes=quiet_minutes)
    rows = inbound_archive.lookup_messages_in_range(
        channel,
        chat_id,
        since,
        current,
        limit=1,
        latest=True,
    )
    if not rows:
        return VoiceQuietDecision(status="allowed")

    window_end = _window_end_today(payload.voice_window_end, current)
    if window_end is not None and current >= window_end:
        return VoiceQuietDecision(
            status="skip",
            reason="quiet_window_expired",
            recent_count=len(rows),
        )

    retry_minutes = _positive_int(payload.voice_retry_minutes, default=30)
    retry_at = current + timedelta(minutes=retry_minutes)
    if window_end is not None and retry_at > window_end:
        retry_at = window_end
    return VoiceQuietDecision(
        status="defer",
        reason="recent_chat_activity",
        retry_at_ms=int(retry_at.timestamp() * 1000),
        recent_count=len(rows),
    )


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _positive_int(value: int | None, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _window_end_today(value: str | None, now: datetime) -> datetime | None:
    token = str(value or "").strip()
    if not token:
        return None
    try:
        hour_text, minute_text = token.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
