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
    while current_id and steps < max_depth:
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
