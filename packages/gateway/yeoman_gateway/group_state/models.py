"""The per-chat group-state document and its sections.

The types here are deliberately plain: ids, counts, timestamps and display names.
No message text, no model output and no instructions live in a group-state document.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

SCHEMA_VERSION = 2
MAX_SOURCES = 500

Trust = Literal["runtime", "derived"]
Layer = Literal["live", "conversation", "day", "week"]


def _jsonable(value: Any) -> Any:
    """Turn tuples and mappings into JSON containers without changing meaning."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Item:
    """One derived piece of content and the history messages it depends on."""

    id: str
    kind: str
    data: Mapping[str, Any]
    sources: tuple[str, ...]
    source_fingerprints: Mapping[str, str]
    produced_at_ms: int
    ttl_ms: int
    trust: Trust
    conversation_id: str | None = None
    person_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources)[:MAX_SOURCES])
        object.__setattr__(self, "source_fingerprints", dict(self.source_fingerprints))
        object.__setattr__(self, "data", _jsonable(dict(self.data)))

    def expired(self, now_ms: int) -> bool:
        return int(now_ms) - int(self.produced_at_ms) > int(self.ttl_ms)

    def to_json(self) -> str:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "data": self.data,
            "sources": self.sources,
            "source_fingerprints": self.source_fingerprints,
            "produced_at_ms": self.produced_at_ms,
            "ttl_ms": self.ttl_ms,
            "trust": self.trust,
            "conversation_id": self.conversation_id,
            "person_id": self.person_id,
        }
        return json.dumps(_jsonable(payload), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | Mapping[str, Any]) -> "Item":
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            id=str(payload["id"]),
            kind=str(payload["kind"]),
            data=dict(payload.get("data") or {}),
            sources=tuple(str(source) for source in payload.get("sources") or ()),
            source_fingerprints={
                str(source): str(fingerprint)
                for source, fingerprint in (payload.get("source_fingerprints") or {}).items()
            },
            produced_at_ms=int(payload["produced_at_ms"]),
            ttl_ms=int(payload["ttl_ms"]),
            trust=payload["trust"],
            conversation_id=payload.get("conversation_id"),
            person_id=payload.get("person_id"),
        )


@dataclass(frozen=True, slots=True)
class Section:
    """One deterministic slice of a chat's state, rebuilt from bounded sources."""

    key: str
    schema_version: int
    trust: Trust
    layer: Layer
    produced_at_ms: int
    ttl_ms: int
    source_cursor: str
    period_start_ms: int | None
    period_end_ms: int | None
    #: History message ids the data depends on, capped at :data:`MAX_SOURCES`.
    sources: tuple[str, ...]
    data: Mapping[str, Any]
    #: Derived content carried by this section; omitted from JSON when empty.
    items: tuple[Item, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources)[:MAX_SOURCES])
        object.__setattr__(self, "data", _jsonable(dict(self.data)))
        object.__setattr__(self, "items", tuple(self.items))

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "key": self.key,
            "schema_version": self.schema_version,
            "trust": self.trust,
            "layer": self.layer,
            "produced_at_ms": self.produced_at_ms,
            "ttl_ms": self.ttl_ms,
            "source_cursor": self.source_cursor,
            "period_start_ms": self.period_start_ms,
            "period_end_ms": self.period_end_ms,
            "sources": self.sources,
            "data": self.data,
        }
        if self.items:
            payload["items"] = [json.loads(item.to_json()) for item in self.items]
        return json.dumps(_jsonable(payload), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | Mapping[str, Any]) -> "Section":
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            key=str(payload["key"]),
            schema_version=int(payload["schema_version"]),
            trust=payload["trust"],
            layer=payload["layer"],
            produced_at_ms=int(payload["produced_at_ms"]),
            ttl_ms=int(payload["ttl_ms"]),
            source_cursor=str(payload.get("source_cursor") or ""),
            period_start_ms=payload.get("period_start_ms"),
            period_end_ms=payload.get("period_end_ms"),
            sources=tuple(str(item) for item in payload.get("sources") or ()),
            data=dict(payload.get("data") or {}),
            items=tuple(Item.from_json(item) for item in payload.get("items") or ()),
        )


@dataclass(frozen=True, slots=True)
class GroupState:
    """The whole state of one chat at one revision; never spans two chats."""

    channel: str
    chat_id: str
    schema_version: int
    revision: int
    history_cursor: str
    updated_ms: int
    sections: Mapping[str, Section]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", dict(self.sections))

    def age_ms(self, now_ms: int) -> int:
        return max(0, int(now_ms) - int(self.updated_ms))

    def required_age_ms(self, now_ms: int, required: Collection[str]) -> int | None:
        """Age of the oldest required section, never of the document write."""
        produced = [
            int(section.produced_at_ms)
            for key in required
            if (section := self.sections.get(key)) is not None
        ]
        return max(0, int(now_ms) - min(produced)) if produced else None

    def to_json(self) -> str:
        payload = {
            "channel": self.channel,
            "chat_id": self.chat_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "history_cursor": self.history_cursor,
            "updated_ms": self.updated_ms,
            "sections": {
                key: json.loads(section.to_json()) for key, section in self.sections.items()
            },
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | Mapping[str, Any]) -> "GroupState":
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            channel=str(payload["channel"]),
            chat_id=str(payload["chat_id"]),
            schema_version=int(payload["schema_version"]),
            revision=int(payload["revision"]),
            history_cursor=str(payload.get("history_cursor") or ""),
            updated_ms=int(payload["updated_ms"]),
            sections={
                str(key): Section.from_json(value)
                for key, value in (payload.get("sections") or {}).items()
            },
        )
