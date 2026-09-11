"""The ambient verdict: may Arvid join a conversation he was not addressed in?

The brake in the fast gate decides *when* the question is even asked (a minimum spacing and
a minimum amount of new chatter). This module answers it, and it is deliberately strict:
"a new topic appeared" is not a reason to speak. Only a message that clearly needs him -
an open factual question he can answer better, a consequential error, or an explicit
invitation to the room - earns a yes, and only with enough confidence.

The verdict is one small model call: a strict prompt, a JSON answer, no history, no tools.
A failure, a timeout or a hesitant answer all mean silence.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from yeoman_gateway.processing.model_route import RouteClient

#: Terse on purpose. The judge must not become a second opinion about the conversation.
AMBIENT_JUDGE_PROMPT = (
    "Du entscheidest, ob ein reservierter Gesprächsteilnehmer (Arvid) sich in einer "
    "Gruppe ungefragt einschalten soll. Sein Standard ist Schweigen.\n"
    "Antworte NUR mit JSON: {\"answer\": true|false, \"confidence\": 0.0-1.0}\n"
    "answer=true nur, wenn mindestens eines zutrifft:\n"
    "- eine offene Sachfrage an die Runde, die er erkennbar besser beantworten kann;\n"
    "- ein folgenreicher sachlicher Fehler, der korrigiert werden muss;\n"
    "- eine ausdrückliche Einladung an die Runde, mitzureden.\n"
    "answer=false bei: neuem Thema ohne Frage, Meinungen, Smalltalk, Witzen, "
    "Statusmeldungen, Fragen an eine bestimmte andere Person, bloßem Interesse.\n"
    "Ein neues Thema ist kein Grund zu antworten."
)

#: A judge never needs more than the message itself.
MAX_MESSAGE_CHARS = 1200


class AmbientJudge:
    """A strict yes/no verdict about joining an unaddressed conversation."""

    def __init__(
        self,
        *,
        client: RouteClient,
        min_confidence: float = 0.75,
        timeout_seconds: float = 12.0,
    ) -> None:
        self._client = client
        self._min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    async def should_answer(self, text: str, *, context: str = "") -> bool:
        """True only for a confident, clearly earned yes."""
        message = " ".join(str(text or "").split())[:MAX_MESSAGE_CHARS]
        if not message:
            return False
        prompt = message if not context else f"{context}\n\nNeueste Nachricht:\n{message}"
        messages = [
            {"role": "system", "content": AMBIENT_JUDGE_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            raw = await asyncio.wait_for(
                self._client.chat(messages, max_tokens=48),
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "ambient_judge_failed route={} error_type={}",
                self._client.route_key,
                type(exc).__name__,
            )
            return False
        verdict = _parse_verdict(raw)
        if verdict is None:
            logger.debug("ambient_judge_unparsed route={}", self._client.route_key)
            return False
        answer, confidence = verdict
        decided = bool(answer) and confidence >= self._min_confidence
        logger.info(
            "ambient_judge answer={} confidence={:.2f} threshold={:.2f} decided={}",
            answer,
            confidence,
            self._min_confidence,
            decided,
        )
        return decided


def _parse_verdict(raw: str) -> tuple[bool, float] | None:
    """Read ``{"answer": bool, "confidence": float}`` out of a small model answer."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        payload: Any = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    answer = payload.get("answer")
    if isinstance(answer, str):
        answer = answer.strip().lower() in {"true", "yes", "ja"}
    if not isinstance(answer, bool):
        return None
    confidence = payload.get("confidence", 1.0)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    return answer, max(0.0, min(1.0, confidence_value))


__all__ = ["AMBIENT_JUDGE_PROMPT", "AmbientJudge"]
