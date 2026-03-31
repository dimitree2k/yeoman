# Autonomous Workflows Design

**Date**: 2026-03-31
**Status**: Draft
**Scope**: Workflow chaining, approval gates, output passing in CronService
**Depends on**: Spec 1 (Event Backbone) — for event-triggered workflows and
overseer-initiated chains
**Extends**: Existing `CronService`, `CronPayload`, `CronTool`

## Problem

Yeoman can do exactly one thing autonomously: fire a cron job that runs a single
agent turn. Real autonomy needs multi-step operations that:

- **Chain**: job A completes, triggers job B with A's output as context
- **Pause**: wait for owner approval before proceeding
- **Survive restarts**: workflow state persists to disk
- **Have guardrails**: max steps, max cost, timeout, scope limits

Today, "every Monday pull calendar, summarize, wait for my OK, then send to
family group" requires a single enormous prompt and hope. That is fragile and
unauditable.

## Solution: Extend CronService with workflow chains

Not a new engine, not a DSL, not a DAG library. We extend what exists —
`CronJob` and `CronPayload` — with three additions: **chaining**, **approval
gates**, and **output passing**.

```
CronJob A (trigger: cron "0 9 * * 1")
  | completes with output
  v
CronJob B (trigger: chained from A, receives A's output as context)
  | completes, but has requires_approval=true
  v
Owner gets message: "Here's the summary. Reply OK to send."
  | owner replies with approval code
  v
CronJob C (trigger: approved, receives B's output as context)
  | delivers to family group
  v
Done. Workflow complete.
```

Each step is a regular `CronJob` — same execution path, same
`responder.process_direct()`, same security pipeline.

---

## Data model changes

### CronPayload extensions

File: `yeoman_gateway/cron/types.py`

New fields on the existing `CronPayload` dataclass:

```python
# Workflow chaining
next_job_id: str | None = None          # job to trigger on completion
requires_approval: bool = False          # pause and ask owner before next
approval_channel: str | None = None      # where to ask (defaults to deliver_to)
input_from_previous: bool = False        # inject previous job's output into prompt

# Workflow metadata
workflow_id: str | None = None           # groups related jobs
workflow_step: int = 0                   # auto-set: 0 for first job, incremented on chain
max_chain_depth: int = 5                 # safety: max steps from this point
```

No new tables. No new services. No new processes.

---

## Execution flow

### In `bootstrap.py` `on_cron_job()` (~line 502)

After a job completes and produces output, three outcomes:

**1. No chain (same as today)**:
`next_job_id` is `None` — done. Existing behavior unchanged.

**2. Chain without approval**:
`next_job_id` set, `requires_approval=false` — immediately trigger next job.

```python
next_job = cron.get_job(current_job.payload.next_job_id)
if not next_job:
    logger.warning("chained job {} not found", current_job.payload.next_job_id)
    return

# Depth guard
remaining = current_job.payload.max_chain_depth - 1
if remaining <= 0:
    await _notify_owner("Workflow chain depth exhausted.")
    return

# Build prompt with optional output passing
if next_job.payload.input_from_previous:
    prompt = (
        f"[Previous step output]\n{job_output}\n\n"
        f"[Your task]\n{next_job.payload.message}"
    )
else:
    prompt = next_job.payload.message

# Execute next step
next_job.payload.max_chain_depth = remaining
response = await responder.process_direct(prompt, session_key=..., ...)
```

**3. Chain with approval**:
`next_job_id` set, `requires_approval=true` — send approval request, store
pending state, wait asynchronously.

---

## Approval mechanics

### Step 1: Send approval request

```python
approval_id = f"wf-approve-{job.id}-{uuid4().hex[:8]}"
approval_msg = (
    f"{job_output}\n\n"
    f"---\n"
    f"Workflow step {job.payload.workflow_step} complete.\n"
    f"Next: {next_job.name}\n"
    f"Reply with this code to approve: {approval_id}"
)
await bus.publish_outbound(OutboundMessage(
    channel=approval_channel, chat_id=approval_chat_id,
    content=approval_msg,
))
```

### Step 2: Store pending approval

New file: `yeoman_gateway/cron/workflow_state.py`

```python
@dataclass
class PendingApproval:
    approval_id: str           # code the owner must reply with
    next_job_id: str           # what to trigger on approval
    previous_output: str       # output to pass forward
    channel: str               # where we asked
    chat_id: str               # who we asked
    created_at: float          # epoch timestamp
    expires_at: float          # auto-expire (default: 24h from creation)
    workflow_id: str | None    # for grouping
    remaining_depth: int       # chain depth remaining
```

Persisted as JSON at `~/.yeoman/data/cron/pending_approvals.json`. Loaded on
startup. Checked on every inbound owner message.

### Step 3: Match approval in pipeline

New middleware: `yeoman_gateway/pipeline/approval.py`

Inserted into the pipeline after `PolicyMiddleware` (which sets
`ctx.decision.is_owner` needed for the owner check):

```python
class ApprovalMiddleware:
    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if not ctx.decision.is_owner:
            await next(ctx)
            return

        content = ctx.event.content.strip()
        approval = self._workflow_state.match_and_consume(content)
        if approval is None:
            await next(ctx)
            return

        # Trigger the approved next job
        await self._trigger_approved_job(approval)
        ctx.halt()  # consume the message
```

