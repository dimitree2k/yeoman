"""The emojis a reaction may carry, and where the choice came from.

A reaction is the cheapest thing Arvid can send and the loudest per character: one wrong
face in a group reads as sarcasm nobody asked for. So the vocabulary is not the model's to
invent - the owner keeps one list of approved emojis, the model picks from it, and an
emoji outside the list is dropped instead of guessed.

Only a *model-chosen* emoji is subject to that list. Confirmations the gateway decides for
itself (a blocked input, a name mention, a saved idea) are code decisions and carry
``origin="system"``; they are not a matter of taste and do not change when the list does.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

#: Where the emoji came from: the language model ("model") or the gateway itself
#: ("system"). Only the former is checked against the owner's list.
ReactionOrigin = Literal["model", "system"]

MODEL_ORIGIN: ReactionOrigin = "model"
SYSTEM_ORIGIN: ReactionOrigin = "system"

#: The approved emojis out of the box. Every one of them is already in use: the first ten
#: are the reactions the runtime personas prescribe, the rest are the obvious neighbours
#: the model reaches for. Editing this list is the supported way to change Arvid's
#: reaction vocabulary - it deliberately lives in configuration, never in a prompt.
DEFAULT_REACTION_EMOJIS: tuple[str, ...] = (
    "👍",  # plain acknowledgement
    "🤙",  # thanks / appreciation
    "🙏",  # thanks, warmer
    "😄",  # genuine amusement
    "😂",  # laughter
    "😏",  # smirk
    "😎",  # composure, a compliment landed
    "😌",  # satisfied, settled
    "🤔",  # thinking, unclear
    "🤷",  # no idea, not my call
    "🥱",  # bait, not worth an answer
    "💀",  # that was brutal, or hilarious
    "🔥",  # strong approval
    "👀",  # paying attention, watching
)

#: A reaction is one emoji - possibly with a variation selector or skin-tone modifier -
#: never a phrase, and never a word that merely names a face ("thumbsup").
_MAX_EMOJI_CHARS = 8


def looks_like_emoji(value: object) -> bool:
    """True when the value is shaped like an emoji rather than a word or a sentence."""
    text = str(value or "").strip()
    if not text or len(text) > _MAX_EMOJI_CHARS:
        return False
    return not any(character.isalnum() or character.isspace() for character in text)


def allowed_reaction(value: object, allowed: Iterable[str]) -> str | None:
    """The emoji to send, or ``None`` when the owner has not approved this one.

    Fail-closed by design: an unapproved, empty or word-shaped value yields ``None`` so the
    caller can drop it. It is never replaced by a default face, and never sent as text.
    """
    text = str(value or "").strip()
    if not looks_like_emoji(text):
        return None
    approved = {str(item).strip() for item in allowed}
    return text if text in approved else None


__all__ = [
    "DEFAULT_REACTION_EMOJIS",
    "MODEL_ORIGIN",
    "SYSTEM_ORIGIN",
    "ReactionOrigin",
    "allowed_reaction",
    "looks_like_emoji",
]
