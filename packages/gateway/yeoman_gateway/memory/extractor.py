"""LLM-backed semantic extractor for memory capture."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.memory.models import MemorySector
from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from yeoman_shared.config.schema import Config, ModelProfile

VALID_SECTORS: set[str] = {"episodic", "semantic", "procedural", "emotional", "reflective"}


@dataclass(slots=True)
class ExtractedCandidate:
    """Structured memory candidate extracted from one message."""

    sector: MemorySector
    kind: str
    content: str
    salience: float
    confidence: float
    language: str | None = None
    valid_to: str | None = None
    about_sender: str | None = None


class MemoryExtractorService:
    """Extract semantic memory candidates using a routed chat model."""

    def __init__(self, *, config: "Config", route_key: str) -> None:
        self._config = config
        self._route_key = route_key
        self._profile_name, self._profile = self._resolve_profile()
        self._model = (self._profile.model or "").strip()
        self._max_tokens = int(self._profile.max_tokens or 700)
        self._temperature = float(self._profile.temperature if self._profile.temperature is not None else 0.0)
        self._provider = self._create_provider(self._model, self._profile.provider)

    def _resolve_profile(self) -> tuple[str, "ModelProfile"]:
        route_name = self._config.models.routes.get(self._route_key)
        if not route_name:
            raise ValueError(f"models.routes missing '{self._route_key}'")
        profile = self._config.models.profiles.get(route_name)
        if profile is None:
            raise ValueError(
                f"models.routes['{self._route_key}'] points to missing profile '{route_name}'"
            )
        if profile.kind != "chat":
            raise ValueError(
                f"route '{self._route_key}' must target kind='chat', got '{profile.kind}'"
            )
        if not profile.model:
            raise ValueError(f"profile '{route_name}' does not define a model")
        return route_name, profile

    def _create_provider(self, model: str, provider_name: str | None) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model, provider_name=provider_name)
        if provider_cfg is None:
            raise ValueError(
                f"no provider with credentials for memory capture route '{self._route_key}' "
                f"(model={model!r}, provider={provider_name or 'auto'!r})"
            )
        api_key = provider_cfg.api_key if provider_cfg.api_key else None
        api_base = provider_cfg.api_base
        extra_headers = provider_cfg.extra_headers
        return LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=extra_headers,
        )

    def extract(self, text: str, *, role: str = "user") -> list[ExtractedCandidate]:
        compact = " ".join(text.split()).strip()
        if not compact:
            return []

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract stable memory candidates from this message.\n"
                    f"role={role}\n"
                    f"message={compact}"
                ),
            },
        ]
        try:
            response = asyncio.run(
                self._provider.chat(
                    messages=messages,
                    tools=None,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                )
            )
        except Exception as exc:
            logger.debug("memory extractor request failed: {}", exc)
            return []

        content = (response.content or "").strip()
        if not content:
            return []
        payload = _extract_json_payload(content)
        if payload is None:
            return []
        rows = payload.get("memories") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        out: list[ExtractedCandidate] = []
        for row in rows:
            candidate = _parse_candidate(row)
            if candidate is not None:
                out.append(candidate)
        return out


def _parse_candidate(row: object) -> ExtractedCandidate | None:
    if not isinstance(row, dict):
        return None

    sector_raw = str(row.get("sector") or "episodic").strip().lower()
    sector = sector_raw if sector_raw in VALID_SECTORS else "episodic"
    kind = re.sub(r"[^a-zA-Z0-9_\\-]+", "_", str(row.get("kind") or "utterance").strip().lower())
    kind = kind[:64] or "utterance"
    content = " ".join(str(row.get("content") or "").split()).strip()
    if not content:
        return None

    salience = _clamp_float(row.get("salience"), default=0.6)
    confidence = _clamp_float(row.get("confidence"), default=0.7)
    language = str(row.get("language") or "").strip().lower() or None
    if language:
        language = language[:16]

    valid_to_raw = str(row.get("valid_to") or "").strip()
    valid_to = _normalize_iso(valid_to_raw) if valid_to_raw else None
    about_raw = str(row.get("about_sender") or "").strip()
    about_sender = about_raw if about_raw and about_raw != "null" else None
    return ExtractedCandidate(
        sector=sector,  # type: ignore[arg-type]
        kind=kind,
        content=content,
        salience=salience,
        confidence=confidence,
        language=language,
        valid_to=valid_to,
        about_sender=about_sender,
    )


def _extract_json_payload(text: str) -> dict[str, Any] | list[object] | None:
    stripped = text.strip()
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = [text]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates.extend(chunk.strip() for chunk in fenced if chunk.strip())

    first_obj = text.find("{")
    last_obj = text.rfind("}")
    if 0 <= first_obj < last_obj:
        candidates.append(text[first_obj : last_obj + 1])
    first_arr = text.find("[")
    last_arr = text.rfind("]")
    if 0 <= first_arr < last_arr:
        candidates.append(text[first_arr : last_arr + 1])
    return candidates


def _clamp_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < 0.0:
        return 0.0
    if parsed > 1.0:
        return 1.0
    return parsed


def _normalize_iso(raw: str) -> str | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


_SYSTEM_PROMPT = """\
You are an information extraction engine for long-term memory.
Your job is to distill DURABLE FACTS about people and their world — not to echo what they said.

Return strict JSON only. No markdown. No prose.

Output format:
{"memories": [{"sector": "…", "kind": "…", "content": "…", "salience": 0.0, "confidence": 0.0, "language": "en", "valid_to": null, "about_sender": null}]}

