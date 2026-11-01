"""The per-chat group-state document and its sections.

The types here are deliberately plain: ids, counts, timestamps and display names.
No message text, no model output and no instructions live in a group-state document.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

SCHEMA_VERSION = 1
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources)[:MAX_SOURCES])
        object.__setattr__(self, "data", _jsonable(dict(self.data)))

    def to_json(self) -> str:
        payload = {
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
