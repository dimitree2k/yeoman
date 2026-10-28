"""Autonomous memes and stickers for the participation path.

The judge may pick ``meme`` or ``sticker`` next to silence/react/comment when the
chat opted in (``participation.autoMemes``). This module owns the parts that are
not judging: the trusted offer the judge sees (templates, described chat photos,
budgets) and the render of a validated plan into one image file.

A chat photo is offered only from the same chat and only when it already has a vision
description, so the judge knows what it shows; without one it is never offered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

MemeSource = Literal["template", "photo", "background"]

MAX_PHOTO_CANDIDATES = 3
MAX_DESCRIPTION_CHARS = 160
MAX_PROMPT_CHARS = 300

_DESCRIPTION_MARKERS = ("[Derived description]:", "[image_description]")


def _description(row: Mapping[str, Any]) -> str:
    summary = str(row.get("media_summary") or "")
    for marker in _DESCRIPTION_MARKERS:
        summary = summary.replace(marker, " ")
    return " ".join(summary.split())


@dataclass(frozen=True, slots=True)
class MemePlan:
    """The judge's validated meme: what to render, never where to send it."""

    source: MemeSource
    captions: tuple[str, ...]
    template: str | None = None
    photo_id: str | None = None
    prompt: str | None = None

    def log_fields(self) -> str:
        what = self.template or self.photo_id or (self.prompt or "")[:60]
        return f"source={self.source} what={what!r} captions={list(self.captions)!r}"


@dataclass(frozen=True, slots=True)
class MemeOffer:
    """What the judge may choose from for one opportunity."""

    templates: tuple[tuple[str, str, int], ...]
    photos: tuple[tuple[str, str], ...]
    backgrounds: bool

    def context_fields(self) -> dict[str, object]:
        return {
            "meme_templates": [item[0] for item in self.templates],
            "meme_template_text": "\n".join(
                f"{template_id}: {name} ({boxes} captions)"
                for template_id, name, boxes in self.templates
            ),
            "meme_photos": [{"id": photo_id, "description": text} for photo_id, text in self.photos],
            "meme_backgrounds": self.backgrounds,
        }


class ParticipationMemes:
    """Adapter from the participation runtime to :class:`MemeService`."""

    def __init__(self, *, service: Any, media_cache: Any | None = None) -> None:
        self._service = service
        self._media_cache = media_cache

    def offer(
        self, *, channel: str, chat_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> MemeOffer:
        templates = tuple(
            (template.id, template.name, len(template.boxes))
            for template in self._service.templates.values()
        )
        return MemeOffer(
            templates=templates,
            photos=self._photo_candidates(channel, chat_id, messages),
            backgrounds=bool(self._service.backgrounds_available),
        )

    def _photo_candidates(
        self, channel: str, chat_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> tuple[tuple[str, str], ...]:
        if self._media_cache is None:
            return ()
        found: list[tuple[str, str]] = []
        for row in reversed(list(messages)):
            if len(found) >= MAX_PHOTO_CANDIDATES:
                break
            if str(row.get("chat_id") or chat_id) != chat_id:
                continue
            message_id = str(row.get("event_id") or row.get("message_id") or "").strip()
            description = _description(row)
            if not message_id or not description:
                continue
            if self._photo_path(channel, chat_id, message_id) is None:
                continue
            found.append((message_id, description[:MAX_DESCRIPTION_CHARS]))
        return tuple(reversed(found))

    def _photo_path(self, channel: str, chat_id: str, message_id: str) -> Path | None:
        try:
            item = self._media_cache.lookup_by_message(channel, chat_id, message_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable cache offers no photo
            logger.warning("participation_meme_photo_lookup_failed error_type={}", type(exc).__name__)
            return None
        if item is None or item.kind != "image" or not Path(item.local_path).is_file():
            return None
        return Path(item.local_path)

    async def render(
        self, plan: MemePlan, *, channel: str, chat_id: str, sticker: bool
    ) -> Path:
        """Render one plan. Raises on any failure; nothing is sent from here."""
        captions = list(plan.captions)
        if plan.source == "template":
            return await self._service.from_template(
                str(plan.template), captions, sticker=sticker
            )
        if plan.source == "photo":
            path = self._photo_path(channel, chat_id, str(plan.photo_id or ""))
            if path is None:
                raise LookupError("photo_unavailable")
            return await self._service.from_photo(path, captions, sticker=sticker)
        return await self._service.from_background(
            str(plan.prompt or ""), captions, channel, sticker=sticker
        )


__all__ = [
    "MAX_PROMPT_CHARS",
    "MemeOffer",
    "MemePlan",
    "ParticipationMemes",
]
