"""Weekly P1 service runs in a history turn and stores numbers only."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
from yeoman_gateway.history.group_signals import iso_week, week_moment_ms
from yeoman_gateway.reception.metrics import (
    ReceptionBounds,
    ReceptionMetricsService,
    ReceptionWeeklyJob,
    reception_policy_snapshot,
)
from yeoman_gateway.reception.store import SentimentStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120363000000000066@g.us"
WEEK = iso_week("2026-W40", tz=UTC)
SECRET = "synthetischer satz der nie gespeichert wird"


async def _history(tmp_path: Path) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    base = WEEK.start_ms // 1000 + 9 * 3600
    history.add_message(
        chat_id=CHAT, message_id="human-1", text=f"{SECRET} frage", timestamp=base,
        sender_id="491510000001@s.whatsapp.net",
    )
    for index in range(5):
        history.add_message(
            chat_id=CHAT, message_id=f"out-{index}", text=f"antwort {index}",
            timestamp=base + 60 + index * 120, direction="out",
        )
    history.add_message(
        chat_id=CHAT, message_id="human-2", text="danke", timestamp=base + 61 + 120,
        sender_id="491510000001@s.whatsapp.net", reply_to_message_id="out-1",
    )
    return history


@pytest.mark.asyncio
async def test_compute_week_stores_numbers_and_never_text(tmp_path: Path) -> None:
    history = await _history(tmp_path)
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    service = ReceptionMetricsService(
        store=store, history=history, bounds=ReceptionBounds(),
        policy_context=lambda chat_id: (
            "personas/arvid.md", "persona-sha",
            reception_policy_snapshot(
                type("P", (), {"persona_file": "personas/arvid.md",
                               "when_to_reply_mode": "all"})(),
                persona_sha_value="persona-sha",
            ),
        ),
    )
    try:
        outcome = await service.compute_week(CHAT, WEEK)
        assert outcome.arvid_messages == 5 and outcome.human_messages == 2
        assert outcome.status == "ok"  # five Arvid messages meet the Arvid floor
        row = store.weekly(CHAT)[0]
        assert row.iso_week == WEEK.label and row.persona_sha == "persona-sha"
        assert row.metrics["arvid_messages"] == 5
        assert row.metrics["direct"] == 1
        assert "windows" in row.metrics and "text" not in row.metrics
        assert SECRET.encode() not in store.path.read_bytes()
        changes = store.persona_changes(CHAT)
        assert changes and changes[0][0] == "persona-sha" and changes[0][1] > 0

        report = await ReceptionWeeklyJob(
            service=service, chats=lambda: (CHAT,)
        ).tick(now_ms=week_moment_ms("2026-W41", hours=5, tz=UTC))
        assert report.skipped == 1  # the completed week is already stored
    finally:
        history.close()
        store.close()
