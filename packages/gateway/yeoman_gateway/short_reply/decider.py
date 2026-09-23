"""One small multilingual decision for a short reply to the bot (spec 2026-09-22 §6.2).

The model reads meaning and tone in any language and returns JSON: react (with ranked,
approved emojis), answer, or none. Trusted code validates everything; self-reported
confidence is logged, never used as permission. Every failure is an ``error`` verdict the
caller maps to its configured fallback - cancellation is the only exception.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from yeoman_shared.reactions import allowed_reaction

from yeoman_gateway.short_reply.signals import truncate_graphemes

SHORT_REPLY_PROMPT = (
    "You decide how Arvid responds to a short message that replies to him in a group "
    "chat. The message and Arvid's earlier text may be in any language; judge meaning "
    "and tone, not wording. Reply ONLY with JSON: "
    '{{"action":"react"|"answer"|"none","emojis":["…"],"confidence":0.0-1.0}}.\n'
    "react: an acknowledgement, thanks, agreement, joke, emotion or small talk that "
    'needs no content. Put 1-3 emojis from the allowed list in "emojis", best fit '
    "first, matching the sender's tone (mirror their emotion where it fits).\n"
    "answer: the message adds a new claim, correction, request or question that "
    "deserves a real reply.\n"
    "none: nothing Arvid could add, not even a gesture.\n"
    "Emojis Arvid used recently in this chat: {recent}. Prefer a different emoji when "
    "several fit equally well; never pick one only for variety.\n"
    "Allowed emojis: {vocabulary}"
)
MAX_TEXT_GRAPHEMES = 400

VerdictAction = Literal["react", "answer", "none", "error"]
_ACTIONS: dict[str, VerdictAction] = {
    "react": "react",
    "reaction": "react",
    "reagieren": "react",
    "answer": "answer",
    "antwort": "answer",
    "reply": "answer",
    "comment": "answer",
    "none": "none",
    "silence": "none",
    "silent": "none",
    "no": "none",
    "false": "none",
    "schweigen": "none",
}


@dataclass(frozen=True, slots=True)
class ShortReplyInput:
    text: str
    bot_text: str = ""
    media_text: str = ""
    recent_emojis: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShortReplyVerdict:
    action: VerdictAction
    emojis: tuple[str, ...] = ()
    confidence: float = 0.0
    error: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0


def _error(code: str, reply: Any | None = None) -> ShortReplyVerdict:
    return ShortReplyVerdict(
        action="error",
        error=code,
        usage=dict(getattr(reply, "usage", {}) or {}),
        model=str(getattr(reply, "model", "") or ""),
        latency_ms=int(getattr(reply, "latency_ms", 0) or 0),
    )


def _json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


class ShortReplyDecider:
    """One bounded provider request: react, answer, or none, without retries."""

    def __init__(
        self,
        *,
        client: Any,
        allowed_emojis: Sequence[str],
        max_candidates: int = 3,
        timeout_seconds: float = 6.0,
        max_output_tokens: int = 48,
    ) -> None:
        self._client = client
        self._vocabulary = tuple(str(item).strip() for item in allowed_emojis if str(item).strip())
        self._max_candidates = max(1, int(max_candidates))
        self._timeout_seconds = max(0.001, float(timeout_seconds))
        self._max_output_tokens = max(16, int(max_output_tokens))

    async def decide(self, request: ShortReplyInput) -> ShortReplyVerdict:
        if not self._vocabulary:
            return _error("no_vocabulary")
        text = truncate_graphemes(request.text, MAX_TEXT_GRAPHEMES)
        media = truncate_graphemes(request.media_text, MAX_TEXT_GRAPHEMES)
        if not text and not media:
            return _error("empty_input")
        bot_text = truncate_graphemes(request.bot_text, MAX_TEXT_GRAPHEMES) or "-"
        reply_block = text + (f"\n[media: {media}]" if media else "")
        recent = " ".join(request.recent_emojis) or "-"
        messages = [
            {
                "role": "system",
                "content": SHORT_REPLY_PROMPT.format(
                    recent=recent, vocabulary=" ".join(self._vocabulary)
                ),
            },
            {
                "role": "user",
                "content": f"Arvid wrote:\n{bot_text}\n\nReply to Arvid:\n{reply_block}",
            },
        ]
        try:
            reply = await asyncio.wait_for(
                self._client.chat_with_usage(
                    messages,
                    max_tokens=self._max_output_tokens,
                    response_format={"type": "json_object"},
                    max_retries=0,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return _error("timeout")
        except Exception:  # noqa: BLE001 - any provider failure is one error verdict
            return _error("provider_error")

        if reply.finish_reason == "error":
            return _error("provider_error", reply)
        if reply.finish_reason == "length":
            return _error("truncated_output", reply)
        payload = _json_object(reply.content)
        if payload is None:
            return _error("invalid_json", reply)
        action = _ACTIONS.get(str(payload.get("action") or "").strip().lower())
        if action is None:
            return _error("invalid_action", reply)
        raw_emojis = payload.get("emojis")
        if not isinstance(raw_emojis, list):
            raw_emojis = [payload.get("emoji")] if payload.get("emoji") else []
        emojis: list[str] = []
        for raw in raw_emojis:
            chosen = allowed_reaction(str(raw or ""), self._vocabulary)
            if chosen and chosen not in emojis:
                emojis.append(chosen)
        emojis = emojis[: self._max_candidates]
        if action == "react" and not emojis:
            return _error("no_valid_emoji", reply)
        return ShortReplyVerdict(
            action=action,
            emojis=tuple(emojis),
            confidence=_confidence(payload.get("confidence")),
            usage=dict(reply.usage),
            model=reply.model,
            latency_ms=reply.latency_ms,
        )


__all__ = [
    "SHORT_REPLY_PROMPT",
    "ShortReplyDecider",
    "ShortReplyInput",
    "ShortReplyVerdict",
]
