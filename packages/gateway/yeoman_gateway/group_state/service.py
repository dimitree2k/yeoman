"""Shadow scheduler for per-chat group state.

The hot path only marks a chat dirty. A ticker rebuilds dirty chats no more often
than ``minIntervalSeconds`` and every managed chat every ``rebuildEverySeconds``.
Every rebuild reads history through ``history_turn`` and closes that scope before
the CAS store write, so no history transaction is held across a store write.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from loguru import logger

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries

from .contract import BuildContext, ContributorRegistry, SourceReaders
from .models import SCHEMA_VERSION, GroupState, Section
from .store import GroupStateStore


class GroupStateService:
    """Keep one ``GroupState`` per managed chat up to date (shadow only)."""

    def __init__(
        self,
        *,
        store: GroupStateStore,
        registry: ContributorRegistry,
        settings: Any,
        history_projector: Any,
        sources: SourceReaders,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        consciousness: Any | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._settings = settings
        self._history_projector = history_projector
        self._sources = sources
        self._clock = clock_ms
        self._consciousness = consciousness
        self._managed = {
            (channel, chat_id)
            for channel, _, chat_id in (
                entry.strip().partition(":") for entry in settings.chats if entry.strip()
            )
            if channel and chat_id
        }
        self._dirty: set[tuple[str, str]] = set()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._last_rebuild: dict[tuple[str, str], int] = {}
        self._last_full = int(clock_ms())
        self._task: asyncio.Task[None] | None = None

    @property
    def store(self) -> GroupStateStore:
        return self._store

    def manages(self, channel: str, chat_id: str) -> bool:
        settings_mode = getattr(self._settings, "mode", "off")
        return settings_mode == "shadow" and (str(channel), str(chat_id)) in self._managed

    def managed_chats(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._managed))

    def mark_dirty(self, channel: str, chat_id: str) -> None:
        """O(1) and never raises: the bus must not learn about a state problem."""
        try:
            if self.manages(channel, chat_id):
                self._dirty.add((str(channel), str(chat_id)))
        except Exception as exc:  # noqa: BLE001 - the hot path is best effort
            logger.debug("group_state_mark_dirty_skipped error_type={}", type(exc).__name__)

    async def handle_event(self, event: Any) -> None:
        self.mark_dirty(str(getattr(event, "channel", "")), str(getattr(event, "chat_id", "")))

    async def rebuild(self, channel: str, chat_id: str) -> GroupState | None:
        if not self.manages(channel, chat_id):
            return None
        key = (str(channel), str(chat_id))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            started = time.perf_counter()
            existing = self._store.get(*key)
            sections: dict[str, Section] = {}
            now_ms = int(self._clock())
            chat_kind = getattr(classify(chat_id), "kind", "unknown")
            try:
                async with history_turn(self._history_projector) as snapshot:
                    ctx = BuildContext(
                        channel=key[0],
                        chat_id=key[1],
                        now_ms=now_ms,
                        history=HistoryQueries(snapshot),
                        sources=self._sources,
                        settings=self._settings,
                        consciousness=self._consciousness,
                    )
                    for contributor in self._registry.for_chat(key[0], chat_kind):
                        previous = existing.sections.get(contributor.key) if existing else None
                        try:
                            sections[contributor.key] = await contributor.build(ctx)
                        except HistoryPaused:
                            raise
                        except Exception as exc:  # noqa: BLE001 - one section must not stop the rest
                            logger.warning(
                                "group_state_section_failed key={} error_type={}",
                                contributor.key,
                                type(exc).__name__,
                            )
                            if previous is not None:
                                sections[contributor.key] = previous
            except HistoryPaused as exc:
                self._dirty.add(key)
                logger.warning(
                    "group_state_rebuild_skipped_paused chat={}:{} reason={}",
                    key[0],
                    key[1],
                    str(exc) or "paused",
                )
                return None
            except Exception as exc:  # noqa: BLE001 - the ticker keeps the chat dirty
                self._dirty.add(key)
                logger.warning(
                    "group_state_rebuild_failed chat={}:{} error_type={}",
                    key[0],
                    key[1],
                    type(exc).__name__,
                )
                return None

            revision = (existing.revision if existing else 0) + 1
            state = GroupState(
                channel=key[0],
                chat_id=key[1],
                schema_version=SCHEMA_VERSION,
                revision=revision,
                history_cursor=_newest_cursor(sections),
                updated_ms=now_ms,
                sections=sections,
            )
            expected = existing.revision if existing else None
            if not self._store.put(state, expected_revision=expected):
                current = self._store.get(*key)
                retry_revision = (current.revision if current else 0) + 1
                retry = replace(state, revision=retry_revision)
                if not self._store.put(
                    retry, expected_revision=current.revision if current else None
                ):
                    logger.warning(
                        "group_state_conflict_give_up chat={}:{} revision={}",
                        key[0],
                        key[1],
                        retry_revision,
                    )
                    self._dirty.add(key)
                    return None
                state = retry
            self._dirty.discard(key)
            self._last_rebuild[key] = now_ms
            logger.info(
                "group_state_updated chat={}:{} revision={} sections={} elapsed_ms={}",
                key[0],
                key[1],
                state.revision,
                len(state.sections),
                int((time.perf_counter() - started) * 1000),
            )
            return state

    async def tick_once(self) -> None:
        """One scheduler pass; exposed for tests and deterministic callers."""
        now_ms = int(self._clock())
        min_interval_ms = max(1, int(self._settings.min_interval_seconds)) * 1000
        full = now_ms - self._last_full >= max(1, int(self._settings.rebuild_every_seconds)) * 1000
        targets = self.managed_chats() if full else tuple(sorted(self._dirty))
        for channel, chat_id in targets:
            key = (channel, chat_id)
            if not full and key not in self._dirty:
                continue
            last = self._last_rebuild.get(key)
            if last is not None and now_ms - last < min_interval_ms:
                continue
            await self.rebuild(channel, chat_id)
        if full:
            self._last_full = now_ms

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="group-state")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def close(self) -> None:
        self._store.close()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a tick failure must not stop the loop
                logger.warning("group_state_tick_failed error_type={}", type(exc).__name__)


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
