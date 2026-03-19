"""Contracts for shared policy admin command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PolicyActorSource = Literal["dm", "cli"]
PolicyOutcome = Literal["applied", "noop", "denied", "invalid", "error"]


@dataclass(frozen=True, slots=True)
class PolicyActorContext:
    """Caller identity for policy admin command execution."""

    source: PolicyActorSource
    channel: str
    chat_id: str
    sender_id: str
    is_group: bool
    is_owner: bool


@dataclass(frozen=True, slots=True)
class PolicyCommand:
    """Normalized policy command envelope."""

    namespace: str
    subcommand: str
    argv: tuple[str, ...]
    raw_text: str


@dataclass(frozen=True, slots=True)
class PolicyExecutionOptions:
    """Execution options that affect side effects."""

    dry_run: bool = False
    confirm: bool = False


@dataclass(frozen=True, slots=True)
class PolicyExecutionResult:
    """Result from policy command execution."""

    outcome: PolicyOutcome
    message: str
    mutated: bool
    before_hash: str | None = None
    after_hash: str | None = None
    audit_id: str | None = None
    backup_ref: str | None = None
    command_name: str = ""
    source: PolicyActorSource = "dm"
    dry_run: bool = False
    unknown_command: bool = False
    audit_write_failed: bool = False
    is_rollback: bool = False
    meta: dict[str, str] = field(default_factory=dict)
