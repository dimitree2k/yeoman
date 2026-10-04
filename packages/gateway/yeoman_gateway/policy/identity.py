"""Identity normalization for policy matching across channels."""

from __future__ import annotations

from collections.abc import Mapping
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


def normalize_sender_list(
    channel: str,
    values: list[str],
    owner_senders: frozenset[str] | None = None,
) -> frozenset[str]:
    """Normalize policy sender list entries.

    The special token ``$owner`` expands to all owner identifiers for the
    channel when *owner_senders* is provided.
    """
    normalized: set[str] = set()
    for value in values:
        if value.strip().lower() == "$owner" and owner_senders is not None:
            normalized.update(owner_senders)
            continue
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


def canonical_user_id(
    channel: str, sender_id: str = "", metadata: dict[str, Any] | None = None
) -> str:
    """Return a quota identity from trusted WhatsApp bridge metadata only.

    A bare sender/LID is deliberately not a fallback: generic IPC and model-provided
    values must never turn into a person identity or an owner exception.
    """
    del sender_id
    if channel != "whatsapp":
        return ""
    meta = metadata or {}
    if any(bool(meta.get(key)) for key in ("lid_conflict", "lidConflict", "identity_conflict")):
        return ""
    raw = normalize_identity_token(str(meta.get("sender_phone_jid") or ""))
    if not raw:
        return ""
    token = raw.split("@", 1)[0].split(":", 1)[0].lstrip("+")
    return f"whatsapp:{token}" if token.isdigit() else ""


def registry_member_principals(channel: str, raw_members: Any) -> frozenset[str]:
    """Project a registry roster without guessing from untyped identifiers.

    WhatsApp bridge participants can carry a LID in ``id`` plus a typed phone proof
    in ``phoneNumber`` or ``phoneJid``. Only those dedicated phone fields may produce
    a qualified phone principal. Missing or conflicting proof keeps the native member
    identifier; an unidentifiable record makes the whole roster unknown.
    """
    if raw_members is None:
        return frozenset()
    if isinstance(raw_members, Mapping):
        raw_members = raw_members.get("participants") or raw_members.get("members") or []
    if isinstance(raw_members, str):
        raw_members = (raw_members,)

    members: set[str] = set()
    for item in raw_members or ():
        projected = _registry_member_values(channel, item)
        if not projected:
            # A partial roster is not proof of a complete audience.
            return frozenset()
        members.update(projected)
    return frozenset(member for member in members if member)


def _registry_member_values(channel: str, item: Any) -> tuple[str, ...]:
    if isinstance(item, str):
        return (item.strip(),) if item.strip() else ()

    if isinstance(item, Mapping):
        explicit_principal = str(item.get("principal_id") or "").strip()
        if explicit_principal:
            return (explicit_principal,)

        native_id = next(
            (
                str(item.get(attribute) or "").strip()
                for attribute in ("id", "jid", "lid", "user_id")
                if str(item.get(attribute) or "").strip()
            ),
            "",
        )
        qualified_id = next(
            (
                str(item.get(attribute) or "").strip()
                for attribute in ("id", "jid", "lid", "user_id")
                if str(item.get(attribute) or "").strip().startswith(f"{channel}:")
            ),
            "",
        )
        if qualified_id:
            return (qualified_id,)

        phone_fields = (
            str(item.get(attribute) or "").strip()
            for attribute in ("phoneNumber", "phoneJid", "phone_jid")
            if str(item.get(attribute) or "").strip()
        )
        phone_values = tuple(phone_fields)
        if channel == "whatsapp" and phone_values:
            principals = tuple(_native_phone_principal(value) for value in phone_values)
            if all(principals) and len(set(principals)) == 1:
                typed_native_id = _native_phone_principal(native_id)
                if typed_native_id and typed_native_id != principals[0]:
                    return (native_id,) if native_id else phone_values
                return (principals[0],)
            # Conflicting or malformed phone proofs cannot rewrite the native ID.
            return (native_id,) if native_id else phone_values
        return (native_id,) if native_id else ()

    for attribute in ("principal_id", "id", "jid", "lid", "user_id"):
        value = str(getattr(item, attribute, "") or "").strip()
        if value:
            return (value,)
    return ()


def _native_phone_principal(value: str) -> str | None:
    token = str(value or "").strip()
    if not token.endswith(("@s.whatsapp.net", "@c.us")):
        return None
    return canonical_user_id("whatsapp", metadata={"sender_phone_jid": token}) or None
