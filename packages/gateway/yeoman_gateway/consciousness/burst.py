"""Burst-triggered wakeups from observed inbound chat activity."""

from __future__ import annotations

import inspect
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

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
        self._last_fired_day: dict[str, str] = self._load_state()

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
        if len(window) < self._config.consciousness.burst_threshold_messages:
            return

        day = datetime.fromtimestamp(float(event.timestamp), UTC).date().isoformat()
        state_key = self._state_key(channel, chat_id)
        if self._last_fired_day.get(state_key) == day:
            return

        self._last_fired_day[state_key] = day
        self._save_state()
        raw = self._on_burst(channel, chat_id)
        if inspect.isawaitable(raw):
            await raw

    async def _eligible(self, channel: str, chat_id: str) -> bool:
        if self._is_eligible is None:
            return True
        raw = self._is_eligible(channel, chat_id)
        if inspect.isawaitable(raw):
            raw = await raw
        return bool(raw)

    def _load_state(self) -> dict[str, str]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw = data.get("last_fired_day", {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key).strip() and str(value).strip()
        }

    def _save_state(self) -> None:
        ensure_dir(self._state_path.parent)
        tmp = self._state_path.with_suffix(f"{self._state_path.suffix}.tmp")
        tmp.write_text(
            json.dumps({"last_fired_day": self._last_fired_day}, indent=2, sort_keys=True),
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
