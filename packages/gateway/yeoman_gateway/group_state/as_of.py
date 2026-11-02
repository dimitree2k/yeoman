"""Rebuild one group state as of a past opportunity, for the offline replay.

The builder never stores anything: a replay state keeps ``revision=0`` and is used
only by the renderer. Every source read drops rows that happened after ``at_ms``,
and the poll/cron sources are ``None`` because they keep no history. The decisions
source is used only when it can answer what was open at a time.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loguru import logger

from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.live import HistoryPaused

from .contract import BuildContext, ContributorRegistry, SourceReaders
from .models import SCHEMA_VERSION, GroupState, Section

#: Sections the replay cannot rebuild honestly and the renderer leaves out.
UNAVAILABLE_IN_REPLAY: frozenset[str] = frozenset({"commitments", "conversation_labels"})


def _value(item: Any, field: str) -> Any:
    return item.get(field) if isinstance(item, Mapping) else getattr(item, field, None)


def _moment(item: Any, field: str) -> int | None:
    value = _value(item, field)
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _not_after(item: Any, at_ms: int, *fields: str) -> bool:
    """True when no known timestamp of ``item`` lies after ``at_ms``."""
    for field in fields:
        moment = _moment(item, field)
        if moment is not None and moment > at_ms:
            return False
    return True


class _AsOfSpeakups:
    def __init__(self, inner: Any, at_ms: int) -> None:
        self._inner = inner
        self._at_ms = int(at_ms)

    async def judge_attempts_since(
        self, *, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        rows = await self._inner.judge_attempts_since(
            channel=channel, chat_id=chat_id, since_ms=since_ms
        )
        return [row for row in rows if _not_after(row, self._at_ms, "created_at_ms")]


class _AsOfProcessing:
    def __init__(self, inner: Any, at_ms: int) -> None:
        self._inner = inner
        self._at_ms = int(at_ms)

    def short_reply_decisions_since(
        self, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        rows = self._inner.short_reply_decisions_since(channel, chat_id, since_ms)
        return [row for row in rows if _not_after(row, self._at_ms, "created_ms", "updated_ms")]

    def recent_reactions(
        self, *, channel: str, chat_id: str, since_ms: int, limit: int
    ) -> list[Any]:
        rows = self._inner.recent_reactions(
            channel=channel, chat_id=chat_id, since_ms=since_ms, limit=limit
        )
        return [row for row in rows if _not_after(row, self._at_ms, "created_ms")]

    def thread_for_message(self, native_id: str) -> Any | None:
        reader = getattr(self._inner, "thread_for_message_as_of", None)
        if callable(reader):
            return reader(native_id, self._at_ms)
        return self._inner.thread_for_message(native_id)


class _AsOfDecisions:
    """Only a reader that can answer "what was open at ``at_ms``" is usable."""

    def __init__(self, inner: Any, at_ms: int) -> None:
        self._inner = inner
        self._at_ms = int(at_ms)

    def list_open_as_of(self, chat_id: str, at_ms: int | None = None) -> list[Any]:
        rows = self._inner.list_open_as_of(chat_id, self._at_ms)
        return [row for row in rows if _not_after(row, self._at_ms, "created_at_ms")]


class AsOfSources:
    """Wrap live readers so every read sees only rows at or before ``at_ms``."""

    def __init__(self, sources: SourceReaders, at_ms: int) -> None:
        self._sources = sources
        self._at_ms = int(at_ms)

    def readers(self) -> SourceReaders:
        decisions = self._sources.decisions
        return SourceReaders(
            speakups=(
                _AsOfSpeakups(self._sources.speakups, self._at_ms)
                if self._sources.speakups is not None
                else None
            ),
            processing=(
                _AsOfProcessing(self._sources.processing, self._at_ms)
                if self._sources.processing is not None
                else None
            ),
            decisions=(
                _AsOfDecisions(decisions, self._at_ms)
                if decisions is not None and callable(getattr(decisions, "list_open_as_of", None))
                else None
            ),
            # Polls and cron keep no history, so a replay cannot rebuild them.
            polls=None,
            cron=None,
        )


async def build_state_as_of(
    *,
    registry: ContributorRegistry,
    channel: str,
    chat_id: str,
    at_ms: int,
    history: Any,
    sources: SourceReaders,
    settings: Any,
    consciousness: Any | None,
) -> GroupState:
    """One unstored state at ``at_ms``; a failing section is left out, never raised."""
    channel = str(channel)
    chat_id = str(chat_id)
    at_ms = int(at_ms)
    try:
        chat_kind = getattr(classify(chat_id), "kind", "unknown")
    except Exception:  # noqa: BLE001 - an unclassifiable chat is simply unmanaged
        chat_kind = "unknown"
    ctx = BuildContext(
        channel=channel,
        chat_id=chat_id,
        now_ms=at_ms,
        history=history,
        sources=AsOfSources(sources, at_ms).readers(),
        settings=settings,
        consciousness=consciousness,
    )
    sections: dict[str, Section] = {}
    for contributor in registry.for_chat(channel, chat_kind):
        if contributor.key in UNAVAILABLE_IN_REPLAY:
            # Its sources keep no history; building it would only report today's state.
            continue
        try:
            sections[contributor.key] = await contributor.build(ctx)
        except HistoryPaused:
            raise
        except Exception as exc:  # noqa: BLE001 - one section must not stop the rest
            logger.warning(
                "group_state_as_of_section_failed key={} error_type={}",
                contributor.key,
                type(exc).__name__,
            )
    return GroupState(
        channel=channel,
        chat_id=chat_id,
        schema_version=SCHEMA_VERSION,
        revision=0,
        history_cursor=_newest_cursor(sections),
        updated_ms=at_ms,
        sections=sections,
    )


def _newest_cursor(sections: dict[str, Section]) -> str:
    """The newest known message id, preferring the widest observation window."""
    for key in ("presence", "conversations", "people_activity", "arvid_actions", "budgets"):
        section = sections.get(key)
        if section is not None and section.source_cursor:
            return section.source_cursor
    return next(
        (section.source_cursor for section in sections.values() if section.source_cursor),
        "",
    )


__all__ = ["AsOfSources", "UNAVAILABLE_IN_REPLAY", "build_state_as_of"]
