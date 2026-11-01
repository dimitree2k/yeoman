"""Contributor contract for deterministic group-state sections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .models import Layer, Section, Trust


@dataclass(frozen=True, slots=True)
class SourceReaders:
    """Bounded readers a section may use; every field is optional."""

    speakups: Any | None = None
    processing: Any | None = None
    decisions: Any | None = None
    polls: Any | None = None
    cron: Any | None = None


@dataclass(frozen=True, slots=True)
class BuildContext:
    """One chat-scoped build: no reader may cross into another chat."""

    channel: str
    chat_id: str
    now_ms: int
    #: ``HistoryQueries`` bound to one open snapshot.
    history: Any
    sources: SourceReaders
    #: ``processing.groupState`` settings for this build.
    settings: Any
    #: ``config.consciousness`` bounds used for conversation segmentation.
    consciousness: Any | None = None


class GroupStateContributor(Protocol):
    key: str
    schema_version: int
    trust: Trust
    layer: Layer

    def applies_to(self, channel: str, chat_kind: str) -> bool: ...

    async def build(self, ctx: BuildContext) -> Section: ...

    def migrate(self, from_version: int, data: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ContributorRegistry:
    """An ordered set of contributors; one key may be registered exactly once."""

    def __init__(self) -> None:
        self._contributors: dict[str, GroupStateContributor] = {}

    def register(self, contributor: GroupStateContributor) -> None:
        if contributor.key in self._contributors:
            raise ValueError(f"duplicate group-state contributor: {contributor.key}")
        self._contributors[contributor.key] = contributor

    def for_chat(self, channel: str, chat_kind: str) -> tuple[GroupStateContributor, ...]:
        return tuple(
            contributor
            for contributor in self._contributors.values()
            if contributor.applies_to(channel, chat_kind)
        )
