"""Pydantic models for runbook YAML frontmatter."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TriggerCondition(BaseModel):
    """A deterministic check condition."""

    check: str
    target: str
    operator: Literal["==", "!=", ">", ">=", "<", "<="] = "=="
    value: Any = True
    window: str | None = None


class TriggerConfig(BaseModel):
    """Trigger configuration — poll, cron, or event."""

    kind: Literal["poll", "cron", "event"]
    interval_s: int | None = None
    condition: TriggerCondition | None = None
    expr: str | None = None
    event_name: str | None = None

    @model_validator(mode="after")
    def _validate_trigger(self) -> TriggerConfig:
        if self.kind == "poll":
            if self.interval_s is None:
                raise ValueError("poll trigger requires interval_s")
            if self.condition is None:
                raise ValueError("poll trigger requires condition")
        elif self.kind == "cron":
            if self.expr is None:
                raise ValueError("cron trigger requires expr")
        elif self.kind == "event":
            if self.event_name is None:
                raise ValueError("event trigger requires event_name")
        return self


class SafetyConfig(BaseModel):
    """Safety constraints for a runbook."""

    max_actions_per_hour: int = 10
    rollback: bool = True
    cooldown_s: int = 300
    manual_reset_after_failures: bool = False
    requires_tests: bool = False
    on_lock_conflict: Literal["queue", "skip"] = "skip"
    shell_timeout_s: int = 60


class LLMBudget(BaseModel):
    """Budget constraints for LLM-escalated runbooks."""

    max_tokens: int = 30_000
    max_tool_calls: int = 100
    llm_profile: str = "overseerDefault"


class RunbookFrontmatter(BaseModel):
    """Parsed YAML frontmatter from a runbook Markdown file."""

    name: str
    domain: str
    enabled: bool = True
    version: int = 1
    origin: Literal["manual", "auto"] = "manual"

    trigger: TriggerConfig

    escalate_to_llm: bool = False
    llm_budget: LLMBudget | None = None

    safety: SafetyConfig = Field(default_factory=SafetyConfig)
