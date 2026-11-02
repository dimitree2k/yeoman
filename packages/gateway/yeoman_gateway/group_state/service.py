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
from typing import Any

from loguru import logger

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries

from .contract import BuildContext, ContributorRegistry, SourceReaders
from .models import GroupState, Section
from .store import GroupStateStore

#: Message sources checked for a purge per invalidation pass.
PURGE_CHECK_LIMIT = 200


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
        self._last_purge_check: dict[tuple[str, str], int] = {}
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
            remove: set[str] = set()
            now_ms = int(self._clock())
            chat_kind = getattr(classify(chat_id), "kind", "unknown")
            #: Waiting for the history lease yields the loop; section builds do not.
            wait_ms = build_ms = 0
            slowest = ("-", 0)
            try:
                async with history_turn(self._history_projector) as snapshot:
                    wait_ms = int((time.perf_counter() - started) * 1000)
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
                        section_started = time.perf_counter()
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
                            else:
                                remove.add(contributor.key)
                        section_ms = int((time.perf_counter() - section_started) * 1000)
                        build_ms += section_ms
                        if section_ms > slowest[1]:
                            slowest = (contributor.key, section_ms)
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

            store_started = time.perf_counter()
            state = self._store.merge_sections(
                key[0],
                key[1],
                sections,
                remove=remove,
                updated_ms=now_ms,
            )
            if state is None:
                logger.warning("group_state_conflict_give_up chat={}:{}", key[0], key[1])
                self._dirty.add(key)
                return None
            store_ms = int((time.perf_counter() - store_started) * 1000)
            self._dirty.discard(key)
            self._last_rebuild[key] = now_ms
            logger.info(
                "group_state_updated chat={}:{} revision={} sections={} elapsed_ms={} "
                "wait_ms={} build_ms={} store_ms={} slowest={}:{}",
                key[0],
                key[1],
                state.revision,
                len(state.sections),
                int((time.perf_counter() - started) * 1000),
                wait_ms,
                build_ms,
                store_ms,
                slowest[0],
                slowest[1],
            )
            return state

    async def tick_once(self) -> None:
        """One scheduler pass; exposed for tests and deterministic callers."""
        now_ms = int(self._clock())
        # Invalidate before rebuilding: a rebuild must not re-add a deleted source's item.
        for channel, chat_id in self.managed_chats():
            await self._invalidate_chat(channel, chat_id, now_ms)
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

    async def _invalidate_chat(self, channel: str, chat_id: str, now_ms: int) -> None:
        """Drop derived items whose source message was deleted, edited or purged."""
        key = (str(channel), str(chat_id))
        cursor_ms, cursor_event = self._store.invalidation_cursor(*key)
        state = self._store.get(*key)
        purge_interval_ms = max(1, int(self._settings.rebuild_every_seconds)) * 1000
        last_check = self._last_purge_check.get(key)
        check_purge = last_check is None or now_ms - last_check >= purge_interval_ms
        sources: tuple[str, ...] = ()
        if check_purge and state is not None:
            sources = tuple(
                sorted(
                    {
                        str(source)
                        for section in state.sections.values()
                        for item in section.items
                        for source in item.sources
                    }
                )[:PURGE_CHECK_LIMIT]
            )
        try:
            async with history_turn(self._history_projector) as snapshot:
                queries = HistoryQueries(snapshot)
                events = queries.content_events_after(
                    chat_id=key[1],
                    after_ms=cursor_ms,
                    after_event_id=cursor_event,
                )
                targets = {
                    str(row["target_message_id"]) for row in events if row["target_message_id"]
                }
                targets.update(
                    source for source in sources if queries.content_fingerprint(source) is None
                )
        except HistoryPaused as exc:
            logger.warning(
                "group_state_invalidation_skipped_paused chat={}:{} reason={}",
                key[0],
                key[1],
                str(exc) or "paused",
            )
            return
        except Exception as exc:  # noqa: BLE001 - a tick failure must not stop the loop
            logger.warning(
                "group_state_invalidation_failed chat={}:{} error_type={}",
                key[0],
                key[1],
                type(exc).__name__,
            )
            return
        if check_purge:
            self._last_purge_check[key] = now_ms
        removed = self._store.scrub_items(key[0], key[1], targets) if targets else 0
        if events:
            last = events[-1]
            self._store.set_invalidation_cursor(
                key[0], key[1], int(last["occurred_ms"]), str(last["event_id"])
            )
        if events or removed:
            logger.info(
                "group_state_invalidated chat={}:{} events={} items_removed={}",
                key[0],
                key[1],
                len(events),
                removed,
            )

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
