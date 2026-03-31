# tests/gateway/test_workflow_types.py
"""Tests for CronPayload workflow fields."""

import tempfile
from pathlib import Path

from yeoman_gateway.cron.service import CronService
from yeoman_gateway.cron.types import CronPayload, CronSchedule


def test_payload_default_workflow_fields() -> None:
    p = CronPayload()
    assert p.next_job_id is None
    assert p.requires_approval is False
    assert p.approval_channel is None
    assert p.input_from_previous is False
    assert p.workflow_id is None
    assert p.workflow_step == 0
    assert p.max_chain_depth == 5


def test_payload_with_workflow_fields() -> None:
    p = CronPayload(
        message="step 2",
        next_job_id="abc123",
        requires_approval=True,
        approval_channel="whatsapp",
        input_from_previous=True,
        workflow_id="weekly-summary",
        workflow_step=1,
        max_chain_depth=3,
    )
    assert p.next_job_id == "abc123"
    assert p.requires_approval is True
    assert p.workflow_step == 1
    assert p.max_chain_depth == 3
    assert p.approval_channel == "whatsapp"
    assert p.input_from_previous is True
    assert p.workflow_id == "weekly-summary"


def test_workflow_fields_survive_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = Path(tmpdir) / "cron.json"
        svc = CronService(store_path=store_path)
        job = svc.add_job(
            name="test",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            message="hello",
        )
        job.payload.next_job_id = "xyz"
        job.payload.requires_approval = True
        job.payload.approval_channel = "telegram"
        job.payload.input_from_previous = True
        job.payload.workflow_id = "wf-1"
        job.payload.workflow_step = 2
        job.payload.max_chain_depth = 10
        svc._save_store()

        svc2 = CronService(store_path=store_path)
        jobs = svc2.list_jobs()
        assert len(jobs) == 1
        p = jobs[0].payload
        assert p.next_job_id == "xyz"
        assert p.requires_approval is True
        assert p.approval_channel == "telegram"
        assert p.input_from_previous is True
        assert p.workflow_id == "wf-1"
        assert p.workflow_step == 2
        assert p.max_chain_depth == 10
