"""Proven chat membership adapter shared by live History capture."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from yeoman_gateway.policy.identity import registry_member_principals


def registry_members(chat_registry: Any, *, channel: str, chat_id: str) -> set[str]:
    """Return participants only when the registry has a proven participant list."""
    from yeoman_gateway.knowledge._history_identity import current_history_scope

    scope = current_history_scope()
    if scope is not None and channel == "whatsapp":
        import time

        audience = scope.queries.members(chat_id=chat_id, at_ms=int(time.time() * 1000))
        return set(audience.members) if audience.status == "known" else set()

    get_chat = getattr(chat_registry, "get_chat", None)
    if get_chat is None:
        return set()
    try:
        record = get_chat(channel, chat_id)
    except Exception:  # pragma: no cover - defensive
        return set()
    if not isinstance(record, Mapping):
        return set()
    metadata = record.get("metadata")
    participants = metadata.get("participants") if isinstance(metadata, Mapping) else None
    return set(registry_member_principals(channel, participants))
