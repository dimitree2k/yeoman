"""Plan 06 / Aufgabe 2: pipeline timings stay bounded and never carry content."""

from __future__ import annotations

import pytest
from yeoman_gateway.processing.timings import (
    PHASES,
    PhaseTimings,
    TimingsReport,
    compare_reports,
    median,
    percentile,
)


def test_percentile_is_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

    assert median(values) == 50.0
    assert percentile(values, 0.95) == 100.0
    assert percentile([], 0.5) == 0.0
    assert percentile([42.0], 0.95) == 42.0
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 1.0) == 100.0


def test_every_reported_phase_is_recordable() -> None:
    timings = PhaseTimings()

    for phase in PHASES:
        timings.observe(phase, 1.0)

    report = timings.report()
    assert set(report.medians) == set(PHASES)
    assert timings.rejected_labels == 0


def test_unknown_phase_is_refused_instead_of_inventing_a_series() -> None:
    timings = PhaseTimings()

    timings.observe("model:chat-12345@s.whatsapp.net", 5.0)  # a would-be content label

    assert timings.report().medians == {}
    assert timings.rejected_labels == 1


def test_samples_are_bounded_per_phase() -> None:
    timings = PhaseTimings(capacity=3)

    for value in range(1, 8):
        timings.observe("model", float(value))

    assert timings.sample_count("model") == 3
    assert timings.report().medians["model"] == median([5.0, 6.0, 7.0])


def test_phase_context_manager_records_the_block() -> None:
    ticks = iter([0.0, 0.25])
    timings = PhaseTimings(clock=lambda: next(ticks))

    with timings.phase("transport_confirm"):
        pass

    assert timings.report().medians["transport_confirm"] == 250.0


def test_counters_are_reported_for_drops_and_deferrals() -> None:
    timings = PhaseTimings()
    timings.note_drop("queue_full")
    timings.note_drop("queue_full")
    timings.note_deferral("budget_exhausted")

    report = timings.report()

    assert report.dropped == {"queue_full": 2}
    assert report.deferred == {"budget_exhausted": 1}
    assert "dropped[queue_full]=2" in report.as_lines()
    assert "deferred[budget_exhausted]=1" in report.as_lines()


def test_report_lines_contain_only_phase_names_and_numbers() -> None:
    timings = PhaseTimings()
    timings.observe("model", 12.5)
    timings.note_deferral("thread_soft_limit")

    lines = timings.report().as_lines()

    assert all(line.split(":")[0].split("[")[0] in PHASES or line.startswith("deferred") for line in lines)
    assert any(line.startswith("model: median=12.5ms") for line in lines)


def test_reset_clears_samples_and_counters() -> None:
    timings = PhaseTimings()
    timings.observe("model", 1.0)
    timings.note_drop("queue_full")

    timings.reset()

    assert timings.report().medians == {}
    assert timings.report().dropped == {}


def test_compare_reports_skips_unmeasured_phases() -> None:
    legacy = TimingsReport(medians={"model": 10.0}, p95={"model": 20.0}, samples={"model": 5})
    managed = TimingsReport(
        medians={"model": 12.0}, p95={"model": 25.0}, samples={"model": 5},
        deferred={"budget_exhausted": 2},
    )

    lines = compare_reports(legacy, managed)

    assert len(lines) == 2
    assert "model: legacy median=10.0ms p95=20.0ms | managed median=12.0ms p95=25.0ms" in lines
    assert "deferred[budget_exhausted]: legacy=0 managed=2" in lines
    assert not any(line.startswith("ingest") for line in lines)  # never sampled, not zero


@pytest.mark.asyncio
async def test_dispatcher_records_its_phase_and_counts_a_blocked_effect() -> None:
    import types

    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.processing.dispatch import (
        EffectNotDeliveredError,
        ManagedOutboundDispatcher,
    )

    class _Router:
        def __init__(self, state: str, detail: str | None = None) -> None:
            self._state = state
            self._detail = detail

        def manages(self, channel: str, chat_id: str) -> bool:
            return True

        async def submit_message(self, message, *, principal, capability, payload):
            return types.SimpleNamespace(
                state=self._state, detail=self._detail, effect_id="fx1"
            )

    class _Bus:
        async def publish_outbound(self, message) -> None:  # pragma: no cover - unused
            raise AssertionError("managed chat must not use the legacy publish")

    message = OutboundMessage(channel="whatsapp", chat_id="chat-1", content="hi")

    sent = PhaseTimings()
    await ManagedOutboundDispatcher(router=_Router("sent"), bus=_Bus(), timings=sent)(message)
    assert sent.sample_count("effect_queue") == 1

    blocked = PhaseTimings()
    with pytest.raises(EffectNotDeliveredError):
        await ManagedOutboundDispatcher(
            router=_Router("blocked", "queue_capacity"), bus=_Bus(), timings=blocked
        )(message)
    assert blocked.report().deferred == {"queue_capacity": 1}
    assert blocked.sample_count("effect_queue") == 1  # the attempt is still measured

    # Without instrumentation the dispatcher behaves exactly as before.
    plain = ManagedOutboundDispatcher(router=_Router("sent"), bus=_Bus())
    await plain(message)
