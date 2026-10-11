"""Tests for quiet-window handling on scheduled voice broadcasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.cron.service import CronJobDeferredError, CronService
from yeoman_gateway.cron.types import CronPayload, CronSchedule
from yeoman_gateway.cron.voice import evaluate_voice_quiet_gate

from tests.gateway.history_test_helpers import ProjectedHistory


def _archive(tmp_path: Path) -> ProjectedHistory:
    return ProjectedHistory(tmp_path / "history")


async def _evaluate(
    history: ProjectedHistory,
    *,
    payload: CronPayload,
    channel: str,
    chat_id: str,
    now: datetime,
):
    from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
    from yeoman_gateway.history.queries import HistoryQueries

    async with history.turn() as snapshot:
        return evaluate_voice_quiet_gate(
            payload=payload,
            inbound_archive=HistoryReplyArchiveAdapter(HistoryQueries(snapshot)),
            channel=channel,
            chat_id=chat_id,
            now=now,
        )


def _voice_payload(**overrides: object) -> CronPayload:
    payload = CronPayload(
        kind="voice_broadcast",
        voice_wait_for_quiet=True,
        voice_quiet_minutes=60,
        voice_retry_minutes=30,
        voice_window_end="11:30",
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    return payload


@pytest.mark.asyncio
async def test_quiet_gate_allows_voice_when_chat_has_been_silent(tmp_path: Path) -> None:
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    archive = _archive(tmp_path)
    archive.add_message(
        chat_id="group@g.us",
        message_id="old",
        participant="4691000000001@s.whatsapp.net",
        sender_id="4691000000001@s.whatsapp.net",
        text="old topic",
        timestamp=(now - timedelta(hours=3)).timestamp(),
    )

    decision = await _evaluate(
        archive,
        payload=_voice_payload(),
        channel="whatsapp",
        chat_id="group@g.us",
        now=now,
    )

    assert decision.status == "allowed"
    assert decision.retry_at_ms is None


@pytest.mark.asyncio
async def test_quiet_gate_defers_voice_when_chat_is_active_inside_window(tmp_path: Path) -> None:
    now = datetime(2026, 5, 8, 8, 30, tzinfo=UTC)
    archive = _archive(tmp_path)
    archive.add_message(
        chat_id="group@g.us",
        message_id="recent",
        participant="4691000000001@s.whatsapp.net",
        sender_id="4691000000001@s.whatsapp.net",
        text="active topic",
        timestamp=(now - timedelta(minutes=10)).timestamp(),
    )

    decision = await _evaluate(
        archive,
        payload=_voice_payload(),
        channel="whatsapp",
        chat_id="group@g.us",
        now=now,
    )

    assert decision.status == "defer"
    assert decision.reason == "recent_chat_activity"
    assert decision.recent_count == 1
    assert decision.retry_at_ms == int((now + timedelta(minutes=30)).timestamp() * 1000)


@pytest.mark.asyncio
async def test_quiet_gate_skips_voice_after_window_end_when_chat_is_still_active(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 5, 8, 11, 45, tzinfo=UTC)
    archive = _archive(tmp_path)
    archive.add_message(
        chat_id="group@g.us",
        message_id="recent",
        participant="4691000000001@s.whatsapp.net",
        sender_id="4691000000001@s.whatsapp.net",
        text="still active",
        timestamp=(now - timedelta(minutes=5)).timestamp(),
    )

    decision = await _evaluate(
        archive,
        payload=_voice_payload(),
        channel="whatsapp",
        chat_id="group@g.us",
        now=now,
    )

    assert decision.status == "skip"
    assert decision.reason == "quiet_window_expired"
    assert decision.retry_at_ms is None


@pytest.mark.asyncio
async def test_cron_deferred_job_keeps_retry_time(tmp_path: Path) -> None:
    now_ms = int(datetime(2026, 5, 8, 8, 30, tzinfo=UTC).timestamp() * 1000)
    retry_ms = int(datetime(2026, 5, 8, 9, 0, tzinfo=UTC).timestamp() * 1000)

    async def on_job(_job) -> str | None:
        raise CronJobDeferredError("recent_chat_activity", retry_at_ms=retry_ms)

    cron = CronService(store_path=tmp_path / "jobs.json", on_job=on_job)
    job = cron.add_voice_job(
        name="Weekly Fun Voice",
        schedule=CronSchedule(kind="at", at_ms=now_ms),
        messages=["fallback"],
        chat_id="group@g.us",
    )

    assert await cron.run_job(job.id) is True

    deferred = cron.get_job(job.id)
    assert deferred is not None
    assert deferred.enabled is True
    assert deferred.state.last_status == "skipped"
    assert deferred.state.last_error == "recent_chat_activity"
    assert deferred.state.next_run_at_ms == retry_ms


def test_voice_quiet_and_generation_settings_persist(tmp_path: Path) -> None:
    cron = CronService(store_path=tmp_path / "jobs.json")
    created = cron.add_voice_job(
        name="Weekly Fun Voice",
        schedule=CronSchedule(kind="cron", expr="0 8 * * 5"),
        messages=["fallback"],
        chat_id="group@g.us",
        wait_for_quiet=True,
        quiet_minutes=75,
        retry_minutes=20,
        window_end="11:00",
        generate=True,
        prompt="Generate a fresh Finanzgruppe voice line.",
    )

    reloaded = CronService(store_path=tmp_path / "jobs.json").get_job(created.id)

    assert reloaded is not None
    assert reloaded.payload.voice_wait_for_quiet is True
    assert reloaded.payload.voice_quiet_minutes == 75
    assert reloaded.payload.voice_retry_minutes == 20
    assert reloaded.payload.voice_window_end == "11:00"
    assert reloaded.payload.voice_generate is True
    assert reloaded.payload.voice_prompt == "Generate a fresh Finanzgruppe voice line."
