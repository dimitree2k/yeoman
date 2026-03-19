"""Cascading delivery — try channels in order until one succeeds."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class CommsChannel(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def send(self, message: str) -> None: ...


@dataclass
class CascadingComms:
    channels: list[CommsChannel] = field(default_factory=list)
    local_log: bool = False
    local_messages: list[str] = field(default_factory=list)

    async def send(self, message: str) -> None:
        errors: list[str] = []
        for channel in self.channels:
            try:
                await channel.send(message)
                return
            except Exception as exc:
                errors.append(f"{channel.name}: {exc}")
                logger.warning("Channel %s failed: %s", channel.name, exc)
        if self.local_log:
            self.local_messages.append(message)
            logger.error("All channels failed, saved to local log: %s", errors)
            return
        raise RuntimeError(f"All communication channels failed: {'; '.join(errors)}")