### Why a code, not just "OK"

The owner might say "OK" in an unrelated conversation. The approval code
(`wf-approve-xxx-a1b2c3d4`) is unambiguous, single-use, and time-bound. It also
provides an audit trail — every approval is traceable to a specific workflow step.

### Expiry

Pending approvals expire after 24 hours (configurable). On expiry, the owner
receives: "Workflow step expired without approval. Use /cron workflow_list to
review."

Expiry check runs in the existing `CronService._on_timer()` loop — no new
timer needed.

---

## Safety guardrails

### Chain depth

Before triggering any next job:

```python
remaining = current_job.payload.max_chain_depth - 1
if remaining <= 0:
    logger.warning("workflow chain depth exhausted for {}", current_job.id)
    await _notify_owner(f"Workflow '{workflow_id}' stopped: max depth reached.")
    return
```

Default max depth: 5. Configurable per-job.

### Cycle detection

At job creation time (in `CronService.add_job()` or `CronTool._add_job()`),
walk the chain from the new job's `next_job_id` forward. If the walk encounters
the new job's own ID, reject with an error. Walk is bounded by `max_chain_depth`
— O(n) where n <= 5.

### Tool scope

Chained jobs use the same `process_direct()` path as regular cron jobs. Each
agent turn passes through `SecurityPort`. No privilege escalation possible —
the chained job cannot gain tools or permissions the originating job didn't have.

### Cost control

Each step is one `process_direct()` call — same token budget as any cron job.
The `max_chain_depth` cap prevents unbounded cost accumulation. For additional
control, the overseer's existing budget tracking (hourly action limit, daily
LLM tokens) applies to all agent turns including chained ones.

---

## Agent-facing tool interface

### CronTool schema additions

File: `yeoman_gateway/agent/tools/cron.py`

New parameters for the `add` action:

```python
"chain_to": {
    "type": "string",
    "description": "Job ID to trigger after this job completes"
},
"requires_approval": {
    "type": "boolean",
    "description": "Pause and ask owner for approval before triggering chained job",
    "default": false
},
"workflow_name": {
    "type": "string",
    "description": "Group name for related jobs in a workflow"
}
```

### New action: `workflow_list`

Returns active workflows grouped by `workflow_id`:

```
Workflow: weekly-family-summary
  Step 1: Pull calendar (cron: 0 9 * * 1) — last run: OK
  Step 2: Summarize and approve — status: pending_approval (expires in 18h)
  Step 3: Send to family group — status: waiting
```

### Workflow creation by agent

The agent creates multi-step workflows by calling `cron add` multiple times and
chaining via `chain_to`. No special "create workflow" command needed — the
agent composes jobs naturally:

```
1. cron add "Pull my calendar for this week" cron="0 9 * * 1" workflow_name="weekly-summary"
   → returns job_id: "abc123"

2. cron add "Summarize the calendar" chain_to=None requires_approval=true workflow_name="weekly-summary"
   → returns job_id: "def456"
   → then: cron update abc123 chain_to=def456

3. cron add "Send summary to family group" deliver=true to="family-jid" workflow_name="weekly-summary"
   → returns job_id: "ghi789"
   → then: cron update def456 chain_to=ghi789
```

---

## Files changed/added

| File | Change |
|------|--------|
| `yeoman_gateway/cron/types.py` | Add chaining fields to `CronPayload` |
| `yeoman_gateway/cron/workflow_state.py` | **New**: `PendingApproval` + JSON persistence (~60 lines) |
| `yeoman_gateway/cron/service.py` | Add expiry check in `_on_timer()` |
| `yeoman_gateway/app/bootstrap.py` (~line 502) | Extend `on_cron_job` with chain/approval logic |
| `yeoman_gateway/pipeline/approval.py` | **New**: `ApprovalMiddleware` (~40 lines) |
| `yeoman_gateway/core/orchestrator.py` | Insert `ApprovalMiddleware` into pipeline after `PolicyMiddleware` |
| `yeoman_gateway/agent/tools/cron.py` | Add `chain_to`, `requires_approval`, `workflow_name` params + `workflow_list` action |

### Dependencies

Zero. JSON for state persistence, `uuid4` for approval codes — both stdlib.

---

## Out of scope

- Conditional branching (if output contains X, go to B; else C) — add when a
  real use case demands it
- Parallel workflow steps (run B and C concurrently after A) — same
- Workflow templates/DSL — the agent composes jobs via tool calls; no YAML
- Event-triggered workflow steps (webhook fires -> resume workflow) — possible
  future integration with Spec 1, but not in this iteration
- Workflow versioning or migration
- Visual workflow builder

## Testing strategy

- Unit: chain depth enforcement (exhaustion, decrement, cycle detection)
- Unit: approval creation, matching, consumption, expiry
- Unit: output passing (with and without `input_from_previous`)
- Unit: `workflow_list` formatting
- Integration: full 3-step workflow (trigger -> chain -> approve -> deliver)
- Integration: approval expiry -> owner notification
- Integration: restart recovery (pending approvals survive process restart)
- Edge: approval from non-owner rejected
- Edge: approval code replay rejected (already consumed)
- Edge: chained job deleted before trigger (graceful error + owner notification)
