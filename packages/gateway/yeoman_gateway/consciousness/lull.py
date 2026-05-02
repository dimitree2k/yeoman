"""Lull-triggered wakeups for chats that went quiet after recent activity.

Complements the burst observer: while burst fires inside a flurry of inbound
messages, lull fires *between* flurries — when a chat had a meaningful run of
recent activity but has now gone silent for a configurable cooldown. Each
chat shares the same per-day budget (``default_daily_cap``) as the burst
path, so the two observers cannot exceed the configured cap together.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from yeoman_shared.config.schema import Config
from yeoman_shared.utils.helpers import ensure_dir

from yeoman_gateway.bus.events import GatewayEvent, InboundObservedEvent

LullCallback = Callable[[str, str], None | Awaitable[None]]
EligibilityCallback = Callable[[str, str], bool | Awaitable[bool]]
ClockFn = Callable[[], float]


class LullObserver:
    """Fire spontaneous consciousness ticks in chats that just went quiet."""

    def __init__(
        self,
        *,
        config: Config,
        state_path: Path,
        on_lull: LullCallback,
        is_eligible: EligibilityCallback | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._state_path = state_path.expanduser()
        self._on_lull = on_lull
        self._is_eligible = is_eligible
        self._clock = clock or time.time
        self._activity: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._fires_today: dict[str, dict[str, object]] = self._load_state()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()

    async def handle(self, event: GatewayEvent) -> None:
        """Track inbound activity timestamps. Does not fire by itself."""

        if not isinstance(event, InboundObservedEvent):
            return
        if not self._config.consciousness.enabled or not self._config.consciousness.lull_enabled:
            return
        channel = str(event.channel or "").strip()
        chat_id = str(event.chat_id or "").strip()
        if not channel or not chat_id:
            return
        if self._is_direct_bot_interaction(event):
            return
        key = (channel, chat_id)
        ts = float(event.timestamp)
        cutoff = ts - (self._config.consciousness.lull_activity_window_minutes * 60)
        bucket = self._activity[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(ts)

    async def start(self) -> None:
        if not self._config.consciousness.enabled or not self._config.consciousness.lull_enabled:
            logger.info("Lull observer disabled (consciousness or lull flag off)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Lull observer started silence_minutes={} activity_window_minutes={} "
            "min_recent_activity={} interval_seconds={}",
            self._config.consciousness.lull_silence_minutes,
            self._config.consciousness.lull_activity_window_minutes,
            self._config.consciousness.lull_min_recent_activity,
            self._config.consciousness.lull_check_interval_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        interval = max(10, int(self._config.consciousness.lull_check_interval_seconds))
        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Lull observer tick failed: {}", exc)
                await asyncio.sleep(interval)

    async def _tick(self) -> None:
        async with self._tick_lock:
            now = float(self._clock())
            silence_secs = self._config.consciousness.lull_silence_minutes * 60
            window_secs = self._config.consciousness.lull_activity_window_minutes * 60
            min_recent = max(1, int(self._config.consciousness.lull_min_recent_activity))
            cap = max(0, int(self._config.consciousness.default_daily_cap))

            for (channel, chat_id), bucket in list(self._activity.items()):
                cutoff = now - window_secs
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if not bucket:
                    continue
                last_seen = bucket[-1]
                if now - last_seen < silence_secs:
                    continue
                if len(bucket) < min_recent:
                    continue
                if not await self._eligible(channel, chat_id):
                    continue

                day = datetime.fromtimestamp(now, UTC).date().isoformat()
                state_key = self._state_key(channel, chat_id)
                count_today = self._count_for_day(state_key, day)
                if cap and count_today >= cap:
                    logger.debug(
                        "lull observer skip: cap reached channel={} chat={} count={} cap={}",
                        channel,
                        chat_id,
                        count_today,
                        cap,
                    )
                    continue

                logger.info(
                    "lull observer firing channel={} chat={} silence_seconds={} "
                    "recent_activity={} fires_today={} cap={}",
                    channel,
                    chat_id,
                    int(now - last_seen),
                    len(bucket),
                    count_today,
                    cap,
                )
                try:
                    raw = self._on_lull(channel, chat_id)
                    if inspect.isawaitable(raw):
                        await raw
                except Exception as exc:
                    logger.warning(
                        "lull observer callback failed channel={} chat={}: {}",
                        channel,
                        chat_id,
                        exc,
                    )
                    continue
                self._increment_for_day(state_key, day)
                self._save_state()

    async def _eligible(self, channel: str, chat_id: str) -> bool:
        if self._is_eligible is None:
            return True
        raw = self._is_eligible(channel, chat_id)
        if inspect.isawaitable(raw):
            raw = await raw
        return bool(raw)

    def _count_for_day(self, state_key: str, day: str) -> int:
        entry = self._fires_today.get(state_key)
        if not isinstance(entry, dict) or entry.get("date") != day:
            return 0
        try:
            return int(entry.get("count", 0))
        except (TypeError, ValueError):
            return 0

    def _increment_for_day(self, state_key: str, day: str) -> None:
        existing = self._fires_today.get(state_key)
        if isinstance(existing, dict) and existing.get("date") == day:
            count = self._count_for_day(state_key, day) + 1
        else:
            count = 1
        self._fires_today[state_key] = {"date": day, "count": count}

    def _load_state(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        fires = data.get("fires_today")
        if not isinstance(fires, dict):
            return {}
        normalized: dict[str, dict[str, object]] = {}
        for key, value in fires.items():
            if not str(key).strip() or not isinstance(value, dict):
                continue
            date = str(value.get("date") or "").strip()
            if not date:
                continue
            try:
                count = int(value.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            normalized[str(key)] = {"date": date, "count": max(0, count)}
        return normalized

    def _save_state(self) -> None:
        ensure_dir(self._state_path.parent)
        tmp = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        tmp.write_text(
            json.dumps({"fires_today": self._fires_today}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._state_path)

    @staticmethod
    def _state_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    @staticmethod
    def _is_direct_bot_interaction(event: InboundObservedEvent) -> bool:
        metadata = event.metadata
        return any(
            bool(metadata.get(key))
            for key in (
                "mentioned_bot",
                "mentionedBot",
                "reply_to_bot",
                "replyToBot",
                "from_me",
                "fromMe",
            )
        )
