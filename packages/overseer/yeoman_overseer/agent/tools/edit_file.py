"""Audited file edit -- exact string replacement with path restrictions."""
from __future__ import annotations

from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry
from yeoman_overseer.agent.tools.write_file import _is_allowed


def edit_file(path: str, old_string: str, new_string: str, ctx: object) -> dict:
    """Replace old_string with new_string in path. Returns {ok, path} or {ok: False, error}."""
    target = Path(path).expanduser()

    if not _is_allowed(target, ctx):
        return {"ok": False, "error": f"path denied: {path}"}

    if not target.exists():
        return {"ok": False, "error": f"file not found: {path}"}

    content = target.read_text(encoding="utf-8")
    if old_string not in content:
        return {"ok": False, "error": f"old_string not found in {path}"}

    new_content = content.replace(old_string, new_string, 1)
    target.write_text(new_content, encoding="utf-8")

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="edit_file",
        target=str(target),
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    if ctx.git is not None:
        try:
            rel = str(target.resolve().relative_to(ctx.data_dir.resolve()))
            ctx.git.commit(files=[rel], message=f"overseer: edit {target.name}")
        except ValueError:
            pass

    return {"ok": True, "path": str(target)}
