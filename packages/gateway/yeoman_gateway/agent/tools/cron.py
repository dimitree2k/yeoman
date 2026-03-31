"""Cron tool for scheduling reminders and tasks."""

import time
from datetime import datetime
from typing import Any

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.cron.service import CronService
from yeoman_gateway.cron.types import CronJob, CronSchedule


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Schedule reminders, recurring tasks, and multi-step workflows. Actions: add, add_workflow, list, workflow_list, remove."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "add_workflow", "list", "workflow_list", "remove"],
                    "description": "Action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Reminder message (for add)"
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)"
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)"
                },
                "at": {
                    "type": "string",
                    "description": "One-shot execution time in ISO 8601 format, e.g. '2026-02-16T13:30:00+01:00'"
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove)"
                },
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
                }
            },
            "required": ["action"]
        }

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

    @staticmethod
    def _parse_at_iso_to_ms(at: str) -> int | None:
        value = at.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(dt.timestamp() * 1000)

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        at: str | None,
        chain_to: str | None = None,
        requires_approval: bool = False,
    ) -> str:
        if not message:
            return "Error: message is required for add"
        if not self._channel or not self._chat_id:
            return "Error: no session context (channel/chat_id)"

        chosen = [every_seconds is not None, bool(cron_expr), bool(at)]
        if sum(chosen) != 1:
            return "Error: specify exactly one schedule: every_seconds, cron_expr, or at"

        # Build schedule
        if every_seconds is not None:
            if every_seconds <= 0:
                return "Error: every_seconds must be > 0"
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
            delete_after_run = False
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr)
            delete_after_run = False
        elif at:
            at_ms = self._parse_at_iso_to_ms(at)
            if at_ms is None:
                return "Error: invalid at format. Use ISO 8601, e.g. 2026-02-16T13:30:00+01:00"
            if at_ms <= int(time.time() * 1000):
                return "Error: at must be in the future"
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after_run = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

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

        created_jobs: list[CronJob] = []
        for i, step in enumerate(steps):
            msg = step.get("message", "")
            if not msg:
                return f"Error: step {i + 1} missing 'message'"

            if i == 0:
                schedule = CronSchedule(kind="cron", expr=trigger)
            else:
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
            job.payload.input_from_previous = i > 0
            created_jobs.append(job)

            if i > 0:
                job.enabled = False

        for i in range(len(created_jobs) - 1):
            created_jobs[i].payload.next_job_id = created_jobs[i + 1].id

        self._cron._save_store()

        job_ids = ", ".join(j.id for j in created_jobs)
        return f"Created workflow '{workflow_name}' with {len(created_jobs)} steps (ids: {job_ids})"

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

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = [f"- {j.name} (id: {j.id}, {j.schedule.kind})" for j in jobs]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"
