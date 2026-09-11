"""The ambient verdict: should Arvid answer, just react, or stay out of it?

The brake in the fast gate decides *when* the question is even asked (a minimum spacing and
a minimum amount of new chatter, skipped when the message names him). This module answers
it, and it is deliberately strict: "a new topic appeared" is not a reason to speak.

One small model call returns all three outcomes - a real answer, a single reaction from the
owner's vocabulary, or silence - so a message that deserves a 👍 never pays for a persona
prompt, and a message that deserves nothing costs one cheap call at most.

Fail-closed everywhere: a failure, a timeout, an unparsed answer, a hesitant verdict or an
emoji the owner did not approve all mean silence.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from yeoman_shared.reactions import allowed_reaction

from yeoman_gateway.processing.model_route import RouteClient

#: Terse on purpose. The judge must not become a second opinion about the conversation.
AMBIENT_JUDGE_PROMPT = (
    "Du entscheidest, wie ein reservierter Gesprächsteilnehmer (Arvid) auf eine Nachricht "
    "in einer Gruppe reagiert, die nicht direkt an ihn gerichtet war. Sein Standard ist "
    "Schweigen.\n"
    "Antworte NUR mit JSON: "
    '{"action": "answer"|"react"|"none", "emoji": "<emoji>", "confidence": 0.0-1.0}\n'
    "action=answer nur, wenn mindestens eines zutrifft:\n"
    "- eine offene Sachfrage an die Runde, die er erkennbar besser beantworten kann;\n"
    "- ein folgenreicher sachlicher Fehler, der korrigiert werden muss;\n"
    "- eine ausdrückliche Einladung an die Runde, mitzureden.\n"
    "action=react, wenn kein Inhalt nötig ist, aber eine kurze Geste passt: eine an ihn "
    "gerichtete Bemerkung, ein Dank, ein Witz, eine knappe Zustimmung oder ein Angebot. "
    'Dann "emoji" auf genau ein Emoji aus der erlaubten Liste setzen.\n'
    "action=none bei: neuem Thema ohne Frage, Meinungen, Smalltalk unter anderen, "
    "Statusmeldungen, Fragen an eine bestimmte andere Person, bloßem Interesse.\n"
    "Ein neues Thema ist kein Grund zu antworten. Eine Reaktion ist kein Ersatz für eine "
    "Antwort, die inhaltlich nötig wäre."
)

#: A judge never needs more than the message itself.
MAX_MESSAGE_CHARS = 1200

#: What the judge may decide.
AmbientAction = Literal["answer", "react", "silence"]


@dataclass(frozen=True, slots=True)
class AmbientVerdict:
    """One verdict: answer it, react to it, or leave it alone."""

    action: AmbientAction
    emoji: str | None = None
    confidence: float = 0.0

    @property
    def speaks(self) -> bool:
        return self.action != "silence"

    @property
    def needs_turn(self) -> bool:
        return self.action == "answer"


SILENCE = AmbientVerdict(action="silence")


class AmbientJudge:
    """One strict verdict per unaddressed message, at most one per brake window."""

    def __init__(
        self,
        *,
        client: RouteClient,
        allowed_emojis: Sequence[str] = (),
        min_confidence: float = 0.75,
        timeout_seconds: float = 12.0,
    ) -> None:
        self._client = client
        self._allowed_emojis = tuple(
            str(item).strip() for item in allowed_emojis if str(item).strip()
        )
        self._min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    async def decide(self, text: str, *, context: str = "") -> AmbientVerdict:
        """The verdict for one message. Every failure path returns silence."""
        message = " ".join(str(text or "").split())[:MAX_MESSAGE_CHARS]
        if not message:
            return SILENCE
        prompt = message if not context else f"{context}\n\nNeueste Nachricht:\n{message}"
        vocabulary = " ".join(self._allowed_emojis) or "-"
        messages = [
            {
                "role": "system",
                "content": f"{AMBIENT_JUDGE_PROMPT}\nErlaubte Emojis: {vocabulary}",
            },
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
            return SILENCE
        verdict = self._parse(raw)
        decided = verdict if verdict.confidence >= self._min_confidence else SILENCE
        logger.info(
            "ambient_judge action={} emoji={} confidence={:.2f} threshold={:.2f} decided={}",
            verdict.action,
            verdict.emoji or "-",
            verdict.confidence,
            self._min_confidence,
            decided.action,
        )
        return decided

    def _parse(self, raw: str) -> AmbientVerdict:
        payload = _parse_payload(raw)
        if payload is None:
            logger.debug("ambient_judge_unparsed route={}", self._client.route_key)
            return SILENCE
        action = _normalize_action(payload.get("action"))
        if action is None:
            logger.debug("ambient_judge_unknown_action route={}", self._client.route_key)
            return SILENCE
        confidence = _as_confidence(payload.get("confidence"))
        if action == "react":
            emoji = allowed_reaction(payload.get("emoji") or "", self._allowed_emojis)
            if emoji is None:
                # A reaction with an emoji the owner did not approve is not a reaction.
                logger.debug("ambient_judge_emoji_rejected route={}", self._client.route_key)
                return AmbientVerdict(action="silence", confidence=confidence)
            return AmbientVerdict(action="react", emoji=emoji, confidence=confidence)
        return AmbientVerdict(action=action, confidence=confidence)


def _normalize_action(value: Any) -> AmbientAction | None:
    text = str(value or "").strip().lower()
    if text in {"answer", "antwort", "reply"}:
        return "answer"
    if text in {"react", "reaction", "reagieren"}:
        return "react"
    if text in {"none", "silence", "silent", "no", "false", "schweigen"}:
        return "silence"
    return None


def _as_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _parse_payload(raw: str) -> dict[str, Any] | None:
    """Read the JSON object out of a small model answer."""
    text = str(raw or "").strip()
    if not text:
        return None
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


__all__ = ["AMBIENT_JUDGE_PROMPT", "AmbientJudge", "AmbientVerdict", "SILENCE"]
