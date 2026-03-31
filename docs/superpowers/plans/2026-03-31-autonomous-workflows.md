# Autonomous Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable multi-step autonomous workflows by extending the existing CronService with job chaining, approval gates, output passing, and a batch `add_workflow` tool action.

**Architecture:** Add workflow fields to `CronPayload`, build a `WorkflowState` manager for pending approvals (JSON file with asyncio.Lock), extend `on_cron_job()` in bootstrap to handle chaining/approval, add `ApprovalMiddleware` to the pipeline, and expose `add_workflow`/`workflow_list` actions in the CronTool.

**Tech Stack:** Python dataclasses, JSON persistence (stdlib), uuid4 (stdlib), asyncio.Lock

**Depends on:** Event Backbone (Plan 1) — the gateway socket must be in place for overseer-initiated workflow triggers. However, all tasks below can be implemented and tested independently using `CronService` + `process_direct()`.

---

### Task 1: Extend CronPayload with Workflow Fields

**Files:**
- Modify: `packages/gateway/yeoman_gateway/cron/types.py`
- Test: `tests/gateway/test_workflow_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_workflow_types.py
"""Tests for CronPayload workflow fields."""

from yeoman_gateway.cron.types import CronPayload


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_workflow_types.py -v`
Expected: FAIL — `TypeError: CronPayload.__init__() got an unexpected keyword argument 'next_job_id'`

- [ ] **Step 3: Add workflow fields to CronPayload**

In `packages/gateway/yeoman_gateway/cron/types.py`, add fields to `CronPayload` after line 42 (`model_profile`):

```python
    # Workflow chaining
    next_job_id: str | None = None
    requires_approval: bool = False
    approval_channel: str | None = None
    input_from_previous: bool = False
    # Workflow metadata
    workflow_id: str | None = None
    workflow_step: int = 0
    max_chain_depth: int = 5
```

- [ ] **Step 4: Update CronService serialization**

In `packages/gateway/yeoman_gateway/cron/service.py`, update `_load_store` (around line 79) to read the new fields from JSON:

After `model_profile=j["payload"].get("modelProfile"),` add:
```python
                            next_job_id=j["payload"].get("nextJobId"),
                            requires_approval=bool(j["payload"].get("requiresApproval", False)),
                            approval_channel=j["payload"].get("approvalChannel"),
                            input_from_previous=bool(j["payload"].get("inputFromPrevious", False)),
                            workflow_id=j["payload"].get("workflowId"),
                            workflow_step=int(j["payload"].get("workflowStep", 0)),
                            max_chain_depth=int(j["payload"].get("maxChainDepth", 5)),
```

Update `_save_store` (around line 153) to write the new fields:

After `"modelProfile": j.payload.model_profile,` add:
```python
                        "nextJobId": j.payload.next_job_id,
                        "requiresApproval": j.payload.requires_approval,
                        "approvalChannel": j.payload.approval_channel,
                        "inputFromPrevious": j.payload.input_from_previous,
                        "workflowId": j.payload.workflow_id,
                        "workflowStep": j.payload.workflow_step,
                        "maxChainDepth": j.payload.max_chain_depth,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_workflow_types.py -v`
Expected: 2 passed

- [ ] **Step 6: Run existing cron tests to verify nothing broke**

Run: `uv run python -m pytest tests/gateway/ -k cron -v`
Expected: All existing cron tests still pass

- [ ] **Step 7: Commit**

```bash
git add packages/gateway/yeoman_gateway/cron/types.py packages/gateway/yeoman_gateway/cron/service.py tests/gateway/test_workflow_types.py
git commit -m "feat(cron): add workflow chaining fields to CronPayload"
```

---

### Task 2: WorkflowState Manager

**Files:**
- Create: `packages/gateway/yeoman_gateway/cron/workflow_state.py`
- Test: `tests/gateway/test_workflow_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_workflow_state.py
"""Tests for WorkflowState pending approval management."""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_workflow_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.cron.workflow_state'`

- [ ] **Step 3: Implement WorkflowState**

Create `packages/gateway/yeoman_gateway/cron/workflow_state.py`:

