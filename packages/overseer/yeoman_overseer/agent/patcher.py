"""CI/CD patch model — isolated git worktrees in /tmp/, merge only on test pass."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PatchContext:
    worktree_path: Path
    branch: str
    live_repo: Path


class Patcher:
    """Manages git worktrees for isolated source code mutation."""

    def create_worktree(self, live_repo: Path, run_id: str) -> PatchContext:
        wt_path = Path(f"/tmp/overseer-wt-{run_id}")
        branch = f"overseer-patch-{run_id}"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch],
            cwd=live_repo,
            check=True,
            capture_output=True,
        )
        return PatchContext(worktree_path=wt_path, branch=branch, live_repo=live_repo)

    def translate_path(self, ctx: PatchContext, original: Path) -> Path:
        """Map a live-repo path to its equivalent in the worktree."""
        rel = original.resolve().relative_to(ctx.live_repo.resolve())
        return ctx.worktree_path / rel

    def apply(self, ctx: PatchContext) -> None:
        """Commit worktree changes, merge into live repo, remove worktree."""
        subprocess.run(
            ["git", "add", "-A"],
            cwd=ctx.worktree_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"overseer: patch {ctx.branch}"],
            cwd=ctx.worktree_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "merge", ctx.branch, "--no-ff", "-m", f"overseer: merge {ctx.branch}"],
            cwd=ctx.live_repo,
            check=True,
            capture_output=True,
        )
        self._cleanup(ctx)

    def discard(self, ctx: PatchContext) -> None:
        """Remove worktree and delete branch without merging."""
        self._cleanup(ctx)

    def _cleanup(self, ctx: PatchContext) -> None:
        subprocess.run(
            ["git", "worktree", "remove", str(ctx.worktree_path), "--force"],
            cwd=ctx.live_repo,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", ctx.branch],
            cwd=ctx.live_repo,
            capture_output=True,
        )
