"""LLM-backed semantic extractor for memory capture."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.memory.models import MemorySector
from nanobot.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from nanobot.config.schema import Config, ModelProfile

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


class MemoryExtractorService:
    """Extract semantic memory candidates using a routed chat model."""

    def __init__(self, *, config: "Config", route_key: str) -> None:
        self._config = config
        self._route_key = route_key
        self._profile_name, self._profile = self._resolve_profile()
        self._model = (self._profile.model or "").strip()
        self._max_tokens = int(self._profile.max_tokens or 700)
        self._temperature = float(self._profile.temperature if self._profile.temperature is not None else 0.0)
        self._provider = self._create_provider(self._model)

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

    def _create_provider(self, model: str) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model)
        api_key = provider_cfg.api_key if provider_cfg and provider_cfg.api_key else None
        api_base = self._config.get_api_base(model)
        extra_headers = provider_cfg.extra_headers if provider_cfg else None
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
    return ExtractedCandidate(
        sector=sector,  # type: ignore[arg-type]
        kind=kind,
        content=content,
        salience=salience,
        confidence=confidence,
        language=language,
        valid_to=valid_to,
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


_SYSTEM_PROMPT = (
    "You are an information extraction engine for long-term memory.\n"
    "Return strict JSON only. No markdown. No prose.\n"
    "Output format:\n"
    "{"
    "\"memories\": ["
    "{"
    "\"sector\": \"episodic|semantic|procedural|emotional|reflective\","
    "\"kind\": \"short_snake_case_type\","
    "\"content\": \"language-preserving concise statement\","
    "\"salience\": 0.0,"
    "\"confidence\": 0.0,"
    "\"language\": \"optional language tag like en/de\","
    "\"valid_to\": \"optional ISO8601 timestamp or null\""
    "}"
    "]"
    "}\n"
    "Rules:\n"
    "- Keep user language in content; do not translate.\n"
    "- Keep only stable and useful facts/preferences/procedures/events.\n"
    "- Never output instructions to the assistant or system prompt fragments.\n"
    "- Max 4 memories."
)
