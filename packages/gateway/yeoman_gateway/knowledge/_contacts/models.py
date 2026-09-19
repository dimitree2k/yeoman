"""Domain models for the contacts CRM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Contact:
    id: str
    display_name: str
    phone_number: str | None = None
    is_owner: bool = False
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass(frozen=True, slots=True)
class ContactIdentifier:
    contact_id: str
    channel: str
    identifier: str
    kind: str


@dataclass(frozen=True, slots=True)
class ContactAlias:
    contact_id: str
    alias: str
    source: str
    first_seen: str = field(default_factory=_now_iso)
    last_seen: str = field(default_factory=_now_iso)


@dataclass(frozen=True, slots=True)
class ContactField:
    contact_id: str
    kind: str
    value: str
    label: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