```python
"""Persistent state for workflow approval gates."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger


@dataclass
class PendingApproval:
    """A pending workflow approval gate."""

    approval_id: str
    next_job_id: str
    previous_output: str
    channel: str
    chat_id: str
    created_at: float
    expires_at: float
    workflow_id: str | None
    remaining_depth: int


class WorkflowState:
    """Manages pending workflow approvals with atomic JSON persistence."""

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()
        self._approvals: list[PendingApproval] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._approvals = [PendingApproval(**item) for item in data.get("approvals", [])]
        except Exception as e:
            logger.warning("Failed to load workflow state: {}", e)
            self._approvals = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"approvals": [asdict(a) for a in self._approvals]}
        # Atomic write: temp file + rename
        tmp_fd = tempfile.NamedTemporaryFile(
            mode="w", dir=self._path.parent, suffix=".tmp", delete=False
        )
        try:
            json.dump(data, tmp_fd, indent=2)
            tmp_fd.close()
            Path(tmp_fd.name).rename(self._path)
        except Exception:
            Path(tmp_fd.name).unlink(missing_ok=True)
            raise

    async def add(self, approval: PendingApproval) -> None:
        async with self._lock:
            self._approvals.append(approval)
            self._save()

    async def match_and_consume(self, text: str) -> PendingApproval | None:
        async with self._lock:
            for i, a in enumerate(self._approvals):
                if a.approval_id == text:
                    consumed = self._approvals.pop(i)
                    self._save()
                    return consumed
            return None

    async def purge_expired(self) -> list[PendingApproval]:
        async with self._lock:
            now = time.time()
            expired = [a for a in self._approvals if a.expires_at < now]
            if expired:
                self._approvals = [a for a in self._approvals if a.expires_at >= now]
                self._save()
            return expired

    async def list_pending(self) -> list[PendingApproval]:
        async with self._lock:
            return list(self._approvals)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_workflow_state.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/cron/workflow_state.py tests/gateway/test_workflow_state.py
git commit -m "feat(cron): add WorkflowState manager for pending approvals"
```

---

### Task 3: Workflow Chaining in on_cron_job()

**Files:**
- Modify: `packages/gateway/yeoman_gateway/app/bootstrap.py`
- Test: `tests/gateway/test_workflow_chaining.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_workflow_chaining.py
"""Tests for workflow chaining logic."""

import pytest

from yeoman_gateway.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def test_chain_depth_decrement() -> None:
    """Verify max_chain_depth is decremented before passing to next job."""
    payload = CronPayload(message="step 1", max_chain_depth=3, next_job_id="next")
    remaining = payload.max_chain_depth - 1
    assert remaining == 2


def test_output_truncation() -> None:
    """Verify output is truncated at 4000 chars for input_from_previous."""
    from yeoman_gateway.cron.workflow_chain import build_chained_prompt

    long_output = "x" * 5000
    prompt = build_chained_prompt(long_output, "Do the next thing", input_from_previous=True)
    assert "[Previous step output]" in prompt
    assert "...[truncated]" in prompt
    assert len(prompt) < 5200  # truncated output + task text


def test_output_not_injected_when_disabled() -> None:
    from yeoman_gateway.cron.workflow_chain import build_chained_prompt

    prompt = build_chained_prompt("some output", "Do the next thing", input_from_previous=False)
    assert prompt == "Do the next thing"
    assert "[Previous step output]" not in prompt


def test_cycle_detection() -> None:
    from yeoman_gateway.cron.workflow_chain import detect_chain_cycle

    jobs = {
        "a": CronJob(id="a", name="A", payload=CronPayload(next_job_id="b")),
        "b": CronJob(id="b", name="B", payload=CronPayload(next_job_id="c")),
        "c": CronJob(id="c", name="C", payload=CronPayload(next_job_id="a")),  # cycle!
    }
    assert detect_chain_cycle("a", jobs, max_depth=5) is True


def test_no_cycle() -> None:
    from yeoman_gateway.cron.workflow_chain import detect_chain_cycle

    jobs = {
        "a": CronJob(id="a", name="A", payload=CronPayload(next_job_id="b")),
        "b": CronJob(id="b", name="B", payload=CronPayload(next_job_id=None)),
    }
    assert detect_chain_cycle("a", jobs, max_depth=5) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_workflow_chaining.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.cron.workflow_chain'`

- [ ] **Step 3: Implement workflow_chain module**

Create `packages/gateway/yeoman_gateway/cron/workflow_chain.py`:

