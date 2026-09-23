"""Varied faces and a burst guard, both from the chat's own reaction history.

Deterministic on purpose: the decider ranks what fits, this module only avoids repeating
itself. It never picks something the decider did not propose (Smart Reply: a fixed
response set plus enforced diversity).
"""

from __future__ import annotations

from collections.abc import Sequence


def choose_varied(candidates: Sequence[str], recent: Sequence[str]) -> str | None:
    """Choose for variety, returning None if every choice would make a triple.

    ``recent`` lists confirmed emojis Arvid used in this chat, newest first. Candidate
    order is the model's fit ranking; the hard three-in-a-row guard is applied first.
    """
    ordered = list(dict.fromkeys(str(item) for item in candidates if str(item or "").strip()))
    if len(recent) >= 2 and recent[0] == recent[1]:
        ordered = [emoji for emoji in ordered if emoji != recent[0]]
    if not ordered:
        return None
    last_use: dict[str, int] = {}
    for index, emoji in enumerate(recent):
        last_use.setdefault(str(emoji), index)
    for emoji in ordered:
        if emoji not in last_use:
            return emoji
    return max(ordered, key=lambda emoji: last_use[emoji])


def cooldown_active(
    times_ms: Sequence[int],
    *,
    now_ms: int,
    count: int,
    window_seconds: int,
    cooldown_seconds: int,
) -> bool:
    """Pure arithmetic only; ProcessingStore owns the atomic check and reservation.

    ``times_ms`` are this chat's reaction timestamps, newest first. A burst is ``count``
    reactions within ``window_seconds``; after it, the chat gets ``cooldown_seconds``
    of reaction silence - the same rule the in-memory bait guard applied, restart-safe.
    """
    if count < 1 or len(times_ms) < count:
        return False
    newest = int(times_ms[0])
    if newest - int(times_ms[count - 1]) > int(window_seconds) * 1000:
        return False
    return int(now_ms) - newest < int(cooldown_seconds) * 1000


__all__ = ["choose_varied", "cooldown_active"]
