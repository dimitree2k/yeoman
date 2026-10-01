"""Where the raw archive lives, and the guard that keeps destructive code away from it.

V1 spec §4.0: nothing except the owner purge may delete, move, truncate or rotate a file
inside ``data/raw/`` or its spool. Every routine that deletes files under a configurable or
swept path calls :func:`assert_deletable` (one file) or :func:`assert_deletable_tree`
(recursive sweep) first. The default runtime home is always protected as well, so a process
running with a different ``YEOMAN_HOME`` cannot sweep the live archive by accident.
"""

from __future__ import annotations

import os
from pathlib import Path

from yeoman_shared.utils.helpers import get_operational_data_path

RAW_DIR = "raw"
SPOOL_DIR = "raw-spool"
PROTECTED_DIR_NAMES: tuple[str, ...] = (RAW_DIR, SPOOL_DIR)
DEFAULT_RUNTIME_DATA = Path("~/.yeoman/data")


class ProtectedPathError(PermissionError):
    """A destructive file operation targeted the raw archive or its spool."""


def raw_root() -> Path:
    """The raw archive root for the current ``YEOMAN_HOME``."""
    return get_operational_data_path() / RAW_DIR


def spool_root() -> Path:
    """The retry spool for lines that could not reach the archive yet."""
    return get_operational_data_path() / SPOOL_DIR


def _normalize(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _lexical_protected_roots() -> tuple[Path, ...]:
    """Normalized protected root paths before following any symlinks."""
    bases = {get_operational_data_path(), DEFAULT_RUNTIME_DATA.expanduser()}
    return tuple(sorted({_normalize(base / name) for base in bases for name in PROTECTED_DIR_NAMES}))


def protected_roots() -> tuple[Path, ...]:
    """Resolved protected roots for the current home and the default runtime home."""
    return tuple(sorted({_resolve(root) for root in _lexical_protected_roots()}))


def is_protected(path: str | Path) -> bool:
    """True when *path* is a protected root or lies inside one."""
    targets = (_normalize(path), _resolve(path))
    lexical_roots = _lexical_protected_roots()
    roots = set(lexical_roots) | {_resolve(root) for root in lexical_roots}
    return any(
        target == root or root in target.parents
        for target in targets
        for root in roots
    )


def contains_protected(path: str | Path) -> bool:
    """True when a recursive operation on *path* would reach a protected root."""
    targets = (_normalize(path), _resolve(path))
    lexical_roots = _lexical_protected_roots()
    roots = set(lexical_roots) | {_resolve(root) for root in lexical_roots}
    return any(
        target == root or root in target.parents or target in root.parents
        for target in targets
        for root in roots
    )


def assert_deletable(path: str | Path) -> None:
    """Refuse to delete, move, truncate or rotate one file inside the raw archive."""
    if is_protected(path):
        raise ProtectedPathError(f"refusing destructive operation on raw archive path: {path}")


def assert_deletable_tree(path: str | Path) -> None:
    """Refuse a recursive destructive operation that would reach the raw archive."""
    if contains_protected(path):
        raise ProtectedPathError(
            f"refusing recursive destructive operation reaching the raw archive: {path}"
        )
