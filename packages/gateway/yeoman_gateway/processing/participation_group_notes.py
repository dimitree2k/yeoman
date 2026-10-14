"""Read path of the weekly group reaction digest (Spec B section 5.5, step B3).

A group note is style guidance about one group, derived from what its participants
reacted to and engaged with. It is never evidence and never about a person. This module
answers one question for the participation context builder: which note, if any, may
this decision see?

* **Same chat only.** The note of chat X is read by an exact ``(channel, chat_id)``
  scope; it is never offered to another chat, a direct chat or a recall result.
* **Fresh and intact only.** Only the chat's active digest counts. It renders when it
  is at most :data:`MAX_NOTE_AGE_MS` old and none of the messages shown to its model
  call was deleted or revoked; a deleted source marks it stale before this read (the
  rule B2 stores), and a stale digest renders nothing.
* **Advisory.** A failure to read a note is logged and the decision goes on without
  one.

``processing.participation.groupNotes`` picks what the caller does with the note:
``off`` reads nothing, ``on`` renders it to the Judge and Writer, ``shadow`` keeps live
decisions unchanged and gives the note to a second, effect-free Judge call only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from yeoman_gateway.history.live import HistoryPaused

#: Spec B sections 5.3/5.5: a note stays usable until it is older than 14 days.
MAX_NOTE_AGE_MS = 14 * 86_400_000
GROUP_NOTES_MODES: tuple[str, ...] = ("off", "shadow", "on")
#: Private context key of a note held for the shadow Judge only; never rendered live.
SHADOW_NOTE_KEY = "_group_notes_shadow"


@dataclass(frozen=True, slots=True)
class GroupNote:
    """One renderable weekly note of one chat."""

    digest_id: str
    chat_id: str
    week: str
    text: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "digest_id": self.digest_id,
            "chat_id": self.chat_id,
            "week": self.week,
            "text": self.text,
        }


def render_group_notes_block(week: object, text: object) -> str:
    """The one fixed group-notes block, or ``""`` without a note."""
    body = str(text or "").strip()
    if not body:
        return ""
    return (
        f"[Group notes, week {str(week or '').strip() or '?'} · style guidance from "
        f"reactions, never fact]\n{body}"
    )


def is_group_chat(channel: str, chat_id: str) -> bool:
    """Weekly notes exist for WhatsApp groups only; a direct chat never gets one."""
    return str(channel) == "whatsapp" and str(chat_id).endswith("@g.us")


class GroupNotesReader:
    """Latest non-stale, same-chat note of at most 14 days, for one decision."""

    def __init__(
        self,
        *,
        knowledge: Any,
        mode: Callable[[], str],
        max_age_ms: int = MAX_NOTE_AGE_MS,
    ) -> None:
        self._knowledge = knowledge
        self._mode = mode
        self._max_age_ms = max(0, int(max_age_ms))

    def mode(self) -> str:
        try:
            value = str(self._mode() or "off")
        except Exception:  # noqa: BLE001 - an unreadable setting renders nothing
            return "off"
        return value if value in GROUP_NOTES_MODES else "off"

    def note_for(self, channel: str, chat_id: str, *, now_ms: int) -> GroupNote | None:
        """The note this chat's decision may see, or ``None``.

        Requires the caller's history turn: without one the deleted-source check cannot
        run, so nothing is rendered.
        """
        if self._knowledge is None or not is_group_chat(channel, chat_id):
            return None
        from yeoman_gateway.history.context import current_history_snapshot
        from yeoman_gateway.history.queries import HistoryQueries

        snapshot = current_history_snapshot()
        if snapshot is None:
            return None
        queries = HistoryQueries(snapshot)
        try:
            # Newest first; the store keeps exactly one active digest per chat, and it
            # is staleness-checked (and marked) before it is returned.
            digests = self._knowledge.group_digests(
                chat_id=str(chat_id),
                source_present=lambda message_id: queries.message(message_id) is not None,
                limit=1,
            )
        except HistoryPaused:
            return None
        except Exception as exc:  # noqa: BLE001 - group notes are advisory
            logger.warning("group_notes_read_failed error_type={}", type(exc).__name__)
            return None
        for digest in digests:
            if (
                str(digest.status) != "active"
                or str(digest.chat_id) != str(chat_id)
                # The Knowledge scope of a digest is its exact channel and chat.
                or str(digest.scope_key) != f"channel:{channel}:chat:{chat_id}"
            ):
                return None
            text = str(digest.rendered or "").strip()
            if not text:
                return None
            if int(digest.created_ms) > int(now_ms):
                return None  # not yet written at this decision's time
            if int(now_ms) - int(digest.window_end_ms) > self._max_age_ms:
                return None
            return GroupNote(
                digest_id=str(digest.digest_id),
                chat_id=str(chat_id),
                week=str(digest.week),
                text=text,
            )
        return None


__all__ = [
    "GROUP_NOTES_MODES",
    "MAX_NOTE_AGE_MS",
    "SHADOW_NOTE_KEY",
    "GroupNote",
    "GroupNotesReader",
    "is_group_chat",
    "render_group_notes_block",
]
