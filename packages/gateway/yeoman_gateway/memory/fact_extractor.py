"""Model-backed extraction of shared-fact candidates (Plan 05, Aufgabe 3/5).

The model only *proposes*: it returns statements with a basis label, and everything that
decides whether a proposal becomes a stored fact happens in deterministic code
(``check_candidate``). Two rules are enforced here rather than trusted to the prompt:

* Only user messages from the journal are source text. Assistant output, summaries and
  persona text never become a shared fact.
* The audience comes from the *proven* participant list of the chat, never from the model.
  Without a proven list the fact falls back to ``author_only``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from loguru import logger

from yeoman_gateway.memory.extraction_jobs import (
    ACCEPTED_BASES,
    REJECT_BASES,
    SharedFactCandidate,
)
from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_shared.config.schema import Config, ModelProfile

#: Hard cap on candidates per job. Kept in code: a chatty turn must not multiply rows.
MAX_CANDIDATES_PER_JOB = 4

FACT_SYSTEM_PROMPT = """You extract durable, shareable facts from chat messages.

Return JSON only: {"facts": [{"content": str, "basis": str, "valid_until": str|null}]}

Rules:
- Only statements the author explicitly asserts about the world, plans, preferences,
  dates or agreements. Never opinions, speculation, guesses or inferences.
- Never record the conversation itself: not who explained, asked, said or discussed
  something, and never mention the assistant, the bot, this chat or a chat transcript.
- Never write about "the author", "the user" or "the questioner" in the third person.
  State the fact directly, or return nothing for that message.
- Never store hedged or possible statements ("würde", "könnte", "vielleicht",
  "eventuell"). If it is not asserted plainly, it is not a fact.
- Skip jokes, banter and rhetorical remarks, however concrete they sound.
- Never restate that a message was sent, delivered or read.
- Never invent facts about a person who is not the author.
- content: one short sentence in the language of the message.
- basis: exactly one of "explicit_statement", "opinion", "speculation", "inference",
  "person_speculation", "delivery_claim". Use "explicit_statement" only when the author
  states the fact directly.
- valid_until: ISO date when the fact is explicitly time-bound, else null.
- State the fact itself, never that someone said something. Phrases like
  "Der Autor sagt:" or "the message says" are forbidden - either state the fact or return
  nothing for that message.
