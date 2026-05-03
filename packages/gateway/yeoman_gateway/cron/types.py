"""Cron types."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn", "voice_broadcast"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    # Voice broadcast settings (kind=voice_broadcast)
    voice_messages: list[str] = field(default_factory=list)
    voice_random: bool = False
    voice_group: str | None = None
    voice_chat_id: str | None = None
    voice_channel: str | None = None
    voice_name: str | None = None
    voice_tts_route: str | None = None
    voice_verbatim: bool = True
    voice_max_sentences: int | None = None
    voice_max_chars: int | None = None
    voice_wait_for_quiet: bool = False
    voice_quiet_minutes: int | None = None
    voice_retry_minutes: int | None = None
    voice_window_end: str | None = None
    voice_generate: bool = False
    voice_prompt: str | None = None
    voice_recent_messages: list[str] = field(default_factory=list)
    # Model profile override (e.g. "overseerDefault" to use a cheaper model)
    model_profile: str | None = None
    # Workflow chaining
    next_job_id: str | None = None
    requires_approval: bool = False
    approval_channel: str | None = None
    input_from_previous: bool = False
    # Workflow metadata
    workflow_id: str | None = None
    workflow_step: int = 0
    max_chain_depth: int = 5


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
