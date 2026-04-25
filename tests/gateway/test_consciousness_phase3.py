"""Tests for Phase 3 outcome and taste loops."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.outcomes import OutcomeEnricher
from yeoman_gateway.consciousness.taste import TasteDistiller
from yeoman_gateway.storage.inbound_archive import InboundArchive


class _FakeMemory:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record_manual(self, **kwargs: object) -> tuple[object, bool]:
        self.records.append(dict(kwargs))
        return object(), True


@pytest.mark.asyncio
async def test_outcome_enricher_classifies_post_speakup_window(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    committed_at = datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp()
    await log.record_sent(
        proposal_id="spk-1",
        channel="whatsapp",
        chat_id="group@g.us",
        action_type="light_humor",
        profile="balanced",
        message="funny finance line",
        trigger="manual",
        context_snapshot={},
        now=committed_at,
    )
    archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="after-1",
        participant="user@s.whatsapp.net",
        sender_id="user@s.whatsapp.net",
        text="haha exactly",
        timestamp=int(committed_at + 60),
    )
    prompts: list[str] = []

    async def classifier(prompt: str) -> dict[str, object]:
        prompts.append(prompt)
        return {"outcome": "replied", "confidence": 0.91}

    enricher = OutcomeEnricher(log=log, inbound_archive=archive, classifier=classifier)

    result = await enricher.run_once(now=datetime.fromtimestamp(committed_at + 3600, UTC))
    history = await log.history("whatsapp", "group@g.us", limit=5)

    assert result == {"classified": 1}
    assert history[0]["outcome"] == "replied"
    assert history[0]["outcome_classified_at"] is not None
    assert "funny finance line" in prompts[0]
    assert "haha exactly" in prompts[0]


@pytest.mark.asyncio
async def test_taste_distiller_writes_patterns_not_raw_messages(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    for index in range(3):
        proposal_id = f"spk-{index}"
        await log.record_sent(
            proposal_id=proposal_id,
            channel="whatsapp",
            chat_id="group@g.us",
            action_type="light_humor",
            profile="balanced",
            message=f"raw joke text {index}",
            trigger="manual",
            context_snapshot={},
            now=(base + timedelta(minutes=index)).timestamp(),
        )
        await log.mark_outcome(
            proposal_id,
            outcome="replied",
            now=(base + timedelta(minutes=index + 10)).timestamp(),
        )
    memory = _FakeMemory()

    async def distiller(prompt: str) -> dict[str, object]:
        assert "raw joke text 0" in prompt
        return {
            "pattern": "Light finance-politics jokes land when they connect a visible cue to a known market meme.",
            "confidence": 0.86,
        }

    taste = TasteDistiller(log=log, memory=memory, distiller=distiller, min_samples=3)

    result = await taste.run_once(channel="whatsapp", chat_id="group@g.us")

    assert result == {"distilled": True, "samples": 3}
    assert len(memory.records) == 1
    record = memory.records[0]
    assert record["scope_type"] == "chat"
    assert record["kind"] == "preference"
    assert "Light finance-politics jokes land" in str(record["text"])
    assert "raw joke text" not in str(record["text"])


@pytest.mark.asyncio
async def test_taste_distiller_waits_for_enough_samples(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_sent(
        proposal_id="spk-1",
        channel="whatsapp",
        chat_id="group@g.us",
        action_type="light_humor",
        profile="balanced",
        message="one example",
        trigger="manual",
        context_snapshot={},
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp(),
    )
    await log.mark_outcome("spk-1", outcome="replied")
    memory = _FakeMemory()
    taste = TasteDistiller(
        log=log,
        memory=memory,
        distiller=lambda prompt: json.loads(prompt),
        min_samples=3,
    )

    result = await taste.run_once(channel="whatsapp", chat_id="group@g.us")

    assert result == {"distilled": False, "reason": "not_enough_samples", "samples": 1}
    assert memory.records == []
