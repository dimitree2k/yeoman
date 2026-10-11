"""Isolated maintenance: receipts and evidence-based outcomes.

Fake-clock tests over real temporary stores. No provider, no live chat.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.participation_maintenance import (
    EVIDENCE_EXPLICIT,
    EVIDENCE_INFERRED,
    EVIDENCE_NONE,
    ParticipationMaintenance,
    eligible_for_outcome,
)

CHANNEL = "whatsapp"
CHAT = "group@g.us"
WINDOW_MS = 120 * 60_000


def _maintenance(log: SpeakupLog, **kwargs):
    """Use a real History-backed archive under a synthetic consumer lease."""
    from history_turn_fixtures import Projector
    from yeoman_gateway.consciousness.participation_maintenance import ParticipationMaintenance
    from yeoman_gateway.history.export import SecondaryArchive

    projector = Projector()
    for message_id, text in (("m1", "synthetic reply after delivery"), ("m2", "synthetic group context")):
        projector.add(message_id, chat=CHAT, text=text)
    projector.db.execute("UPDATE messages SET sent_ms=1000 WHERE chat_id=?", (CHAT,))
    projector.db.execute("DELETE FROM messages_fts")
    from yeoman_gateway.history.schema import FTS_ROWS

    projector.db.execute("INSERT INTO messages_fts(message_id,chat_id,text) " + FTS_ROWS)
    projector.db.commit()
    maintenance = ParticipationMaintenance(ledger=log, archive=SecondaryArchive(), **kwargs)
    maintenance._history_projector = projector
    return maintenance, projector


def test_observation_window_boundary_arithmetic() -> None:
    assert (
        eligible_for_outcome(delivered_at_ms=0, now_ms=30 * 60_000, window_ms=WINDOW_MS)
        is False
    )
    assert (
        eligible_for_outcome(delivered_at_ms=0, now_ms=120 * 60_000, window_ms=WINDOW_MS)
        is True
    )
    assert (
        eligible_for_outcome(delivered_at_ms=0, now_ms=119 * 60_000, window_ms=WINDOW_MS)
        is False
    )


async def _delivered(
    log: SpeakupLog,
    *,
    proposal_id: str,
    effect_id: str,
    delivered_at_ms: int,
    state: str = "delivered",
) -> None:
    await log.record_proposed(
        proposal_id=proposal_id,
        channel=CHANNEL,
        chat_id=CHAT,
        action_type="observation",
        profile="balanced",
        message="a synthetic statement",
        trigger="burst",
        context_snapshot={},
        now=1.0,
    )
    await log.reserve_delivery(
        proposal_id=proposal_id,
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=delivered_at_ms - 1000,
        limits=(("comment", 1000, 3_600_000),),
        origin="participation",
        lane="production",
    )
    await log.project_transport_accepted(
        proposal_id,
        effect_id=effect_id,
        provider_message_id=f"prov-{effect_id}",
        evidence_kind="transport_receipt",
        evidence_ref="receipt-1",
        now_ms=delivered_at_ms - 500,
    )
    if state == "delivered":
        await log.project_recipient_delivery(
            proposal_id,
            effect_id=effect_id,
            provider_message_id=f"prov-{effect_id}",
            evidence_kind="recipient_delivery",
            evidence_ref="signal-1",
            now_ms=delivered_at_ms,
        )


@pytest.mark.asyncio
async def test_only_completed_recipient_deliveries_are_eligible(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _delivered(log, proposal_id="p1", effect_id="e1", delivered_at_ms=0)
    await _delivered(
        log, proposal_id="p2", effect_id="e2", delivered_at_ms=0, state="accepted"
    )
    # At 30 minutes nothing is eligible yet.
    rows = await log.pending_outcome_deliveries(before_ms=30 * 60_000 - WINDOW_MS + 1)
    assert rows == []
    # At the completed window only the delivered row is a candidate.
    rows = await log.pending_outcome_deliveries(before_ms=120 * 60_000 - WINDOW_MS)
    assert [row["effect_id"] for row in rows] == ["e1"]
    log.close()


@pytest.mark.asyncio
async def test_unverified_states_never_become_samples(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    for index, state in enumerate(("accepted", "failed", "unknown")):
        await _delivered(
            log,
            proposal_id=f"p{index}",
            effect_id=f"e{index}",
            delivered_at_ms=0,
            state=state,
        )
    rows = await log.pending_outcome_deliveries(before_ms=10**9)
    assert rows == []
    assert await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    ) == []
    log.close()


@pytest.mark.asyncio
async def test_explicit_feedback_is_used_without_a_classifier_call(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _delivered(log, proposal_id="p1", effect_id="e1", delivered_at_ms=0)

    calls: list[str] = []

    def _classifier(prompt: str) -> str:
        calls.append(prompt)
        return '{"outcome": "replied"}'

    async def _reader(*, channel: str, chat_id: str, message_id: str):
        del channel, chat_id, message_id
        return {"kind": "reply", "event_id": "quote-1"}

    log.set_explicit_feedback_reader(_reader)
    maintenance, projector = _maintenance(
        log, classifier=_classifier, observation_window_minutes=120
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.outcomes_classified == 1
    assert report.classified_without_a_call == 1
    assert calls == []
    samples = await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    )
    assert len(samples) == 1
    assert samples[0]["outcome"] == "replied"
    assert samples[0]["outcome_kind"] == EVIDENCE_EXPLICIT
    projector.close()
    log.close()


@pytest.mark.asyncio
async def test_absence_of_feedback_is_recorded_without_a_rejection(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _delivered(log, proposal_id="p1", effect_id="e1", delivered_at_ms=0)

    calls: list[str] = []

    def _classifier(prompt: str) -> str:
        calls.append(prompt)
        return '{"outcome": "no_observed_feedback"}'

    maintenance, projector = _maintenance(
        log, classifier=_classifier, observation_window_minutes=120
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.outcomes_classified == 1
    samples = await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    )
    assert samples[0]["outcome"] == "no_observed_feedback"
    assert samples[0]["outcome_kind"] == EVIDENCE_NONE
    projector.close()
    log.close()


@pytest.mark.asyncio
async def test_inferred_relation_carries_evidence_ids(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _delivered(log, proposal_id="p1", effect_id="e1", delivered_at_ms=0)

    def _classifier(prompt: str) -> str:
        del prompt
        return '{"outcome": "topic_changed", "evidence_ids": ["m1", "m2"]}'

    maintenance, projector = _maintenance(
        log, classifier=_classifier, observation_window_minutes=120
    )
    await maintenance.run_once(now_ms=WINDOW_MS)
    samples = await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    )
    assert samples[0]["outcome_kind"] == EVIDENCE_INFERRED
    assert "m1" in str(samples[0]["outcome_evidence_json"])
    projector.close()
    log.close()


@pytest.mark.asyncio
async def test_invalid_classifier_output_is_a_failed_attempt_not_an_outcome(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _delivered(log, proposal_id="p1", effect_id="e1", delivered_at_ms=0)

    def _classifier(prompt: str) -> str:
        del prompt
        return "not json"

    maintenance, projector = _maintenance(
        log, classifier=_classifier, observation_window_minutes=120
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.outcomes_classified == 0
    assert report.failures == 1
    samples = await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    )
    assert samples == []
    # The row is still eligible for a retry; nothing was written.
    rows = await log.pending_outcome_deliveries(before_ms=WINDOW_MS)
    assert len(rows) == 1
    projector.close()
    log.close()


@pytest.mark.asyncio
async def test_one_failing_sample_does_not_discard_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    for index in range(3):
        await _delivered(
            log, proposal_id=f"p{index}", effect_id=f"e{index}", delivered_at_ms=0
        )

    seen: list[str] = []

    def _classifier(prompt: str) -> str:
        seen.append(prompt)
        # The first sample fails; the remaining two still get classified.
        if len(seen) == 1:
            raise RuntimeError("classifier down")
        return '{"outcome": "replied"}'

    maintenance, projector = _maintenance(
        log, classifier=_classifier, observation_window_minutes=120
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.deliveries_seen == 3
    assert report.outcomes_classified == 2
    assert report.failures == 1
    assert len(seen) == 3
    projector.close()
    log.close()


@pytest.mark.asyncio
async def test_reconciliation_runs_inside_maintenance_and_is_optional(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")

    class _Reconciler:
        def __init__(self) -> None:
            self.calls = 0

        async def reconcile(self, *, limit: int, now_ms: int) -> dict[str, int]:
            self.calls += 1
            del limit, now_ms
            return {"accepted": 1}

    reconciler = _Reconciler()
    maintenance, projector = _maintenance(
        log, reconciler=reconciler, observation_window_minutes=120
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.reconciled == {"accepted": 1}
    assert reconciler.calls == 1

    broken = ParticipationMaintenance(
        ledger=log,
        reconciler=_BrokenReconciler(),
        observation_window_minutes=120,
    )
    broken._history_projector = projector
    survived = await broken.run_once(now_ms=WINDOW_MS)
    assert survived.reconciled == {}
    projector.close()
    log.close()


class _BrokenReconciler:
    async def reconcile(self, *, limit: int, now_ms: int) -> dict[str, int]:
        del limit, now_ms
        raise RuntimeError("store unavailable")


@pytest.mark.asyncio
async def test_batch_size_bounds_one_pass(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    for index in range(5):
        await _delivered(
            log, proposal_id=f"p{index}", effect_id=f"e{index}", delivered_at_ms=0
        )
    maintenance, projector = _maintenance(
        log, classifier=lambda prompt: '{"outcome": "replied"}',
        observation_window_minutes=120, batch_size=2,
    )
    report = await maintenance.run_once(now_ms=WINDOW_MS)
    assert report.deliveries_seen == 2
    assert report.outcomes_classified == 2
    projector.close()
    log.close()


