"""Language-neutral signals for a short reply to the bot (spec 2026-09-22 §6.1).

Nothing here knows a word of any language. Lengths are grapheme clusters, questions are
punctuation of several scripts, emojis are a Unicode property. The only fixed strings are
the protocol markers our own bridge and enrichment write into the text. What a reply
*means* is left to the small decider.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import regex

#: Question punctuation: Latin, fullwidth, inverted, Arabic, Greek, Armenian, Ethiopic,
#: and the double/interrobang forms.
QUESTION_MARKS: frozenset[str] = frozenset(
    "?\uff1f\u00bf\u061f\u037e\u055e\u1367\u2047\u2048\u2049"
)
#: Placeholders the bridge writes when a media message has no caption.
_BRIDGE_PLACEHOLDERS: dict[str, str] = {
    "[Image]": "image",
    "[Video]": "video",
    "[Document]": "document",
    "[Voice Message]": "audio",
    "[Sticker]": "sticker",
}
#: Markers the gateway's media enrichment appends to the text.
_ENRICHMENT_SPLIT = regex.compile(r"\s*\[(?:image|video|sticker)_description\]\s*")
_GRAPHEME = regex.compile(r"\X")
_EMOJI = regex.compile(r"[\p{Extended_Pictographic}\p{Regional_Indicator}]")
_EMOJI_COMPONENTS = regex.compile(r"[\p{Emoji_Modifier}\u200d]+")
MEDIA_TEXT_MAX_GRAPHEMES = 200


def _compact(text: object) -> str:
    return " ".join(str(text or "").split())


def grapheme_count(text: str) -> int:
    return len(_GRAPHEME.findall(_compact(text)))


def truncate_graphemes(text: str, limit: int) -> str:
    clusters = _GRAPHEME.findall(_compact(text))
    return "".join(clusters[: max(0, int(limit))])


def has_question_punct(text: str) -> bool:
    return any(character in QUESTION_MARKS for character in str(text or ""))


def emojis_in(text: str) -> tuple[str, ...]:
    return tuple(
        cluster for cluster in _GRAPHEME.findall(str(text or "")) if _EMOJI.search(cluster)
    )


def is_emoji_only(text: str) -> bool:
    clusters = [c for c in _GRAPHEME.findall(_compact(text)) if not c.isspace()]
    return bool(clusters) and all(
        _EMOJI.search(cluster) or _EMOJI_COMPONENTS.fullmatch(cluster) for cluster in clusters
    )


@dataclass(frozen=True, slots=True)
class ShortReplySignals:
    """What trusted code may know about a reply without understanding its language."""

    text: str
    media_text: str
    media_kind: str
    direct_reply: bool
    from_me: bool
    has_media: bool
    graphemes: int
    has_question_punct: bool
    emojis: tuple[str, ...]
    emoji_only: bool
    bot_asked: bool

    def is_candidate(self, *, max_chars: int) -> bool:
        if not self.direct_reply or self.from_me or self.has_question_punct:
            return False
        if self.has_media and self.media_kind == "sticker":
            return bool(self.media_text)
        if self.has_media and not self.media_text:
            return False
        return 1 <= self.graphemes <= int(max_chars)

    def log_fields(self) -> dict[str, object]:
        return {
            "graphemes": self.graphemes,
            "emoji_only": self.emoji_only,
            "has_media": self.has_media,
            "bot_asked": self.bot_asked,
            "question": self.has_question_punct,
        }


def compute_signals(
    *,
    content: str,
    reply_to_bot: bool,
    reply_to_text: str | None,
    metadata: Mapping[str, Any] | None,
) -> ShortReplySignals:
    metadata = metadata or {}
    media_kind = str(metadata.get("media_kind") or metadata.get("mediaKind") or "").strip()
    parts = _ENRICHMENT_SPLIT.split(str(content or ""), maxsplit=1)
    text = _compact(parts[0])
    enrichment = _compact(parts[1]) if len(parts) > 1 else ""
    placeholder_kind = _BRIDGE_PLACEHOLDERS.get(text)
    if placeholder_kind is not None:
        text = ""
        media_kind = media_kind or placeholder_kind
    if media_kind == "audio":
        text = ""
    transcript = _compact(metadata.get("voice_transcript"))
    description = enrichment or _compact(metadata.get("media_description"))
    if media_kind == "audio":
        media_text = truncate_graphemes(transcript, MEDIA_TEXT_MAX_GRAPHEMES)
    elif media_kind == "sticker":
        media_text = truncate_graphemes(description or "sticker", MEDIA_TEXT_MAX_GRAPHEMES)
    else:
        media_text = truncate_graphemes(description, MEDIA_TEXT_MAX_GRAPHEMES)
    has_media = bool(media_kind)
    measured_text = transcript if media_kind == "audio" else text
    return ShortReplySignals(
        text=text,
        media_text=media_text,
        media_kind=media_kind,
        direct_reply=bool(reply_to_bot),
        from_me=bool(metadata.get("from_me") or metadata.get("fromMe")),
        has_media=has_media,
        graphemes=grapheme_count(measured_text),
        has_question_punct=has_question_punct(measured_text),
        emojis=emojis_in(measured_text),
        emoji_only=is_emoji_only(measured_text),
        bot_asked=has_question_punct(str(reply_to_text or "")),
    )


__all__ = [
    "MEDIA_TEXT_MAX_GRAPHEMES",
    "QUESTION_MARKS",
    "ShortReplySignals",
    "compute_signals",
    "emojis_in",
    "grapheme_count",
    "has_question_punct",
    "is_emoji_only",
    "truncate_graphemes",
]
