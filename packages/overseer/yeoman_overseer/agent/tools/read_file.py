"""read_file tool — read files under ~/.yeoman/ or ~/Documents/yeoman/."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext

_DENY_PARTS = {".env", "secrets", ".git"}


def _is_allowed(path: Path, ctx: ToolContext) -> bool:
    resolved = path.resolve()
    roots = [ctx.yeoman_home.resolve(), ctx.source_dir.resolve()]
    in_root = any(
        resolved == r or resolved.is_relative_to(r) for r in roots
    )
    if not in_root:
        return False
    for part in resolved.parts:
        if part in _DENY_PARTS:
            return False
    return True


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    path = Path(args["path"]).expanduser()
    if not _is_allowed(path, ctx):
        return f"[read_file] BLOCKED: {path} is outside allowed roots or in deny-list"
    if not path.exists():
        return f"[read_file] ERROR: file not found: {path}"
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"[read_file] ERROR: {exc}"
