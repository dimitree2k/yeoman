"""Typing execution adapter backed by ChannelManager."""

from __future__ import annotations

from yeoman.channels.manager import ChannelManager


class ChannelManagerTypingAdapter:
    """Executes typing toggles against channel capabilities."""

    def __init__(self, manager: ChannelManager) -> None:
        self._manager = manager

    async def __call__(self, channel: str, chat_id: str, enabled: bool) -> None:
        await self._manager.set_typing(channel, chat_id, enabled)
