"""Metadata-only path preflight for offline maintenance utilities."""

from __future__ import annotations

import os
from pathlib import Path


def preflight_isolated_paths(*paths: Path) -> None:
    """Reject relative, symlinked, or runtime paths before any content access."""
    if not paths or any(not path.is_absolute() for path in paths):
        raise ValueError("explicit absolute isolated paths required")

    runtime_homes = {
        Path("/home/dm/.yeoman"),
        Path.home() / ".yeoman",
    }
    configured_home = os.environ.get("YEOMAN_HOME", "").strip()
    if configured_home:
        runtime_homes.add(Path(configured_home).expanduser())
    protected = tuple(home.resolve() for home in runtime_homes)

    for path in paths:
        for candidate in (path, *(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", ".lock"))):
            if any(part.is_symlink() for part in (candidate, *candidate.parents)):
                raise ValueError("symlinked isolated paths are refused")
            resolved = candidate.resolve()
            if any(resolved == home or home in resolved.parents for home in protected):
                raise ValueError("runtime paths are refused")
