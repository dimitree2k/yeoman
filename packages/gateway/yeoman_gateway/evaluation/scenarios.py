"""Scenario data model and loader for files with a shared ``cast`` and scenario list."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KNOWN_FEATURES = frozenset(
    {"awareness", "direct_judge", "writer", "guidance_checks", "scoring", "owner_lookup", "identity_linking"}
)
FEATURES_AVAILABLE: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Person:
    key: str
    name: str
    sender_id: str


@dataclass(frozen=True, slots=True)
class Chat:
    key: str
    chat_id: str
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    chat: str
    sender: str
    at_ms: int
    text: str
    reply_to: str = ""


@dataclass(frozen=True, slots=True)
class Trigger:
    chat: str
    kind: str
    source_ids: tuple[str, ...]
    direct: bool = False


@dataclass(frozen=True, slots=True)
class Expectation:
    actions: tuple[str, ...] = ()
    forbidden_emojis: tuple[str, ...] = ()
    private_terms: tuple[str, ...] = ()
    needed_evidence: tuple[str, ...] = ()
    abstain: bool = False


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    category: str
    modes: tuple[str, ...]
    requires: tuple[str, ...]
    now_ms: int
    people: Mapping[str, Person]
    chats: Mapping[str, Chat]
    messages: tuple[Message, ...]
    trigger: Trigger
    expected: Expectation
    notes: str = ""


def parse_time(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def scenario_from_dict(data: Mapping[str, Any], cast: Mapping[str, Any]) -> Scenario:
    people = {
        key: Person(key, str(person["name"]), str(person["sender_id"]))
        for key, person in dict(cast.get("people", {})).items()
    }
    chats = {
        key: Chat(key, str(chat["chat_id"]), tuple(chat.get("members", ())))
        for key, chat in dict(cast.get("chats", {})).items()
    }
    sid = str(data["id"])
    requires = tuple(str(item) for item in data.get("requires", ()))
    unknown = set(requires) - KNOWN_FEATURES
    if unknown:
        raise ValueError(f"scenario {sid}: unknown feature(s) {sorted(unknown)}")
    messages: list[Message] = []
    for row in data.get("messages", ()):
        mid, chat, sender, at, text, *rest = row
        if chat not in chats:
            raise ValueError(f"scenario {sid}: unknown chat {chat!r}")
        if sender not in people:
            raise ValueError(f"scenario {sid}: unknown sender {sender!r}")
        messages.append(Message(str(mid), str(chat), str(sender), parse_time(str(at)), str(text),
                                str(rest[0]) if rest else ""))
    trigger_data = data["trigger"]
    if trigger_data["chat"] not in chats:
        raise ValueError(f"scenario {sid}: unknown trigger chat {trigger_data['chat']!r}")
    known_ids = {message.id for message in messages}
    trigger = Trigger(
        chat=str(trigger_data["chat"]), kind=str(trigger_data.get("kind", "inbound")),
        source_ids=tuple(str(item) for item in trigger_data["source_ids"]),
        direct=bool(trigger_data.get("direct", False)),
    )
    if not set(trigger.source_ids) <= known_ids:
        raise ValueError(f"scenario {sid}: trigger source not among messages")
    expected_data = dict(data.get("expected", {}))
    expected = Expectation(
        actions=tuple(expected_data.get("actions", ())),
        forbidden_emojis=tuple(expected_data.get("forbidden_emojis", ())),
        private_terms=tuple(expected_data.get("private_terms", ())),
        needed_evidence=tuple(expected_data.get("needed_evidence", ())),
        abstain=bool(expected_data.get("abstain", False)),
    )
    if not set(expected.needed_evidence) <= known_ids:
        raise ValueError(f"scenario {sid}: needed evidence not among messages")
    return Scenario(
        id=sid, title=str(data.get("title", "")), category=str(data.get("category", "")),
        modes=tuple(data.get("modes", ())), requires=requires, now_ms=parse_time(str(data["now"])),
        people=people, chats=chats, messages=tuple(messages), trigger=trigger, expected=expected,
        notes=str(data.get("notes", "")),
    )


def load_scenario_file(path: Path) -> list[Scenario]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cast = data.get("cast", {})
    return [scenario_from_dict(item, cast) for item in data.get("scenarios", ())]
