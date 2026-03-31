# tests/gateway/test_approval_middleware.py
"""Tests for ApprovalMiddleware."""

import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState
from yeoman_gateway.pipeline.approval import ApprovalMiddleware


def _make_ctx(content: str, is_owner: bool = True) -> PipelineContext:
    event = InboundEvent(
        channel="whatsapp", sender_id="owner", chat_id="123",
        content=content, timestamp="2026-01-01T00:00:00Z",
    )
    decision = PolicyDecision(
        accept_message=True, should_respond=True, allowed_tools=[],
        reason="test", is_owner=is_owner,
    )
    ctx = PipelineContext(event=event)
    ctx.decision = decision
    return ctx


@pytest.mark.asyncio
async def test_approval_code_consumed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        await ws.add(PendingApproval(
            approval_id="wf-approve-test-abcd1234",
            next_job_id="job2", previous_output="output",
            channel="whatsapp", chat_id="owner",
            created_at=time.time(), expires_at=time.time() + 86400,
            workflow_id="test", remaining_depth=3,
        ))

        triggered_jobs: list[str] = []

        async def mock_trigger(approval: PendingApproval) -> None:
            triggered_jobs.append(approval.next_job_id)

        mw = ApprovalMiddleware(workflow_state=ws, trigger_callback=mock_trigger)
        ctx = _make_ctx("wf-approve-test-abcd1234")
        next_fn = AsyncMock()
        await mw(ctx, next_fn)

        assert ctx.halted is True
        next_fn.assert_not_called()
        assert triggered_jobs == ["job2"]


@pytest.mark.asyncio
async def test_non_owner_message_passes_through() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        await ws.add(PendingApproval(
            approval_id="wf-approve-test-abcd1234",
            next_job_id="job2", previous_output="output",
            channel="whatsapp", chat_id="owner",
            created_at=time.time(), expires_at=time.time() + 86400,
            workflow_id="test", remaining_depth=3,
        ))

        mw = ApprovalMiddleware(workflow_state=ws, trigger_callback=AsyncMock())
        ctx = _make_ctx("wf-approve-test-abcd1234", is_owner=False)
        next_fn = AsyncMock()
        await mw(ctx, next_fn)

        assert ctx.halted is not True
        next_fn.assert_called_once()


@pytest.mark.asyncio
async def test_non_matching_message_passes_through() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        mw = ApprovalMiddleware(workflow_state=ws, trigger_callback=AsyncMock())
        ctx = _make_ctx("just a normal message")
        next_fn = AsyncMock()
        await mw(ctx, next_fn)

        assert ctx.halted is not True
        next_fn.assert_called_once()
