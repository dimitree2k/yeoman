"""Transactional incremental projection contracts; engine implementation follows in Task 3."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.records import SourceBoundary

from .layer1 import Layer1Line
from .project import ProjectionRows


class RebuildRequired(ValueError):  # noqa: N818 - exact Task 2/3 contract name
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class ProjectionDelta:
    rows: ProjectionRows
    message_ids: set[str]
    event_ids: set[str]
    requires_rebuild: bool
    reason: str | None
    pending_pairs: dict[str, list[str]]


class ProjectionIndex:
    @classmethod
    def from_prefix(cls, raw_root: Path, boundaries: Sequence[SourceBoundary]) -> ProjectionIndex:
        # Task 3 implements prefix recovery.
        raise NotImplementedError

    def plan(self, lines: Sequence[Layer1Line]) -> ProjectionDelta:
        # Task 3 implements dependency closure planning.
        raise NotImplementedError

    def accept(self, delta: ProjectionDelta) -> None:
        # Task 3 implements post-commit cache acceptance.
        raise NotImplementedError


def apply_committed(conn: sqlite3.Connection, index: ProjectionIndex, raw_root: Path,
                    target: Sequence[SourceBoundary]) -> dict[str, Any]:
    # Task 3 implements transactional rows and checkpoint advancement.
    raise NotImplementedError
