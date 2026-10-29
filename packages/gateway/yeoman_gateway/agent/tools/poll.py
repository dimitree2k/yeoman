"""Tool that posts a WhatsApp poll into the current chat, with a persistent per-chat guard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.processing.tool_context import current_tool_context, tool_in_group_chat

MAX_QUESTION_CHARS = 255
MAX_OPTION_CHARS = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 12
#: A poll counts as open until it is closed or this old.
POLL_TTL_MS = 24 * 60 * 60 * 1000
#: Polls per chat in any rolling window of POLL_TTL_MS, closed ones included.
MAX_POLLS_PER_WINDOW = 3

#: (chat_id, question, options, max_selections, client_message_id) -> provider poll id.
PollSender = Callable[[str, str, list[str], int, str], Awaitable[str | None]]


class PollGuard:
    """Per-chat poll limits in one small JSON file, so a restart cannot reset them.

    At most one open poll per chat (open until closed or POLL_TTL_MS old) and at most
    MAX_POLLS_PER_WINDOW polls per chat per rolling POLL_TTL_MS. Entries older than the
    window are dropped on every write, so the file stays small.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _store_path(self) -> Path:
        if self._path is None:
            from yeoman_shared.utils.helpers import get_operational_store_path

            self._path = get_operational_store_path("poll_guard")
        return self._path

    def _read(self, now_ms: int) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._store_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("poll guard unreadable, starting empty: {}", type(exc).__name__)
            return []
        entries = raw.get("polls") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return []
        return [
            entry for entry in entries
            if isinstance(entry, dict) and int(entry.get("opened_ms", 0)) > now_ms - POLL_TTL_MS
        ]

    def _write(self, entries: list[dict[str, Any]]) -> None:
        path = self._store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"polls": entries}), encoding="utf-8")
        os.replace(tmp, path)

    async def reserve(self, chat_id: str, client_id: str, now_ms: int) -> str | None:
        """Reserve a poll slot; returns a refusal for the model, or None when reserved."""
        async with self._lock:
            entries = self._read(now_ms)
            if any(entry.get("id") == client_id for entry in entries):
                return "This poll was already sent; do not send it again."
            in_chat = [entry for entry in entries if entry.get("chat") == chat_id]
            if any(entry.get("closed_ms") is None for entry in in_chat):
                return (
                    "A poll is already open in this chat. Close it first (action 'close') "
                    "or wait until it is a day old."
                )
            if len(in_chat) >= MAX_POLLS_PER_WINDOW:
                return f"This chat already had {MAX_POLLS_PER_WINDOW} polls in the last 24 hours."
            entries.append({
                "chat": chat_id, "id": client_id, "opened_ms": now_ms,
                "closed_ms": None, "poll_id": None,
            })
            self._write(entries)
            return None

    async def confirm(self, client_id: str, poll_id: str | None, now_ms: int) -> None:
        async with self._lock:
            entries = self._read(now_ms)
            for entry in entries:
                if entry.get("id") == client_id:
                    entry["poll_id"] = poll_id
            self._write(entries)

    async def release(self, client_id: str, now_ms: int) -> None:
        """Give a reservation back after a failed send."""
        async with self._lock:
            entries = self._read(now_ms)
            self._write([entry for entry in entries if entry.get("id") != client_id])

    async def close(self, chat_id: str, now_ms: int) -> bool:
        """Close this chat's open poll; False when none was open."""
        async with self._lock:
            entries = self._read(now_ms)
            closed = False
            for entry in entries:
                if entry.get("chat") == chat_id and entry.get("closed_ms") is None:
                    entry["closed_ms"] = now_ms
                    closed = True
            if closed:
                self._write(entries)
            return closed


