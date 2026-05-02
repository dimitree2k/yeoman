"""Burst-triggered wakeups from observed inbound chat activity."""

from __future__ import annotations

import inspect
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from yeoman_shared.config.schema import Config
from yeoman_shared.utils.helpers import ensure_dir

from yeoman_gateway.bus.events import GatewayEvent, InboundObservedEvent

BurstCallback = Callable[[str, str], None | Awaitable[None]]
EligibilityCallback = Callable[[str, str], bool | Awaitable[bool]]


class BurstObserver:
    """Observe inbound activity and request bounded consciousness burst ticks."""

    def __init__(
        self,
        *,
        config: Config,
        state_path: Path,
        on_burst: BurstCallback,
        is_eligible: EligibilityCallback | None = None,
    ) -> None:
        self._config = config
        self._state_path = state_path.expanduser()
        self._on_burst = on_burst
        self._is_eligible = is_eligible
        self._windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._fires_today: dict[str, dict[str, object]] = self._load_state()

    async def handle(self, event: GatewayEvent) -> None:
        if not isinstance(event, InboundObservedEvent):
            return
        if not self._config.consciousness.enabled or not self._config.consciousness.burst_enabled:
            return
        channel = str(event.channel or "").strip()
        chat_id = str(event.chat_id or "").strip()
        if not channel or not chat_id:
            return
        if self._is_direct_bot_interaction(event):
            return
        if not await self._eligible(channel, chat_id):
            return

        key = (channel, chat_id)
        cutoff = float(event.timestamp) - (self._config.consciousness.burst_window_minutes * 60)
        window = self._windows[key]
        while window and window[0] < cutoff:
            window.popleft()
        window.append(float(event.timestamp))
        threshold = self._config.consciousness.burst_threshold_messages
        if len(window) < threshold:
            logger.debug(
                "burst observer accumulating channel={} chat={} window_size={} threshold={}",
                channel,
                chat_id,
                len(window),
                threshold,
            )
            return

        day = datetime.fromtimestamp(float(event.timestamp), UTC).date().isoformat()
        state_key = self._state_key(channel, chat_id)
        cap = max(0, int(self._config.consciousness.default_daily_cap))
        count_today = self._count_for_day(state_key, day)
        if cap and count_today >= cap:
            logger.info(
                "burst observer daily cap reached channel={} chat={} count={} cap={}",
                channel,
                chat_id,
                count_today,
                cap,
            )
            return

        logger.info(
            "burst observer firing channel={} chat={} window_size={} threshold={} "
            "fires_today={} cap={} window_minutes={}",
            channel,
            chat_id,
            len(window),
            threshold,
            count_today,
            cap,
            self._config.consciousness.burst_window_minutes,
        )
        raw = self._on_burst(channel, chat_id)
        if inspect.isawaitable(raw):
            await raw
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
        if isinstance(fires, dict):
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
        legacy = data.get("last_fired_day")
        if isinstance(legacy, dict):
            return {
                str(key): {"date": str(value), "count": 1}
                for key, value in legacy.items()
                if str(key).strip() and str(value).strip()
            }
        return {}

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
