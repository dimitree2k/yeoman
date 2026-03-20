"""git_log tool — read git history from source or internal repo."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    repo = args.get("repo", "source")
    limit = int(args.get("limit", 20))
    cwd = ctx.source_dir if repo == "source" else ctx.data_dir
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--oneline"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return f"[git_log] (no commits or git error: {result.stderr.strip()})"
        return result.stdout.strip() or "[git_log] (no commits)"
    except Exception as exc:
        return f"[git_log] ERROR: {exc}"