def normalize_poll(question: Any, options: Any) -> tuple[str, list[str]] | str:
    """Validated (question, options) or an error message for the model."""
    text = " ".join(str(question or "").split())
    if not text:
        return "Error: a poll needs a question."
    if len(text) > MAX_QUESTION_CHARS:
        return f"Error: the question is longer than {MAX_QUESTION_CHARS} characters."
    if not isinstance(options, list):
        return "Error: options must be a list of strings."
    cleaned: list[str] = []
    seen: set[str] = set()
    for option in options:
        value = " ".join(str(option or "").split())
        if not value or value.casefold() in seen:
            continue
        if len(value) > MAX_OPTION_CHARS:
            return f"Error: each option must be at most {MAX_OPTION_CHARS} characters."
        seen.add(value.casefold())
        cleaned.append(value)
    if not MIN_OPTIONS <= len(cleaned) <= MAX_OPTIONS:
        return f"Error: a poll needs {MIN_OPTIONS} to {MAX_OPTIONS} different options."
    return text, cleaned


class PollTool(Tool):
    """Post a WhatsApp poll into the current chat; it never takes a target."""

    def __init__(
        self,
        *,
        sender: Callable[[], PollSender | None],
        guard: PollGuard | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._sender = sender
        self._guard = guard or PollGuard()
        self._clock_ms = clock_ms

    @property
    def name(self) -> str:
        return "poll"

    @property
    def description(self) -> str:
        return (
            "Post a WhatsApp poll into the current chat (action 'create'), or mark this "
            "chat's open poll as closed (action 'close'). Use it only when someone "
            "explicitly asks for a poll, or when several people are stuck on one concrete "
            "choice (a date, a place, an option). Never for small talk, jokes or rhetorical "
            "questions. One open poll per chat and at most three per chat per day. Write a "
            "short neutral question in the chat's language with 2-12 distinct options. The "
            "poll is sent on its own; do not repeat the options as text."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "close"], "default": "create"},
                "question": {"type": "string", "maxLength": MAX_QUESTION_CHARS},
                "options": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": MAX_OPTION_CHARS},
                    "minItems": MIN_OPTIONS,
                    "maxItems": MAX_OPTIONS,
                },
                "multi_select": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow choosing several options.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str = "create",
        question: str = "",
        options: list[str] | None = None,
        multi_select: bool = False,
        **kwargs: Any,
    ) -> str:
        context = current_tool_context()
        if context is None or context.channel != "whatsapp":
            return "Error: polls are only available in WhatsApp chats."
        if not tool_in_group_chat(context) and not context.is_owner:
            return "Error: polls are only available in groups."
        if action == "close":
            if await self._guard.close(context.chat_id, self._clock_ms()):
                return "The open poll in this chat is now closed; a new one may be started."
            return "There is no open poll in this chat."
        if action != "create":
            return f"Unknown action: {action}"

        normalized = normalize_poll(question, options)
        if isinstance(normalized, str):
            return normalized
        text, cleaned = normalized
        send = self._sender()
        if send is None:
            return "Error: polls are unavailable right now."

        client_id = _client_message_id(context.turn_id or context.session_key, context.chat_id,
                                       text, cleaned)
        now_ms = self._clock_ms()
        refusal = await self._guard.reserve(context.chat_id, client_id, now_ms)
        if refusal is not None:
            return refusal
        max_selections = len(cleaned) if multi_select else 1
        try:
            poll_id = await send(context.chat_id, text, cleaned, max_selections, client_id)
        except Exception as exc:  # noqa: BLE001 - any send failure frees the slot
            await self._guard.release(client_id, now_ms)
            logger.warning("poll send failed chat={} error_type={}", context.chat_id,
                           type(exc).__name__)
            return "Error: the poll could not be sent."
        await self._guard.confirm(client_id, poll_id, now_ms)
        return f"Poll sent to this chat: {text} ({len(cleaned)} options)."


def _client_message_id(turn_key: str, chat_id: str, question: str, options: list[str]) -> str:
    """Same turn and same poll give the same id, so a repeated call cannot post twice."""
    digest = hashlib.sha256(
        json.dumps([turn_key, chat_id, question, options], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return "YPOLL" + digest[:27].upper()
