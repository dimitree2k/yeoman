"""Direct Telegram Bot API client — no gateway dependency."""
from __future__ import annotations

import httpx

from yeoman_overseer.comms.cascading import CommsChannel


class TelegramDirectChannel(CommsChannel):
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        # Truncate to Telegram's 4096-char limit
        text = message[:4096]
        async with httpx.AsyncClient(timeout=10) as client:
            # Try Markdown first; fall back to plain text if it fails
            resp = await client.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            })
            if resp.status_code == 400:
                resp = await client.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                })
            resp.raise_for_status()
