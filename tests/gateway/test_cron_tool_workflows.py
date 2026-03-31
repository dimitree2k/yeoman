"""Tests for CronTool add_workflow and workflow_list actions."""

import tempfile
from pathlib import Path

import pytest

from yeoman_gateway.agent.tools.cron import CronTool
from yeoman_gateway.cron.service import CronService


@pytest.mark.asyncio
async def test_add_workflow_creates_chained_jobs() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cron = CronService(store_path=Path(tmpdir) / "jobs.json")
        await cron.start()
        tool = CronTool(cron)
        tool.set_context("whatsapp", "owner")

        result = await tool.execute(
            action="add_workflow",
            workflow_name="test-wf",
            trigger="0 9 * * 1",
            steps=[
                {"message": "Step 1: gather data"},
                {"message": "Step 2: review", "requires_approval": True},
                {"message": "Step 3: send", "deliver": True, "to": "group-jid"},
            ],
        )

        assert "Created workflow" in result
        jobs = cron.list_jobs(include_disabled=True)
        assert len(jobs) == 3

        # Sort by step for predictable order
        jobs.sort(key=lambda j: j.payload.workflow_step)

        # Verify chain links
        assert jobs[0].payload.next_job_id == jobs[1].id
        assert jobs[1].payload.next_job_id == jobs[2].id
        assert jobs[2].payload.next_job_id is None

        # Verify workflow metadata
        assert all(j.payload.workflow_id == "test-wf" for j in jobs)
        assert jobs[0].payload.workflow_step == 0
        assert jobs[1].payload.workflow_step == 1
        assert jobs[1].payload.requires_approval is True
        assert jobs[1].payload.input_from_previous is True

        cron.stop()


@pytest.mark.asyncio
async def test_workflow_list() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cron = CronService(store_path=Path(tmpdir) / "jobs.json")
        await cron.start()
        tool = CronTool(cron)
        tool.set_context("whatsapp", "owner")

        await tool.execute(
            action="add_workflow",
            workflow_name="my-wf",
            trigger="0 9 * * 1",
            steps=[
                {"message": "Step A"},
                {"message": "Step B"},
            ],
        )

        result = await tool.execute(action="workflow_list")
        assert "my-wf" in result
        assert "Step A" in result or "Step 1" in result

        cron.stop()
