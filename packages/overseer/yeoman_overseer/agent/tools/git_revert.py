"""Revert an internal overseer git commit by SHA."""
from __future__ import annotations

import re

from yeoman_overseer.audit.logger import AuditEntry

_SHA_RE = re.compile(r"^[0-9a-f]{6,40}$")


def git_revert(sha: str, *, ctx: object) -> dict:
    """Revert a single commit in the internal overseer git by SHA."""
    if not _SHA_RE.match(sha):
        return {"ok": False, "error": f"invalid sha: {sha!r}"}

    try:
        ctx.git.revert(sha)
    except Exception as exc:
        ctx.audit.append(AuditEntry(
            runbook=ctx.runbook_name,
            trigger="llm",
            action="git_revert",
            target=sha,
            result=f"error: {exc}",
            duration_ms=0,
            escalated_to_llm=True,
            domain=ctx.domain,
        ))
        return {"ok": False, "error": str(exc)}

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="git_revert",
        target=sha,
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))
    return {"ok": True, "reverted": sha}