```python
"""Workflow chaining helpers for cron job execution."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yeoman_gateway.cron.types import CronJob

_MAX_PREVIOUS_OUTPUT_CHARS = 4000


def build_chained_prompt(previous_output: str, next_message: str, *, input_from_previous: bool) -> str:
    """Build the prompt for a chained job, optionally including previous output."""
    if not input_from_previous:
        return next_message

    truncated = previous_output[:_MAX_PREVIOUS_OUTPUT_CHARS]
    if len(previous_output) > _MAX_PREVIOUS_OUTPUT_CHARS:
        truncated += "\n...[truncated]"
    return f"[Previous step output]\n{truncated}\n\n[Your task]\n{next_message}"


def detect_chain_cycle(start_job_id: str, jobs: dict[str, "CronJob"], max_depth: int = 5) -> bool:
    """Walk the chain from start_job_id. Return True if a cycle is detected."""
    visited: set[str] = set()
    current_id: str | None = start_job_id
    steps = 0
    while current_id and steps <= max_depth:
        if current_id in visited:
            return True
        visited.add(current_id)
        job = jobs.get(current_id)
        if not job:
            break
        current_id = job.payload.next_job_id
        steps += 1
    return False


def is_chain_failure(response: str | None) -> bool:
    """Check if a process_direct() response indicates a failure."""
    if response is None:
        return True
    return response.startswith("Error calling LLM:")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_workflow_chaining.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/cron/workflow_chain.py tests/gateway/test_workflow_chaining.py
git commit -m "feat(cron): add workflow chain helpers (prompt build, cycle detection, failure check)"
```

---

### Task 4: Wire Chaining into bootstrap on_cron_job()

**Files:**
- Modify: `packages/gateway/yeoman_gateway/app/bootstrap.py`

- [ ] **Step 1: Update on_cron_job to handle chaining**

In `packages/gateway/yeoman_gateway/app/bootstrap.py`, replace the `on_cron_job` function (lines 463-517) with the expanded version. Add the workflow state initialization before the function and the chaining logic after the existing `response = await responder.process_direct(...)` call.

Before the `on_cron_job` definition, add:

```python
    from yeoman_gateway.cron.workflow_chain import build_chained_prompt, is_chain_failure
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState

    workflow_state = WorkflowState(
        store_path=Path(workspace) / "data" / "cron" / "pending_approvals.json"
    )
```

After the existing `return response` at line 517, replace the `agent_turn` branch (lines 502-517) with:

```python
        response = await responder.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            model_profile=job.payload.model_profile,
        )
        if job.payload.deliver and job.payload.to:
            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response or "",
                )
            )

        # Workflow chaining
        if job.payload.next_job_id and response is not None:
            if is_chain_failure(response):
                # Notify owner of failure
                fail_channel = job.payload.approval_channel or job.payload.channel or "cli"
                fail_chat = job.payload.to or "direct"
                wf_name = job.payload.workflow_id or job.id
                await bus.publish_outbound(OutboundMessage(
                    channel=fail_channel, chat_id=fail_chat,
                    content=f"Workflow '{wf_name}' failed at step {job.payload.workflow_step}: {response[:200]}. Use /cron workflow_list to review.",
                ))
            else:
                await _handle_chain(job, response)

        return response
```

Add the `_handle_chain` helper after `on_cron_job`:

```python
    async def _handle_chain(job: CronJob, output: str) -> None:
        next_job = cron.get_job(job.payload.next_job_id) if job.payload.next_job_id else None
        if not next_job:
            logger.warning("Chained job {} not found", job.payload.next_job_id)
            return

        remaining = job.payload.max_chain_depth - 1
        if remaining <= 0:
            wf_name = job.payload.workflow_id or job.id
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.approval_channel or job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
                content=f"Workflow '{wf_name}' stopped: max chain depth reached.",
            ))
            return

        if job.payload.requires_approval:
            from uuid import uuid4
            approval_id = f"wf-approve-{job.id}-{uuid4().hex[:8]}"
            approval_channel = job.payload.approval_channel or job.payload.channel or "cli"
            approval_chat = job.payload.to or "direct"

            await workflow_state.add(PendingApproval(
                approval_id=approval_id,
                next_job_id=next_job.id,
                previous_output=output,
                channel=approval_channel,
                chat_id=approval_chat,
                created_at=time.time(),
                expires_at=time.time() + 86400,
                workflow_id=job.payload.workflow_id,
                remaining_depth=remaining,
            ))

            await bus.publish_outbound(OutboundMessage(
                channel=approval_channel, chat_id=approval_chat,
                content=(
                    f"{output}\n\n---\n"
                    f"Workflow step {job.payload.workflow_step} complete.\n"
                    f"Next: {next_job.name}\n"
                    f"Reply with this code to approve: {approval_id}"
                ),
            ))
        else:
            # Direct chain — execute next step immediately
            prompt = build_chained_prompt(output, next_job.payload.message, input_from_previous=next_job.payload.input_from_previous)
            next_job.payload.max_chain_depth = remaining
            chain_response = await responder.process_direct(
                prompt,
                session_key=f"cron:{next_job.id}",
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to or "direct",
                model_profile=next_job.payload.model_profile,
            )
            if next_job.payload.deliver and next_job.payload.to:
                await bus.publish_outbound(OutboundMessage(
                    channel=next_job.payload.channel or "cli",
                    chat_id=next_job.payload.to,
                    content=chain_response or "",
                ))
            # Recurse for further chain steps
            if next_job.payload.next_job_id and chain_response is not None and not is_chain_failure(chain_response):
                await _handle_chain(next_job, chain_response)
```

