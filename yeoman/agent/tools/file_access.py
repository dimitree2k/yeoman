"""Scoped file-access resolver with grant support.

Provides a single entry-point for all file-path validation used by
filesystem tools. The resolver enforces a strict resolution order:

    1. Resolve + normalize the requested path.
    2. Blocked patterns -> PermissionError  (highest priority)
    3. Blocked paths   -> PermissionError
    4. Workspace       -> OK  (always read-write)
    5. Grants          -> OK  (only when _grants_active contextvar is True)
    6. Otherwise       -> PermissionError

The ``_grants_active`` context variable is the only mechanism that
enables grant evaluation. It is set server-side by trusted code and is
invisible to the LLM tool-call interface.
"""

from __future__ import annotations

import contextvars
import fnmatch
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Literal

from loguru import logger

if TYPE_CHECKING:
    from nanobot.policy.schema import PolicyConfig

_grants_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "file_access_grants_active",
    default=False,
)


@contextmanager
def enable_grants() -> Iterator[None]:
    """Enable grant evaluation for the current execution context."""
    token = _grants_active.set(True)
    try:
        yield
    finally:
        _grants_active.reset(token)


def grants_are_active() -> bool:
    """Return whether grants are currently enabled in this context."""
    return _grants_active.get(False)


@dataclass(frozen=True, slots=True)
class FileAccessGrant:
    """A single scoped file-access grant (resolved at boot)."""

    id: str
    path: Path
    recursive: bool
    mode: Literal["read", "read-write"]


def _sanitize_grant_id(grant_id: str) -> str:
    """Convert policy grant id into a safe container path segment."""
    normalized = re.sub(r"[^A-Za-z0-9._-]", "-", str(grant_id)).strip(".-")
    return normalized or "grant"


def build_file_access_resolver(
    *,
    workspace: Path,
    policy: "PolicyConfig | None",
) -> "FileAccessResolver | None":
    if policy is None or policy.file_access is None:
        return None

    file_access = policy.file_access
    grants = [
        FileAccessGrant(
            id=grant.id,
            path=Path(grant.path).expanduser().resolve(),
            recursive=bool(grant.recursive),
            mode=grant.mode,
        )
        for grant in file_access.grants
    ]
    blocked_paths = [Path(raw).expanduser().resolve() for raw in file_access.blocked_paths]

    return FileAccessResolver(
        workspace=workspace,
        grants=grants,
        blocked_paths=blocked_paths,
        blocked_patterns=file_access.blocked_patterns,
        owner_only=file_access.owner_only,
        audit=file_access.audit,
    )


class FileAccessResolver:
    """Resolves file paths against workspace + optional grants."""

    def __init__(
        self,
        workspace: Path,
        grants: list[FileAccessGrant] | None = None,
        blocked_paths: list[Path] | None = None,
        blocked_patterns: list[str] | None = None,
        owner_only: bool = True,
        audit: bool = True,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._grants: list[FileAccessGrant] = sorted(
            grants or [],
            key=lambda g: len(g.path.parts),
            reverse=True,
        )
        self._blocked_paths: list[Path] = [p.expanduser().resolve() for p in (blocked_paths or [])]
        self._blocked_patterns: list[str] = list(blocked_patterns or [])
        self._owner_only = bool(owner_only)
        self._audit = bool(audit)

    @property
    def has_grants(self) -> bool:
        return bool(self._grants)

    @property
    def grants(self) -> tuple[FileAccessGrant, ...]:
        return tuple(self._grants)

    @property
    def owner_only(self) -> bool:
        return self._owner_only

    def iter_grant_mounts(self) -> tuple[tuple[Path, str, bool], ...]:
        """Return (host_path, container_path, readonly) tuples for exec sandbox mounts."""
        mounts: list[tuple[Path, str, bool]] = []
        for grant in self._grants:
            container_path = f"/grants/{_sanitize_grant_id(grant.id)}"
            readonly = grant.mode != "read-write"
            mounts.append((grant.path, container_path, readonly))
        return tuple(mounts)

    def grant_container_prefixes(self) -> tuple[str, ...]:
        return tuple(container_path for _, container_path, _ in self.iter_grant_mounts())

    def resolve(
        self,
        path: str,
        *,
        operation: Literal["read", "write", "list"],
    ) -> Path:
        """Resolve *path* and enforce access rules."""
        resolved = Path(path).expanduser().resolve()

        self._check_blocked_patterns(resolved)
        self._check_blocked_paths(resolved)

        if self._is_within(resolved, self._workspace):
            return resolved

        if grants_are_active() and self._grants:
            grant = self._match_grant(resolved)
            if grant is not None:
                if grant.mode == "read" and operation == "write":
                    raise PermissionError(
                        f"Grant {grant.id} allows read-only access; write to {path} is denied"
                    )
                if self._audit:
                    logger.info(
                        "file_access | grant={} op={} path={}", grant.id, operation, resolved
                    )
                return resolved

        if self._audit:
            logger.warning(
                "file_access | DENIED op={} path={} reason=outside_allowed_paths",
                operation,
                resolved,
            )
        raise PermissionError(f"Path {path} is outside allowed directory {self._workspace}")

    def _check_blocked_patterns(self, resolved: Path) -> None:
        name = resolved.name
        path_str = str(resolved)
        for pattern in self._blocked_patterns:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(path_str, pattern):
                if self._audit:
                    logger.warning(
                        "file_access | DENIED path={} reason=blocked_pattern:{}",
                        resolved,
                        pattern,
                    )
                raise PermissionError(f"Access to {resolved.name} is blocked by security policy")
            if not pattern.startswith("*") and pattern in path_str:
                if self._audit:
                    logger.warning(
                        "file_access | DENIED path={} reason=blocked_pattern:{}",
                        resolved,
                        pattern,
                    )
                raise PermissionError(f"Access to {resolved.name} is blocked by security policy")

    def _check_blocked_paths(self, resolved: Path) -> None:
        for blocked in self._blocked_paths:
            if self._is_within(resolved, blocked) or resolved == blocked:
                if self._audit:
                    logger.warning(
                        "file_access | DENIED path={} reason=blocked_path:{}",
                        resolved,
                        blocked,
                    )
                raise PermissionError(f"Access to {resolved} is blocked by security policy")

    def _match_grant(self, resolved: Path) -> FileAccessGrant | None:
        for grant in self._grants:
            if grant.recursive:
                if self._is_within(resolved, grant.path) or resolved == grant.path:
                    return grant
            else:
                if resolved == grant.path or resolved.parent == grant.path:
                    return grant
        return None

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
