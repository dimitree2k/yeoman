"""Context assembly for LLM agent invocations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.audit.logger import AuditLogger
    from yeoman_overseer.runbook.parser import Runbook

_SYSTEM_PROMPT = """\
You are the yeoman overseer. You maintain system health, governance, and evolution.
You have no user contact. You report to the owner via digest, not conversation.
Take targeted, minimal actions. Prefer to observe and alert over modifying state.
If unsure, send an alert rather than acting."""


@dataclass
class AgentContext:
    system_prompt: str
    user_message: str


def build_context(
    runbook: Runbook,
    observations: dict[str, Any],
    audit: AuditLogger,
) -> AgentContext:
    """Build system prompt + user message for an LLM agent invocation."""
    audit_entries = audit.read_recent(limit=20, domain=runbook.meta.domain)
    tombstones = audit.query_tombstones(domain=runbook.meta.domain)

    parts = [
        f"## Active Runbook: {runbook.meta.name}",
        "",
        runbook.body.strip(),
        "",
        "## Observations",
        json.dumps(observations, indent=2),
    ]

    if audit_entries:
        parts += [
            "",
            f"## Recent Audit Log (domain={runbook.meta.domain}, last {len(audit_entries)})",
            *[json.dumps(e) for e in audit_entries],
        ]

    if tombstones:
        parts += [
            "",
            "## Recently Retired Features (tombstones)",
            *[f"- {t.get('name', '?')}: {t.get('reason', '?')}" for t in tombstones],
        ]

    return AgentContext(
        system_prompt=_SYSTEM_PROMPT,
        user_message="\n".join(parts),
    )
