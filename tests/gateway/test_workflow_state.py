# tests/gateway/test_workflow_state.py
"""Tests for WorkflowState pending approval management."""

import tempfile
import time
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.approval import PendingSpeakupApproval, SpeakupApprovalStore
from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState


@pytest.mark.asyncio
async def test_add_and_match_approval() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        state = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        approval = PendingApproval(
            approval_id="wf-approve-abc-12345678",
            next_job_id="job2",
            previous_output="Step 1 output",
            channel="whatsapp",
            chat_id="owner",
            created_at=time.time(),
            expires_at=time.time() + 86400,
            workflow_id="test-wf",
            remaining_depth=4,
        )
        await state.add(approval)

        # Match succeeds and consumes
        matched = await state.match_and_consume("wf-approve-abc-12345678")
        assert matched is not None
        assert matched.next_job_id == "job2"

        # Second match fails (consumed)
        matched2 = await state.match_and_consume("wf-approve-abc-12345678")
        assert matched2 is None


@pytest.mark.asyncio
async def test_expired_approvals_purged() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        state = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        approval = PendingApproval(
            approval_id="wf-approve-old-99999999",
            next_job_id="job2",
            previous_output="old",
            channel="whatsapp",
            chat_id="owner",
            created_at=time.time() - 90000,
            expires_at=time.time() - 1,  # already expired
            workflow_id=None,
            remaining_depth=3,
        )
        await state.add(approval)

        expired = await state.purge_expired()
        assert len(expired) == 1
        assert expired[0].approval_id == "wf-approve-old-99999999"


@pytest.mark.asyncio
async def test_persistence_survives_reload() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "approvals.json"
        state1 = WorkflowState(store_path=path)
        await state1.add(PendingApproval(
            approval_id="wf-approve-persist-11111111",
            next_job_id="job3", previous_output="test", channel="whatsapp",
            chat_id="owner", created_at=time.time(), expires_at=time.time() + 86400,
            workflow_id=None, remaining_depth=2,
        ))

        # New instance loads from disk
        state2 = WorkflowState(store_path=path)
        matched = await state2.match_and_consume("wf-approve-persist-11111111")
        assert matched is not None
        assert matched.next_job_id == "job3"


@pytest.mark.asyncio
async def test_list_pending() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        state = WorkflowState(store_path=Path(tmpdir) / "approvals.json")
        await state.add(PendingApproval(
            approval_id="wf-approve-a-00000001", next_job_id="j1", previous_output="",
            channel="whatsapp", chat_id="owner", created_at=time.time(),
            expires_at=time.time() + 86400, workflow_id="wf1", remaining_depth=3,
        ))
        await state.add(PendingApproval(
            approval_id="wf-approve-b-00000002", next_job_id="j2", previous_output="",
            channel="whatsapp", chat_id="owner", created_at=time.time(),
            expires_at=time.time() + 86400, workflow_id="wf1", remaining_depth=2,
        ))
        pending = await state.list_pending()
        assert len(pending) == 2


def _speakup(proposal_id: str, *, expires_at: float | None = None) -> PendingSpeakupApproval:
    return PendingSpeakupApproval(
        proposal_id=proposal_id,
        target_channel="whatsapp",
        target_chat_id="group@g.us",
        owner_channel="whatsapp",
        owner_chat_id="owner",
        message="hello group",
        action_type="observation",
        profile="careful",
        created_at=time.time(),
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
        context_snapshot={"reason": "test"},
    )


@pytest.mark.asyncio
async def test_speakup_approval_approve_consumes() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SpeakupApprovalStore(store_path=Path(tmpdir) / "speakups.json")
        await store.add(_speakup("abc123"))

        matched = await store.match_and_consume("spk-approve-abc123")
        assert matched is not None
        action, approval = matched
        assert action == "approve"
        assert approval.proposal_id == "abc123"

        assert await store.match_and_consume("spk-approve-abc123") is None


@pytest.mark.asyncio
async def test_speakup_approval_deny_consumes() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SpeakupApprovalStore(store_path=Path(tmpdir) / "speakups.json")
        await store.add(_speakup("abc123"))

        matched = await store.match_and_consume("spk-deny-abc123")
        assert matched is not None
        action, approval = matched
        assert action == "deny"
        assert approval.proposal_id == "abc123"

        pending = await store.list_pending()
        assert pending == []


@pytest.mark.asyncio
async def test_speakup_approval_persists_reloads_and_expires() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "speakups.json"
        store1 = SpeakupApprovalStore(store_path=path)
        await store1.add(_speakup("fresh"))
        await store1.add(_speakup("expired", expires_at=time.time() - 1))

        store2 = SpeakupApprovalStore(store_path=path)
        expired = await store2.purge_expired()
        pending = await store2.list_pending()

        assert [item.proposal_id for item in expired] == ["expired"]
        assert [item.proposal_id for item in pending] == ["fresh"]
        matched = await store2.match_and_consume("spk-approve-fresh")
        assert matched is not None
        assert matched[1].message == "hello group"