Also add a `get_job` method to `CronService` if not already present (check service.py):

```python
    def get_job(self, job_id: str) -> CronJob | None:
        """Get a job by ID."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                return job
        return None
```

- [ ] **Step 2: Run existing tests to verify nothing broke**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -x -q`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add packages/gateway/yeoman_gateway/app/bootstrap.py packages/gateway/yeoman_gateway/cron/service.py
git commit -m "feat(cron): wire workflow chaining and approval into on_cron_job"
```

---

### Task 5: ApprovalMiddleware

**Files:**
- Create: `packages/gateway/yeoman_gateway/pipeline/approval.py`
- Modify: `packages/gateway/yeoman_gateway/core/orchestrator.py`
- Test: `tests/gateway/test_approval_middleware.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_approval_middleware.py
"""Tests for ApprovalMiddleware."""

import asyncio
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
        is_owner=is_owner,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_approval_middleware.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.pipeline.approval'`

- [ ] **Step 3: Implement ApprovalMiddleware**

Create `packages/gateway/yeoman_gateway/pipeline/approval.py`:

```python
"""Middleware to intercept workflow approval codes from owner messages."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from yeoman_gateway.core.pipeline import NextFn, PipelineContext
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState


class ApprovalMiddleware:
    """Intercepts owner messages that match a pending workflow approval code.

    If the message content exactly matches an approval_id, the approval
    is consumed and the trigger callback fires. The message is halted
    (not passed to subsequent middleware).
    """

    def __init__(
        self,
        workflow_state: "WorkflowState",
        trigger_callback: Callable[["PendingApproval"], Awaitable[None]],
    ) -> None:
        self._state = workflow_state
        self._trigger = trigger_callback

    async def __call__(self, ctx: "PipelineContext", next: "NextFn") -> None:
        if not getattr(ctx.decision, "is_owner", False):
            await next(ctx)
            return

        content = ctx.event.content.strip()
        if not content.startswith("wf-approve-"):
            await next(ctx)
            return

        approval = await self._state.match_and_consume(content)
        if approval is None:
            await next(ctx)
            return

        logger.info("Workflow approval matched: {} -> job {}", approval.approval_id, approval.next_job_id)
        await self._trigger(approval)
        ctx.halt()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_approval_middleware.py -v`
Expected: 3 passed

- [ ] **Step 5: Insert into pipeline**

In `packages/gateway/yeoman_gateway/core/orchestrator.py`, add import:
```python
from yeoman_gateway.pipeline.approval import ApprovalMiddleware
```

Add `workflow_state` and `approval_trigger` parameters to `Orchestrator.__init__`:
```python
        workflow_state: "WorkflowState | None" = None,
        approval_trigger: "Callable | None" = None,
```

After `PolicyMiddleware(policy=policy),` (line 89), insert:
```python
        if workflow_state and approval_trigger:
            layers.append(ApprovalMiddleware(workflow_state=workflow_state, trigger_callback=approval_trigger))
```

Wire in `bootstrap.py` where the `Orchestrator` is constructed (~line 444):
```python
        workflow_state=workflow_state,
        approval_trigger=_handle_approved_job,
```

