"""Execute a shell command inside the bubblewrap sandbox."""
from __future__ import annotations

from yeoman_overseer.audit.logger import AuditEntry


def shell(command: str, *, ctx: object) -> dict:
    """Run command string in sandbox. Returns {stdout, stderr, exit_code}."""
    cmd = ["/bin/sh", "-c", command]

    try:
        result = ctx.sandbox.run(cmd, timeout=ctx.shell_timeout_s)
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="shell",
        target=command[:200],
        result=f"exit={result['exit_code']}",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    return result
