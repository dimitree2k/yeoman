"""Identity normalization for policy matching across channels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Canonical sender identity used by the policy engine."""

    primary: str
    aliases: tuple[str, ...]


def normalize_identity_token(value: str) -> str:
    """Normalize one identity token for matching."""
    token = value.strip()
    if not token:
        return ""
    if token.startswith("@"):
        token = token[1:]
    return token.strip().lower()


def _expand_channel_aliases(channel: str, token: str) -> set[str]:
    """Expand one normalized token into channel-aware aliases."""
    if not token:
        return set()

    aliases = {token}

    if channel == "telegram":
        # Username variants: "@foo" vs "foo".
        if token and not token.isdigit():
            aliases.add(f"@{token}")

    if channel == "whatsapp":
        # JID variants: "123:1@s.whatsapp.net" / "123@s.whatsapp.net" / "123".
        left = token
        right = ""
        if "@" in token:
            left, right = token.split("@", 1)
        left_base = left.split(":", 1)[0]
        aliases.add(left_base)
        if right:
            aliases.add(f"{left_base}@{right}")
        if left_base.startswith("+"):
            aliases.add(left_base[1:])
        elif left_base.isdigit():
            aliases.add(f"+{left_base}")

    return aliases


def normalize_sender_list(channel: str, values: list[str]) -> frozenset[str]:
    """Normalize policy sender list entries."""
    normalized: set[str] = set()
    for value in values:
        token = normalize_identity_token(value)
        normalized.update(_expand_channel_aliases(channel, token))
    return frozenset(normalized)


def _split_sender_id(sender_id: str) -> list[str]:
    return [part.strip() for part in sender_id.split("|") if part.strip()]


def resolve_actor_identity(channel: str, sender_id: str, metadata: dict[str, Any] | None = None) -> ActorIdentity:
    """Resolve sender identity and aliases from channel payload."""
    meta = metadata or {}

    candidates: list[str] = _split_sender_id(str(sender_id))

    # Generic metadata hooks.
    for key in ("user_id", "username", "sender", "pn", "sender_id"):
        value = meta.get(key)
        if value:
            candidates.append(str(value))

    aliases: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        token = normalize_identity_token(candidate)
        if not token:
            continue
        for alias in sorted(_expand_channel_aliases(channel, token)):
            if alias not in seen:
                seen.add(alias)
                aliases.append(alias)

    primary = aliases[0] if aliases else ""
    return ActorIdentity(primary=primary, aliases=tuple(aliases))