Add the trigger callback in bootstrap, after `_handle_chain`:
```python
    async def _handle_approved_job(approval: PendingApproval) -> None:
        next_job = cron.get_job(approval.next_job_id)
        if not next_job:
            logger.warning("Approved job {} not found", approval.next_job_id)
            return
        prompt = build_chained_prompt(
            approval.previous_output, next_job.payload.message,
            input_from_previous=next_job.payload.input_from_previous,
        )
        next_job.payload.max_chain_depth = approval.remaining_depth
        response = await responder.process_direct(
            prompt, session_key=f"cron:{next_job.id}",
            channel=next_job.payload.channel or "cli",
            chat_id=next_job.payload.to or "direct",
            model_profile=next_job.payload.model_profile,
        )
        if next_job.payload.deliver and next_job.payload.to:
            await bus.publish_outbound(OutboundMessage(
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to, content=response or "",
            ))
        if next_job.payload.next_job_id and response and not is_chain_failure(response):
            await _handle_chain(next_job, response)
```

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -x -q`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add packages/gateway/yeoman_gateway/pipeline/approval.py packages/gateway/yeoman_gateway/core/orchestrator.py packages/gateway/yeoman_gateway/app/bootstrap.py tests/gateway/test_approval_middleware.py
git commit -m "feat: add ApprovalMiddleware and wire into pipeline"
```

---

### Task 6: CronTool add_workflow and workflow_list Actions

**Files:**
- Modify: `packages/gateway/yeoman_gateway/agent/tools/cron.py`
- Test: `tests/gateway/test_cron_tool_workflows.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_cron_tool_workflows.py
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
        jobs = cron.list_jobs()
        assert len(jobs) == 3

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_cron_tool_workflows.py -v`
Expected: FAIL — `TypeError: CronTool.execute() got an unexpected keyword argument 'workflow_name'`

- [ ] **Step 3: Implement add_workflow and workflow_list**

In `packages/gateway/yeoman_gateway/agent/tools/cron.py`:

1. Update `description` property to mention new actions:
```python
    @property
    def description(self) -> str:
        return "Schedule reminders, recurring tasks, and multi-step workflows. Actions: add, add_workflow, list, workflow_list, remove."
```

2. Update `parameters` to add new fields. Add to `properties` dict:
```python
                "workflow_name": {
                    "type": "string",
                    "description": "Name for a multi-step workflow (for add_workflow)"
                },
                "trigger": {
                    "type": "string",
                    "description": "Cron expression for the first step (for add_workflow)"
                },
                "steps": {
                    "type": "array",
                    "description": "Workflow steps (for add_workflow). Each has: message (required), requires_approval (bool), deliver (bool), to (string).",
                    "items": {"type": "object"},
                    "minItems": 2,
                    "maxItems": 5
                },
                "chain_to": {
                    "type": "string",
                    "description": "Job ID to trigger after this job completes (for add)"
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "Pause for owner approval before chained job (for add)"
                },
```

Update `enum` in `action` to:
```python
                    "enum": ["add", "add_workflow", "list", "workflow_list", "remove"],
```

3. Update `execute` method signature and routing:
```python
    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        workflow_name: str = "",
        trigger: str = "",
        steps: list[dict] | None = None,
        chain_to: str | None = None,
        requires_approval: bool = False,
        **kwargs: Any
    ) -> str:
        if action == "add":
            return self._add_job(message, every_seconds, cron_expr, at, chain_to=chain_to, requires_approval=requires_approval)
        elif action == "add_workflow":
            return self._add_workflow(workflow_name, trigger, steps or [])
        elif action == "list":
            return self._list_jobs()
        elif action == "workflow_list":
            return self._workflow_list()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"
```

4. Update `_add_job` to accept chaining params:
```python
    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        at: str | None,
        chain_to: str | None = None,
        requires_approval: bool = False,
    ) -> str:
        # ... existing validation ...

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=True,
            channel=self._channel,
            to=self._chat_id,
            delete_after_run=delete_after_run,
        )
        if chain_to:
            job.payload.next_job_id = chain_to
            job.payload.requires_approval = requires_approval
            self._cron._save_store()
        return f"Created job '{job.name}' (id: {job.id})"
```

