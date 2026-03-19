"""Direct Telegram Bot API client — no gateway dependency."""
from __future__ import annotations

from yeoman_overseer.comms.cascading import CommsChannel

import httpx


class TelegramDirectChannel(CommsChannel):
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "Markdown",
            })
            resp.raise_for_status()
