"""Audited file write -- path allowlist + deny-list, auto-committed."""
from __future__ import annotations

from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry

_DENY_PARTS: frozenset[str] = frozenset({".env", "secrets", ".git", "systemd", "runbooks"})


def _is_allowed(path: Path, ctx: object) -> bool:
    """Return True iff path resolves within an allowed root and passes the deny-list."""
    try:
        resolved = path.resolve()
    except OSError:
        return False

    roots = [ctx.yeoman_home.resolve(), ctx.source_dir.resolve()]
    in_root = any(resolved == r or resolved.is_relative_to(r) for r in roots)
    if not in_root:
        return False

    for part in resolved.parts:
        if part in _DENY_PARTS:
            return False
    return True


def write_file(path: str, content: str, ctx: object) -> dict:
    """Write content to path. Returns {ok, path} or {ok: False, error}."""
    target = Path(path)

    if not _is_allowed(target, ctx):
        return {"ok": False, "error": f"path denied: {path}"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="write_file",
        target=str(target),
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    if ctx.git is not None:
        try:
            rel = str(target.resolve().relative_to(ctx.data_dir.resolve()))
            ctx.git.commit(files=[rel], message=f"overseer: write {target.name}")
        except ValueError:
            pass

    return {"ok": True, "path": str(target)}