5. Add `_add_workflow`:
```python
    def _add_workflow(self, workflow_name: str, trigger: str, steps: list[dict]) -> str:
        if not workflow_name:
            return "Error: workflow_name is required"
        if not trigger:
            return "Error: trigger (cron expression) is required"
        if len(steps) < 2:
            return "Error: workflow needs at least 2 steps"
        if len(steps) > 5:
            return "Error: workflow limited to 5 steps"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        # Create all jobs first
        created_jobs: list[CronJob] = []
        for i, step in enumerate(steps):
            msg = step.get("message", "")
            if not msg:
                return f"Error: step {i + 1} missing 'message'"

            if i == 0:
                schedule = CronSchedule(kind="cron", expr=trigger)
            else:
                # Non-trigger steps: one-shot placeholder (triggered by chain, not timer)
                schedule = CronSchedule(kind="at", at_ms=0)

            deliver = bool(step.get("deliver", False))
            to = step.get("to", self._chat_id)

            job = self._cron.add_job(
                name=f"{workflow_name}:{i + 1}",
                schedule=schedule,
                message=msg,
                deliver=deliver,
                channel=self._channel,
                to=to,
                delete_after_run=False,
            )
            job.payload.workflow_id = workflow_name
            job.payload.workflow_step = i
            job.payload.requires_approval = bool(step.get("requires_approval", False))
            job.payload.input_from_previous = i > 0  # all steps after first get previous output
            created_jobs.append(job)

            # Disable non-trigger steps (they run via chain, not timer)
            if i > 0:
                job.enabled = False

        # Wire chain links
        for i in range(len(created_jobs) - 1):
            created_jobs[i].payload.next_job_id = created_jobs[i + 1].id

        self._cron._save_store()

        job_ids = ", ".join(j.id for j in created_jobs)
        return f"Created workflow '{workflow_name}' with {len(created_jobs)} steps (ids: {job_ids})"
```

6. Add `_workflow_list`:
```python
    def _workflow_list(self) -> str:
        jobs = self._cron.list_jobs(include_disabled=True)
        workflows: dict[str, list[CronJob]] = {}
        for job in jobs:
            wf_id = job.payload.workflow_id
            if wf_id:
                if wf_id not in workflows:
                    workflows[wf_id] = []
                workflows[wf_id].append(job)

        if not workflows:
            return "No active workflows."

        lines = []
        for wf_id, wf_jobs in workflows.items():
            wf_jobs.sort(key=lambda j: j.payload.workflow_step)
            lines.append(f"Workflow: {wf_id}")
            for job in wf_jobs:
                status = job.state.last_status or "waiting"
                lines.append(f"  Step {job.payload.workflow_step + 1}: {job.name} — {status}")
            lines.append("")
        return "\n".join(lines).strip()
```

Add the missing import at the top:
```python
from yeoman_gateway.cron.types import CronJob, CronSchedule
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_cron_tool_workflows.py -v`
Expected: 2 passed

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add packages/gateway/yeoman_gateway/agent/tools/cron.py tests/gateway/test_cron_tool_workflows.py
git commit -m "feat(cron): add add_workflow and workflow_list tool actions"
```

---

### Task 7: Approval Expiry in CronService Timer

**Files:**
- Modify: `packages/gateway/yeoman_gateway/cron/service.py`
- Modify: `packages/gateway/yeoman_gateway/app/bootstrap.py`

- [ ] **Step 1: Add expiry check to CronService**

Add an optional `workflow_state` attribute to `CronService.__init__`:

```python
    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        sessions_dir: Path | None = None,
        workflow_state: "WorkflowState | None" = None,
        on_approval_expired: Callable[["PendingApproval"], Coroutine[Any, Any, None]] | None = None,
    ):
        # ... existing code ...
        self._workflow_state = workflow_state
        self._on_approval_expired = on_approval_expired
```

Add expiry check to `_on_timer()` (after line 281, before `self._save_store()`):

```python
        # Check for expired workflow approvals
        if self._workflow_state:
            try:
                expired = await self._workflow_state.purge_expired()
                for approval in expired:
                    logger.info("Workflow approval expired: {}", approval.approval_id)
                    if self._on_approval_expired:
                        await self._on_approval_expired(approval)
            except Exception as e:
                logger.warning("Error checking approval expiry: {}", e)
```

- [ ] **Step 2: Wire expiry notification in bootstrap**

In `bootstrap.py`, when creating `CronService`, pass the workflow state:

```python
    async def _on_approval_expired(approval: PendingApproval) -> None:
        await bus.publish_outbound(OutboundMessage(
            channel=approval.channel,
            chat_id=approval.chat_id,
            content=f"Workflow approval expired: {approval.approval_id}. Use /cron workflow_list to review.",
        ))

    cron = CronService(
        store_path=cron_store_path,
        sessions_dir=...,
        workflow_state=workflow_state,
        on_approval_expired=_on_approval_expired,
    )
```

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -x -q`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add packages/gateway/yeoman_gateway/cron/service.py packages/gateway/yeoman_gateway/app/bootstrap.py
git commit -m "feat(cron): add workflow approval expiry check to timer loop"
```
