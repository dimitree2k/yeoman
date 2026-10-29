"""Consecutive cron failures: counted, persisted, alerted at most daily, never disabled."""

import pytest
from yeoman_gateway.agent.tools.cron import CronTool
from yeoman_gateway.cron import service as cron_service
from yeoman_gateway.cron.service import FAILURE_ALERT_INTERVAL_MS, CronService
from yeoman_gateway.cron.types import CronSchedule


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def cron(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cron_service, "_now_ms", clock)
    service = CronService(store_path=tmp_path / "jobs.json")
    alerts: list[tuple[str, bool, int]] = []
    outcome = {"fail": True}

    async def on_job(job):
        if outcome["fail"]:
            raise RuntimeError("provider down")
        return "done"

    async def on_alert(job, recovered):
        alerts.append((job.id, recovered, job.state.consecutive_failures))

    service.on_job = on_job
    service.on_failure_alert = on_alert
    job = service.add_job("digest", CronSchedule(kind="every", every_ms=60_000), "summarize")
    return service, job, clock, alerts, outcome


@pytest.mark.asyncio
async def test_failures_alert_once_a_day_and_never_disable(cron):
    service, job, clock, alerts, _ = cron

    for _ in range(12):
        await service._execute_job(job)
        clock.now += 60_000

    assert job.state.consecutive_failures == 12
    assert job.enabled is True
    assert alerts == [(job.id, False, 1)]

    clock.now += FAILURE_ALERT_INTERVAL_MS
    await service._execute_job(job)
    assert alerts == [(job.id, False, 1), (job.id, False, 13)]


@pytest.mark.asyncio
async def test_recovery_alerts_once_and_resets(cron):
    service, job, clock, alerts, outcome = cron
    await service._execute_job(job)
    await service._execute_job(job)
    outcome["fail"] = False

    await service._execute_job(job)
    await service._execute_job(job)

    assert alerts == [(job.id, False, 1), (job.id, True, 2)]
    assert job.state.consecutive_failures == 0
    assert job.state.failure_alerted_at_ms is None


@pytest.mark.asyncio
async def test_failing_alert_callback_does_not_break_the_run(cron):
    service, job, *_ = cron

    async def broken(job, recovered):
        raise RuntimeError("no route")

    service.on_failure_alert = broken
    await service._execute_job(job)
    assert job.state.last_status == "error"
    assert job.state.next_run_at_ms is not None


@pytest.mark.asyncio
async def test_failure_count_persists_and_shows_in_list(cron, tmp_path):
    service, job, *_ = cron
    await service._execute_job(job)
    await service._execute_job(job)
    service._save_store()

    reloaded = CronService(store_path=tmp_path / "jobs.json").list_jobs()[0]
    assert reloaded.state.consecutive_failures == 2
    assert reloaded.state.failure_alerted_at_ms is not None
    assert "2 failed in a row" in CronTool(CronService(store_path=tmp_path / "jobs.json"))._list_jobs()


def test_reenable_resets_failures(cron):
    service, job, *_ = cron
    job.state.consecutive_failures = 4
    job.state.failure_alerted_at_ms = 5
    service.enable_job(job.id, enabled=False)
    service.enable_job(job.id, enabled=True)
    assert job.state.consecutive_failures == 0
    assert job.state.failure_alerted_at_ms is None


@pytest.mark.asyncio
async def test_error_without_message_records_its_type(cron):
    service, job, *_ = cron

    async def silent(job):
        raise TimeoutError()

    service.on_job = silent
    await service._execute_job(job)
    assert job.state.last_error == "TimeoutError"
