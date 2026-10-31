"""Owner report, weekly digest and validation helper read only stored numbers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.history.group_signals import week_of
from yeoman_gateway.reception.alerts import alert_reason
from yeoman_gateway.reception.owner import OwnerDigestJob
from yeoman_gateway.reception.report import (
    label_agreement,
    persona_before_after,
    render_report,
    resolve_chat_selector,
    trend_arrow,
    weekly_digest,
)
from yeoman_gateway.reception.store import LabelRow, SentimentStore

CHAT = "120363000000000042@g.us"
OTHER = "120363000000000043@g.us"


def _store(tmp_path: Path) -> SentimentStore:
    return SentimentStore(tmp_path / "ops" / "sentiment.db")


def _weekly(store: SentimentStore, chat: str, week: str, *, direct: float,
            silence: float, status: str = "ok") -> None:
    store.store_weekly(
        chat_id=chat, iso_week=week, computed_at_ms=1, status=status,
        persona_file="personas/arvid.md", persona_sha="persona-sha",
        policy_snapshot={"when_to_reply": "all"},
        metrics={
            "arvid_messages": 10, "human_messages": 60, "addressed": 4,
            "unprompted": 6, "direct_rate": direct, "indirect_rate": 0.2,
            "silence_rate": silence, "reactions_per_arvid": 0.5,
            "reactions_per_human": 0.1, "negative_reactions": 2,
            "breadth": 3, "top_person_share": 0.5, "top_person_id": "c-a",
        },
    )


def _label(store: SentimentStore, chat: str, week: str, message: str, *,
           stance: str = "annoyed", signals: tuple[str, ...] = ()) -> None:
    store.store_label(
        chat_id=chat, iso_week=week, arvid_message_id="a-1", message_id=message,
        responds_to_arvid="yes", stance=stance, signals=signals, confidence=0.8,
        model="fake", prompt_version="v1", labeled_at_ms=1,
    )


def test_report_renders_one_table_per_chat_and_insufficient_weeks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _weekly(store, CHAT, "2026-W41", direct=0.5, silence=0.1)
    _weekly(store, CHAT, "2026-W40", direct=0.3, silence=0.4)
    _weekly(store, OTHER, "2026-W41", direct=0.0, silence=1.0, status="insufficient_data")
    _label(store, CHAT, "2026-W41", "m-1", signals=("asks_to_remove_or_mute",))
    text = render_report(
        store, chat_labels={CHAT: "Ente", OTHER: "Finanzgruppe"},
        chat_ids=[CHAT, OTHER], weeks=8,
    )
    assert "Ente" in text and "Finanzgruppe" in text
    assert "direct 50%" in text and "silence 10%" in text
    assert "trend ↑" in text
    assert "stance ann 1" in text and "remove/mute 1" in text
    assert "zu wenig Daten" in text
    assert "personas/arvid.md" not in text  # a basename, not the full path
    store.close()


def test_trend_and_chat_selector() -> None:
    class Row:
        def __init__(self, rate: float, status: str = "ok") -> None:
            self.metrics = {"direct_rate": rate}
            self.status = status

    assert trend_arrow(Row(0.5), Row(0.3)) == "↑"
    assert trend_arrow(Row(0.3), Row(0.5)) == "↓"
    assert trend_arrow(Row(0.5), Row(0.5)) == "→"
    assert trend_arrow(Row(0.5), Row(0.1, "insufficient_data")) == "→"
    assert resolve_chat_selector(
        "Ente", chat_comments={CHAT: "Ente"}, chat_ids=[CHAT]
    ) == CHAT
    assert resolve_chat_selector(CHAT, chat_comments={}, chat_ids=[CHAT]) == CHAT
    assert resolve_chat_selector("missing", chat_comments={}, chat_ids=[CHAT]) is None


def test_persona_before_after_is_added_once_after_seven_days(tmp_path: Path) -> None:
    store = _store(tmp_path)
    changed = int(datetime(2026, 9, 14, tzinfo=UTC).timestamp() * 1000)
    before = week_of(changed - 7 * 24 * 60 * 60 * 1000, tz=UTC)
    after = week_of(changed + 7 * 24 * 60 * 60 * 1000, tz=UTC)
    _weekly(store, CHAT, before.label, direct=0.2, silence=0.5)
    _weekly(store, CHAT, after.label, direct=0.6, silence=0.1)
    store.note_persona(chat_id=CHAT, persona_sha="new-sha", now_ms=changed)
    line = persona_before_after(store, chat_id=CHAT, now_ms=changed + 8 * 24 * 60 * 60 * 1000)
    assert line is not None and "direct 20% → 60%" in line and "silence 50% → 10%" in line
    store.mark_persona_reported(chat_id=CHAT, persona_sha="new-sha", now_ms=changed)
    assert persona_before_after(
        store, chat_id=CHAT, now_ms=changed + 9 * 24 * 60 * 60 * 1000
    ) is None
    store.close()


def test_weekly_digest_uses_live_quotes_without_storing_them(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _weekly(store, CHAT, "2026-W41", direct=0.5, silence=0.1)
    _label(store, CHAT, "2026-W41", "m-1", stance="neutral")
    text, used = weekly_digest(
        store, chat_labels={CHAT: "Ente"}, chat_ids=[CHAT],
        now_ms=int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000),
        quotes={CHAT: ["a live quote"]},
    )
    assert "Ente: Arvid 10" in text and "engaged 90%" in text
    assert "„a live quote“" in text
    assert used == (CHAT,)
    assert b"a live quote" not in store.path.read_bytes()
    store.close()


def test_label_agreement_judges_each_reply_across_its_labels() -> None:
    actual = [
        LabelRow(CHAT, "2026-W41", "a-1", "m-1", "yes", "playful", (), 0.9, "fake", "v1", 1),
        # m-2 followed two Arvid messages: the "yes" label speaks for the reply.
        LabelRow(CHAT, "2026-W41", "a-1", "m-2", "no", "neutral", (), 0.9, "fake", "v1", 1),
        LabelRow(CHAT, "2026-W41", "a-2", "m-2", "yes", "annoyed", (), 0.6, "fake", "v1", 1),
    ]
    report = label_agreement(
        expected=[
            {"message_id": "m-1", "expected_responds": "yes", "expected_stance": "playful"},
            {"message_id": "m-2", "expected_responds": "yes", "expected_stance": "neutral"},
            {"message_id": "m-3", "expected_responds": "yes", "expected_stance": "hostile"},
        ],
        actual=actual,
    )
    assert report["rows"] == 3 and report["missing_labels"] == 1
    assert report["responds_total"] == 2 and report["responds_accuracy"] == 1.0
    assert report["stance_accuracy"] == 0.5
    assert report["negative_accuracy"] == 0.5
    assert report["stance_confusion"] == {"neutral->annoyed": 1, "playful->playful": 1}


@pytest.mark.asyncio
async def test_owner_digest_is_sent_once_per_week_across_restarts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _weekly(store, CHAT, "2026-W41", direct=0.5, silence=0.1)
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    def job() -> OwnerDigestJob:
        return OwnerDigestJob(
            store=store, chats=lambda: [CHAT], chat_labels=lambda: {CHAT: "Ente"},
            send=send, run_hour=0, tz=UTC,
        )

    monday = int(datetime(2026, 10, 12, 9, tzinfo=UTC).timestamp() * 1000)
    assert (await job().tick(now_ms=monday)).sent
    # A fresh job object stands in for a gateway restart.
    assert not (await job().tick(now_ms=monday + 60_000)).sent
    assert len(sent) == 1
    store.close()


def test_alert_reason_uses_signals_and_a_bounded_annoyance_cluster() -> None:
    base = 1_000_000
    remove = LabelRow(CHAT, "2026-W41", "a", "m1", "yes", "neutral",
                      ("asks_to_remove_or_mute",), 0.9, "f", "v1", base)
    annoyed = [
        LabelRow(CHAT, "2026-W41", "a", f"m{i}", "yes", "annoyed", (), 0.9, "f", "v1", base)
        for i in range(2, 4)
    ]
    assert alert_reason([remove], now_ms=base + 1) == "asks_to_remove_or_mute"
    assert alert_reason([*annoyed, remove], now_ms=base + 1) == "asks_to_remove_or_mute"
    assert alert_reason(annoyed, now_ms=base + 1) is None
    assert alert_reason(
        annoyed, now_ms=base + 1, threshold=2
    ) == "annoyed_cluster_2"
    stale = LabelRow(CHAT, "2026-W41", "a", "m9", "yes", "hostile", (), 0.9,
                     "f", "v1", base - 2 * 24 * 60 * 60 * 1000)
    assert alert_reason([*annoyed, stale], now_ms=base + 1, threshold=3) is None