- Never quote the message back.
- Return an empty list when nothing qualifies. Never pad the list."""


@dataclass(frozen=True, slots=True)
class _EventView:
    """The source fields an extraction is allowed to see."""

    event_id: str
    principal: str
    text: str
    channel: str
    chat_id: str
    revision: int
    occurred_ms: int | None
    is_group: bool
    is_user_message: bool


def event_view(event: object, *, revision: int = 1) -> _EventView:
    payload = getattr(event, "payload", None)
    body: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    role = str(body.get("role") or "user").lower()
    return _EventView(
        event_id=str(getattr(event, "event_id", "") or ""),
        principal=str(getattr(event, "principal", "") or ""),
        text=str(body.get("text") or "").strip(),
        channel=str(getattr(event, "channel", "") or ""),
        chat_id=str(getattr(event, "chat_id", "") or ""),
        revision=int(revision),
        occurred_ms=getattr(event, "occurred_ms", None),
        is_group=bool(body.get("is_group")),
        is_user_message=str(getattr(event, "kind", "message")) == "message"
        and role not in ("assistant", "system", "bot"),
    )


class SharedFactExtractor:
    """Turns journal events of one turn into shared-fact candidates."""

    def __init__(
        self,
        *,
        config: "Config",
        route_key: str,
        member_provider: Callable[[str, str], frozenset[str] | None] | None = None,
        max_candidates: int = MAX_CANDIDATES_PER_JOB,
    ) -> None:
        self._config = config
        self._route_key = str(route_key)
        self._member_provider = member_provider
        self._max_candidates = max(1, int(max_candidates))
        self._profile_name, self._profile = self._resolve_profile()
        self._model = str(self._profile.model or "").strip()
        self._max_tokens = int(self._profile.max_tokens or 700)
        self._temperature = float(
            self._profile.temperature if self._profile.temperature is not None else 0.0
        )
        self._provider = self._create_provider(self._model, self._profile.provider)

    # -- construction -----------------------------------------------------------

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
            raise ValueError(f"route '{self._route_key}' must target kind='chat'")
        if not profile.model:
            raise ValueError(f"profile '{route_name}' does not define a model")
        return route_name, profile

    def _create_provider(self, model: str, provider_name: str | None) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model, provider_name=provider_name)
        if provider_cfg is None:
            raise ValueError(
                f"no provider with credentials for shared-fact route '{self._route_key}'"
            )
        return LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg.api_key else None,
            api_base=provider_cfg.api_base,
            default_model=model,
            extra_headers=provider_cfg.extra_headers,
        )

    # -- extraction -------------------------------------------------------------

    def __call__(self, events: Iterable[object]) -> list[SharedFactCandidate]:
        views = [event_view(event) for event in events]
        sources = [view for view in views if view.is_user_message and view.text]
        if not sources:
            logger.debug("shared fact extraction skipped: no user message in turn")
            return []

        author = sources[0].principal
        if not author:
            logger.debug("shared fact extraction skipped: source has no principal")
            return []

        text = "\n".join(view.text for view in sources)[:4000]
        rows = self._ask_model(text)
        if not rows:
            return []

        audience, group = self._resolve_audience(sources[0])
        candidates: list[SharedFactCandidate] = []
        for row in rows[: self._max_candidates]:
            candidate = self._to_candidate(
                row,
                author=author,
                sources=sources,
                audience=audience,
                is_group=group,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _ask_model(self, text: str) -> list[Mapping[str, Any]]:
        messages = [
            {"role": "system", "content": FACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Messages:\n{text}"},
        ]
        try:
            # The extractor runs on its own thread, so a nested event loop is safe here.
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
            logger.warning("shared fact extractor request failed: {}", exc)
            raise
        content = str(response.content or "").strip()
        if not content:
            return []
        payload = _extract_json(content)
        if payload is None:
            logger.warning("shared fact extractor returned unparseable content")
            return []
        rows = payload.get("facts") if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, Mapping)]

    def _resolve_audience(self, source: _EventView) -> tuple[frozenset[str], bool]:
        """Proven participants of the chat, or an empty set when nothing is proven."""
        members: frozenset[str] | None = None
        if self._member_provider is not None and source.channel and source.chat_id:
            try:
                members = self._member_provider(source.channel, source.chat_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("shared fact audience lookup failed: {}", exc)
                members = None
        if not members:
            return frozenset(), source.is_group
        return frozenset(str(item) for item in members if item), source.is_group

    def _to_candidate(
        self,
        row: Mapping[str, Any],
        *,
        author: str,
        sources: Sequence[_EventView],
        audience: frozenset[str],
        is_group: bool,
    ) -> SharedFactCandidate | None:
        content = str(row.get("content") or "").strip()
        if not content:
            return None
        basis = str(row.get("basis") or "uncertain").strip()
        if basis not in ACCEPTED_BASES and basis not in REJECT_BASES:
            basis = "uncertain"
        scope = (
            "chat_shared"
            if is_group and audience
            else ("principals" if audience else "author_only")
        )
        # The reader must be able to see their own fact; a chat_shared fact without a
        # proven audience would be invisible, so it degrades to author_only instead.
        allowed_audience = audience | {author}
        valid_until = _parse_iso_ms(row.get("valid_until"))
        return SharedFactCandidate(
            content=content,
            author_principal=author,
            source_role="user",
            basis=basis,
            visibility_scope=scope,
            temporal_basis="absolute",
            valid_until_ms=valid_until,
            source_refs=tuple((view.event_id, view.revision) for view in sources),
            source_scopes=tuple(f"{view.channel}:{view.chat_id}" for view in sources),
            audience=frozenset(allowed_audience),
        )


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    fenced = stripped
    if stripped.startswith("```"):
        parts = stripped.split("```")
        if len(parts) >= 2:
            fenced = parts[1]
            if fenced.lstrip().lower().startswith("json"):
                fenced = fenced.lstrip()[4:]
    try:
        return json.loads(fenced.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_iso_ms(raw: object) -> int | None:
    from datetime import UTC, datetime

    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)
