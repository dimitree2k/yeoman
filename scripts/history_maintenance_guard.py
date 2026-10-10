"""Metadata-only path preflight for offline maintenance utilities."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_homes() -> tuple[Path, ...]:
    """Runtime homes isolated maintenance must stay outside of, unresolved.

    A caller that has to refuse a path *before* any filesystem access (so a live-home
    argument is never even stat'ed) compares against these lexically; the resolved
    variants in :func:`preflight_isolated_paths` stay authoritative for content reads.
    """
    homes = {
        Path("/home/dm/.yeoman"),
        Path.home() / ".yeoman",
    }
    configured_home = os.environ.get("YEOMAN_HOME", "").strip()
    if configured_home:
        homes.add(Path(configured_home).expanduser())
    return tuple(homes)


def preflight_isolated_paths(*paths: Path) -> None:
    """Reject relative, symlinked, or runtime paths before any content access."""
    if not paths or any(not path.is_absolute() for path in paths):
        raise ValueError("explicit absolute isolated paths required")

    protected = tuple(home.resolve() for home in runtime_homes())

    for path in paths:
        for candidate in (path, *(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", ".lock"))):
            if any(part.is_symlink() for part in (candidate, *candidate.parents)):
                raise ValueError("symlinked isolated paths are refused")
            resolved = candidate.resolve()
            if any(resolved == home or home in resolved.parents for home in protected):
                raise ValueError("runtime paths are refused")
