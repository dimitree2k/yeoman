"""Internal git operations — init, commit, revert, log."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class InternalGit:
    """Manages an internal git repo for overseer state and audit trail."""

    def __init__(self, repo_dir: Path) -> None:
        self._dir = repo_dir

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self._dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def init(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not (self._dir / ".git").is_dir():
            self._run("init")
            self._run("config", "user.email", "overseer@yeoman.local")
            self._run("config", "user.name", "yeoman-overseer")

    def commit(self, *, files: list[str], message: str) -> str | None:
        if not files:
            return None
        for f in files:
            path = self._dir / f
            if path.exists():
                self._run("add", f)
        result = self._run("diff", "--cached", "--quiet", check=False)
        if result.returncode == 0:
            return None
        self._run("commit", "-m", message)
        sha = self._run("rev-parse", "HEAD").stdout.strip()
        return sha

    def revert(self, sha: str) -> None:
        self._run("revert", "--no-edit", sha)

    def log(self, *, limit: int = 10) -> list[dict[str, Any]]:
        fmt = "%H%n%ai%n%s%n---"
        result = self._run("log", f"-{limit}", f"--format={fmt}", check=False)
        if result.returncode != 0:
            return []
        entries: list[dict[str, Any]] = []
        blocks = result.stdout.strip().split("---\n")
        for block in blocks:
            lines = block.strip().splitlines()
            if len(lines) >= 3:
                entries.append({"sha": lines[0], "date": lines[1], "message": lines[2]})
        return entries