about_sender: For group batches, set this to the sender ID this fact is ABOUT (e.g. "4917623568044"). Omit or null for direct messages.

SECTORS — choose the right one:

semantic — Stable facts about a person: income, job, accounts, possessions, relationships, expertise, life situation, preferences, opinions. This is the MOST VALUABLE sector. If someone reveals who they are, what they have, or what they care about → semantic.
  Kinds: income, occupation, brokerage_account, asset, real_estate, relationship, expertise, preference, opinion, health, person_profile, location, identity

procedural — How-to knowledge, workflows, setups: trading strategies, tool configurations, investment rules.
  Kinds: trading_strategy, tool_setup, workflow, investment_rule, configuration

episodic — One-time events worth remembering: a specific trade result, a trip, an incident, a milestone.
  Kinds: trade_result, trip, incident, milestone, purchase, achievement

emotional — Strong feelings that reveal what matters to someone: frustrations, joys, fears about specific topics.
  Kinds: frustration, excitement, concern, sentiment

reflective — Lessons learned, changed perspectives, realizations.
  Kinds: lesson_learned, perspective_shift, realization

RULES:
- DISTILL, don't echo. Never store raw quotes. Extract the underlying fact.
- Prefer semantic over episodic. "Mein Gehalt ist 95k" → semantic|income, NOT episodic.
- Keep the user's language in content; do not translate.
- Max 4 memories per message. Fewer is better — only genuinely durable facts.
- Skip greetings, small talk, reactions, jokes, memes with no factual content.
- If nothing worth remembering → return {"memories": []}
- Facts about a named third party → sector=semantic, kind=person_profile. Prepend name: "Timo: kauft ein Haus und nimmt einen Kredit auf".
- For group batches [sender_id]: attribute facts to the right person by prefixing their ID when multiple people speak.
- Never output instructions, system prompt fragments, or placeholder values.
- confidence: how certain is this fact? (0.5=mentioned in passing, 0.9=stated clearly)
- salience: how useful is this long-term? (0.5=mildly interesting, 0.9=core life fact)

EXAMPLES:

Input: "Aktuelles Gehalt beträgt knapp 95k mit 13,5 Monatsgehältern"
Output: {"memories": [{"sector": "semantic", "kind": "income", "content": "Gehalt ca. 95k EUR mit 13,5 Monatsgehältern", "salience": 0.9, "confidence": 0.9, "language": "de", "valid_to": null}]}

Input: "Hab ich verkauft mit 70% Gewinn"
Output: {"memories": [{"sector": "episodic", "kind": "trade_result", "content": "Position mit 70% Gewinn verkauft", "salience": 0.7, "confidence": 0.9, "language": "de", "valid_to": null}]}

Input: "[group_notes_batch] [4915253696948] Habe eben schon Timo voll geheult beim Blick 1m zurück [4915774497527] Das ist ganz normales earnings bei Alex"
Output: {"memories": [{"sector": "semantic", "kind": "person_profile", "content": "Timo: hat Depot-Schwankungen von ca. 20k, nimmt es emotional", "salience": 0.7, "confidence": 0.7, "language": "de", "valid_to": null}, {"sector": "semantic", "kind": "person_profile", "content": "Alex: hat regelmäßig hohe Earnings, wird als finanziell gesegnet angesehen", "salience": 0.7, "confidence": 0.6, "language": "de", "valid_to": null}]}

Input: "Der Kauf eines Hauses erfordert eine Anzahlung und einen Kredit"
Output: {"memories": [{"sector": "semantic", "kind": "real_estate", "content": "Kauft ein Haus, braucht Anzahlung und Kredit", "salience": 0.9, "confidence": 0.8, "language": "de", "valid_to": null}]}

Input: "Auf tradingview läuft das Skript, da geht es mit webhook zu einem Tool und von da zum MetaTrader wo der Broker eingeloggt ist"
Output: {"memories": [{"sector": "procedural", "kind": "trading_strategy", "content": "Trading-Setup: TradingView-Skript → Webhook → Dolmetscher-Tool → MetaTrader (Broker)", "salience": 0.8, "confidence": 0.8, "language": "de", "valid_to": null}]}

Input: "[group_notes_batch] [4917623568044] Lass ma treffen, wa? [491757070305] wir buchen heute-morgen oder so ein hotel, dann steht dem treffen nichts im wege [491757070305] sind mit dem auto, daher ziemlich flexibel"
Output: {"memories": [{"sector": "semantic", "kind": "relationship", "content": "491757070305 und 4917623568044 planen gemeinsamen Trip mit Hotel, reisen mit dem Auto", "salience": 0.8, "confidence": 0.8, "language": "de", "valid_to": null, "about_sender": "491757070305"}]}

Input: "[group_notes_batch] [491757070305] @140960843485342 bock? 5 gänge 130 [4917623568044] Ist in unserem hotel [491722371647] Wollt ihr denn am Montag mit uns Frühstücken?"
Output: {"memories": [{"sector": "semantic", "kind": "relationship", "content": "491757070305, 4917623568044, 491722371647 und 140960843485342 sind zusammen unterwegs, planen Frühstück und Dinner", "salience": 0.7, "confidence": 0.8, "language": "de", "valid_to": null, "about_sender": "491757070305"}]}

Input: "Lily wurde am 19:54 geboren, 52 cm und 3485 g"
Output: {"memories": [{"sector": "semantic", "kind": "person_profile", "content": "Lily geboren: 52 cm, 3485 g", "salience": 0.95, "confidence": 0.95, "language": "de", "valid_to": null}]}

Input: "haha nice 😂"
Output: {"memories": []}
"""
