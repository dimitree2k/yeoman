"""Shared heuristics for messages implicitly addressed to the bot."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

_QUESTION_OR_REQUEST_RE = re.compile(
    r"(?ix)"
    r"\b("
    r"was|wie|warum|wieso|weshalb|wann|wo|wer|wen|wem|welche\w*|"
    r"kannst|kannste|koenntest|könntest|wuerdest|würdest|"
    r"meinst|denkst|findest|haeltst|hältst|"
    r"check(?:st)?|pruef(?:st)?|prüf(?:st)?|such(?:st)?|"
    r"erklaer(?:st)?|erklär(?:st)?|rechne|bewert(?:e|est)?|analysier(?:e|st)?|"
    r"sag\s+mal|hilf"
    r")\b"
)
_FOLLOWUP_FRAGMENT_RE = re.compile(r"(?i)^\s*(und|dann|dazu|hier|bei|zu)\b")
_NEGATIVE_REACTION_RE = re.compile(r"(?i)\b(lol|haha|cringe|wtf|quatsch|lost|fail)\b")
_THINKING_REACTION_RE = re.compile(r"(?i)\b(hm+|hmm+|vielleicht|komisch|spannend)\b")
_REPAIR_FEEDBACK_RE = re.compile(
    r"(?i)\b("
    r"keine gute antwort|schlechte antwort|bad answer|wrong|falsch|stimmt nicht|"
    r"quatsch|bullshit|garbage|macht keinen sinn|nicht gut|fail|json|tool call"
    r")\b"
)
_RESEARCH_REQUEST_RE = re.compile(
    r"(?i)\b(deep\s+search|deep\s+research|recherch|quellen?|sources?|such(?:e|st)?|prüf|pruef)\b"
)


class SessionManagerLike(Protocol):
    def get_or_create(self, key: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ConversationState:
    addressed_to_bot: bool
    address_mode: str
    preferred_action: str
    answer_shape: str
    room_mode: str
    direct_bot_interaction: bool
    name_mentioned: bool = False
    repair_requested: bool = False

    def as_metadata(self) -> dict[str, object]:
        return {
            "addressed_to_bot": self.addressed_to_bot,
            "address_mode": self.address_mode,
            "preferred_action": self.preferred_action,
            "answer_shape": self.answer_shape,
            "room_mode": self.room_mode,
            "direct_bot_interaction": self.direct_bot_interaction,
            "name_mentioned": self.name_mentioned,
            "repair_requested": self.repair_requested,
        }


def contains_bot_name(
    text: str,
    *,
    bot_name_aliases: Sequence[str] = ("arvid",),
) -> bool:
    return any(
        re.search(rf"(?i)(?<![\w@])@?{re.escape(str(alias).strip())}(?!\w)", text)
        for alias in bot_name_aliases
        if str(alias).strip()
    )


def looks_like_question_or_request(text: str) -> bool:
    compact = " ".join(str(text or "").strip().split())
    if not compact:
        return False
    if "?" in compact:
        return True
    return bool(_QUESTION_OR_REQUEST_RE.search(compact))


def looks_like_followup_request(text: str) -> bool:
    compact = " ".join(str(text or "").strip().split())
    if not compact:
        return False
    return looks_like_question_or_request(compact) or bool(_FOLLOWUP_FRAGMENT_RE.search(compact))


def looks_like_repair_feedback(text: str) -> bool:
    compact = " ".join(str(text or "").strip().split())
    return bool(compact and _REPAIR_FEEDBACK_RE.search(compact))


def reaction_for_name_mention(text: str) -> str:
    if _NEGATIVE_REACTION_RE.search(text):
        return "🙄"
    if _THINKING_REACTION_RE.search(text):
        return "🤔"
    return "👀"


def classify_conversation_state(
    *,
    session_manager: SessionManagerLike | None,
    channel: str,
    chat_id: str,
    event_time: datetime,
    content: str,
    metadata: dict[str, Any] | None = None,
    mentioned_bot: bool = False,
    reply_to_bot: bool = False,
    bot_name_aliases: Sequence[str] = ("arvid",),
    followup_window_seconds: float = 10.0,
    repair_window_seconds: float = 120.0,
) -> ConversationState:
    metadata = metadata or {}
    content = str(content or "")
    name_mentioned = contains_bot_name(content, bot_name_aliases=bot_name_aliases)
    explicit_mention = bool(mentioned_bot or metadata.get("mentioned_bot") or metadata.get("mentionedBot"))
    reply_direct = bool(reply_to_bot or metadata.get("reply_to_bot") or metadata.get("replyToBot"))
    from_me = bool(metadata.get("from_me") or metadata.get("fromMe"))
    request_like = looks_like_question_or_request(content)
    recent_followup = is_recent_assistant_followup(
        session_manager=session_manager,
        channel=channel,
        chat_id=chat_id,
        event_time=event_time,
        content=content,
        followup_window_seconds=followup_window_seconds,
    )
    seconds_since_assistant = seconds_since_last_assistant(
        session_manager=session_manager,
        channel=channel,
        chat_id=chat_id,
        event_time=event_time,
    )
    recent_assistant = (
        seconds_since_assistant is not None
        and 0 <= seconds_since_assistant <= max(0.0, float(repair_window_seconds))
    )
    repair_feedback = looks_like_repair_feedback(content) and (
        name_mentioned or reply_direct or recent_assistant
    )

    if from_me:
        address_mode = "from_me"
    elif repair_feedback:
        address_mode = "repair_feedback"
    elif explicit_mention:
        address_mode = "explicit_mention"
    elif reply_direct:
        address_mode = "reply_to_bot"
    elif name_mentioned and request_like:
        address_mode = "plain_name_request"
    elif recent_followup:
        address_mode = "recent_assistant_followup"
    elif name_mentioned:
        address_mode = "name_mention"
    else:
        address_mode = "none"

    if address_mode == "none":
        preferred_action = "silence"
    elif address_mode == "name_mention":
        preferred_action = "react"
    else:
        preferred_action = "answer"

    if address_mode == "repair_feedback":
        answer_shape = "repair"
    elif preferred_action != "answer":
        answer_shape = "none"
    elif _RESEARCH_REQUEST_RE.search(content):
        answer_shape = "researched_answer"
    elif address_mode == "recent_assistant_followup" and len(content.strip()) <= 90:
        answer_shape = "one_liner"
    else:
        answer_shape = "short_take"

    direct = address_mode != "none"
    return ConversationState(
        addressed_to_bot=direct,
        address_mode=address_mode,
        preferred_action=preferred_action,
        answer_shape=answer_shape,
        room_mode="direct_thread" if direct else "ambient",
        direct_bot_interaction=direct,
        name_mentioned=name_mentioned,
        repair_requested=repair_feedback,
    )


def is_direct_bot_interaction(
    *,
    session_manager: SessionManagerLike | None,
    channel: str,
    chat_id: str,
    event_time: datetime,
    content: str,
    metadata: dict[str, Any] | None = None,
    bot_name_aliases: Sequence[str] = ("arvid",),
    followup_window_seconds: float = 10.0,
) -> bool:
    raw_state = (metadata or {}).get("conversation_state") if metadata else None
    if isinstance(raw_state, dict) and bool(raw_state.get("direct_bot_interaction")):
        return True
    return classify_conversation_state(
        session_manager=session_manager,
        channel=channel,
        chat_id=chat_id,
        event_time=event_time,
        content=content,
        metadata=metadata,
        bot_name_aliases=bot_name_aliases,
        followup_window_seconds=followup_window_seconds,
    ).direct_bot_interaction


def is_recent_assistant_followup(
    *,
    session_manager: SessionManagerLike | None,
    channel: str,
    chat_id: str,
    event_time: datetime,
    content: str,
    followup_window_seconds: float = 10.0,
) -> bool:
    if session_manager is None or not looks_like_followup_request(content):
        return False
    delta = seconds_since_last_assistant(
        session_manager=session_manager,
        channel=channel,
        chat_id=chat_id,
        event_time=event_time,
    )
    return delta is not None and 0 <= delta <= max(0.0, float(followup_window_seconds))


def seconds_since_last_assistant(
    *,
    session_manager: SessionManagerLike | None,
    channel: str,
    chat_id: str,
    event_time: datetime,
) -> float | None:
    if session_manager is None:
        return None
    session = session_manager.get_or_create(f"{channel}:{chat_id}")
    messages = getattr(session, "messages", [])
    if not isinstance(messages, list):
        return None
    for row in reversed(messages):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        assistant_ts = parse_timestamp(row.get("timestamp"))
        if assistant_ts is None:
            return None
        return _seconds_between(event_time, assistant_ts)
    return None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _seconds_between(event_time: datetime, assistant_time: datetime) -> float:
    if assistant_time.tzinfo is None:
        event_wall = (
            event_time.astimezone().replace(tzinfo=None)
            if event_time.tzinfo is not None
            else event_time
        )
        return (event_wall - assistant_time).total_seconds()
    event_aware = event_time
    if event_aware.tzinfo is None:
        event_aware = event_aware.replace(tzinfo=UTC)
    return (event_aware.astimezone(UTC) - assistant_time.astimezone(UTC)).total_seconds()
