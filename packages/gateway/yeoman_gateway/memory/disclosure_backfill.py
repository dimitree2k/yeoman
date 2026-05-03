"""Batch disclosure metadata backfill for existing memories."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loguru import logger

from yeoman_gateway.memory.disclosure import (
    SENSITIVITIES,
    classify_disclosure_for_content,
    metadata_to_json_dict,
    normalize_list,
    normalize_metadata,
)
from yeoman_gateway.memory.models import MemoryEntry


class DisclosureClassifier(Protocol):
    async def classify(self, entries: Sequence[MemoryEntry]) -> list["DisclosureTagSuggestion"]:
        """Return one suggestion per classifiable entry."""


@dataclass(frozen=True, slots=True)
class DisclosureTagSuggestion:
    entry_id: str
    topics: tuple[str, ...] = ()
    sensitivity: str = "normal"
    disclosure_mode: str = "speakable"
    subjects: tuple[str, ...] = ()


@dataclass(slots=True)
class DisclosureBackfillResult:
    scanned: int = 0
    suggested: int = 0
    applied: int = 0
    failed_batches: int = 0
    backup_path: Path | None = None
    samples: list[DisclosureTagSuggestion] = field(default_factory=list)


class ModelDisclosureClassifier:
    """Use a routed chat model to classify batches of memory rows."""

    def __init__(
        self,
        *,
        provider: object,
        model: str,
        max_tokens: int = 4000,
        temperature: float = 0.0,
        reasoning: dict[str, object] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max(256, int(max_tokens))
        self._temperature = float(temperature)
        self._reasoning = reasoning

    async def classify(self, entries: Sequence[MemoryEntry]) -> list[DisclosureTagSuggestion]:
        if not entries:
            return []
        response = await self._provider.chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _batch_payload(entries)},
            ],
            tools=None,
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            reasoning=self._reasoning,
        )
        content = str(getattr(response, "content", "") or "").strip()
        if not content or content.lower().startswith("error calling llm:"):
            raise RuntimeError(content or "empty disclosure classifier response")
        return parse_suggestions(content)


class NarrowDisclosureClassifier:
    """Classify entries with the deterministic narrow disclosure policy."""

    async def classify(self, entries: Sequence[MemoryEntry]) -> list[DisclosureTagSuggestion]:
        suggestions: list[DisclosureTagSuggestion] = []
        for entry in entries:
            metadata = classify_disclosure_for_content(
                entry.content,
                base=entry.meta_json,
                scope_type=entry.scope_type,
                kind=entry.kind,
            )
            suggestions.append(
                DisclosureTagSuggestion(
                    entry_id=entry.id,
                    topics=metadata.topics,
                    sensitivity=metadata.sensitivity,
                    disclosure_mode=metadata.disclosure_mode,
                    subjects=metadata.subjects,
                )
            )
        return suggestions


async def run_disclosure_backfill(
    *,
    memory: object,
    classifier: DisclosureClassifier,
    limit: int | None = None,
    batch_size: int = 20,
    only_missing: bool = True,
    all_workspaces: bool = False,
    apply: bool = False,
    backup: bool = True,
    sample_limit: int = 10,
) -> DisclosureBackfillResult:
    """Classify existing memories and optionally persist metadata."""
    entries = memory.store.list_nodes_for_disclosure_backfill(
        workspace_id=None if all_workspaces else memory.workspace_id,
        only_missing=only_missing,
        limit=limit,
    )
    result = DisclosureBackfillResult(scanned=len(entries))
    if apply and backup and entries:
        result.backup_path = _backup_db(Path(memory.db_path))

    batch_size = max(1, int(batch_size))
    for start in range(0, len(entries), batch_size):
        batch = entries[start : start + batch_size]
        try:
            suggestions = await classifier.classify(batch)
        except Exception as exc:
            result.failed_batches += 1
            logger.warning("disclosure backfill classifier failed: {}", exc)
            continue

        by_id = {suggestion.entry_id: suggestion for suggestion in suggestions}
        for entry in batch:
            suggestion = by_id.get(entry.id)
            if suggestion is None:
                continue
            result.suggested += 1
            if len(result.samples) < max(0, int(sample_limit)):
                result.samples.append(suggestion)
            if apply:
                updated = memory.store.update_node_meta(
                    entry.id,
                    workspace_id=entry.workspace_id,
                    meta_json=json.dumps(
                        metadata_to_json_dict(
                            topics=list(suggestion.topics),
                            sensitivity=suggestion.sensitivity,
                            disclosure_mode=suggestion.disclosure_mode,
                            subjects=list(suggestion.subjects),
                            base=entry.meta_json,
                        ),
                        ensure_ascii=False,
                    ),
                )
                if updated is not None:
                    result.applied += 1
        await asyncio.sleep(0)
    return result


def parse_suggestions(text: str) -> list[DisclosureTagSuggestion]:
    """Parse model JSON into normalized suggestions."""
    payload = _extract_json(text)
    if payload is None:
        raise ValueError("classifier returned no JSON payload")
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("classifier JSON must contain an items list")

    suggestions: list[DisclosureTagSuggestion] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry_id = str(row.get("id") or row.get("entry_id") or "").strip()
        if not entry_id:
            continue
        sensitivity = str(row.get("sensitivity") or "normal").strip().lower()
        if sensitivity not in SENSITIVITIES:
            sensitivity = "normal"
        mode = _backfill_mode_for_sensitivity(sensitivity)
        suggestions.append(
            DisclosureTagSuggestion(
                entry_id=entry_id,
                topics=normalize_list(row.get("topics")),
                sensitivity=sensitivity,
                disclosure_mode=mode,
                subjects=normalize_list(row.get("subjects")),
            )
        )
    return suggestions


def _batch_payload(entries: Sequence[MemoryEntry]) -> str:
    rows = []
    for entry in entries:
        content = " ".join(entry.content.split())
        if len(content) > 700:
            content = content[:697] + "..."
        rows.append(
            {
                "id": entry.id,
                "sector": entry.sector,
                "kind": entry.kind,
                "scope_type": entry.scope_type,
                "content": content,
            }
        )
    return json.dumps({"items": rows}, ensure_ascii=False)


def _backfill_mode_for_sensitivity(sensitivity: str) -> str:
    """Keep batch model output coherent and conservative."""
    return normalize_metadata({"sensitivity": sensitivity}).disclosure_mode


def _extract_json(text: str) -> object | None:
    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(
        chunk.strip()
        for chunk in re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
        if chunk.strip()
    )
    first_obj = stripped.find("{")
    last_obj = stripped.rfind("}")
    if 0 <= first_obj < last_obj:
        candidates.append(stripped[first_obj : last_obj + 1])
    first_arr = stripped.find("[")
    last_arr = stripped.rfind("]")
    if 0 <= first_arr < last_arr:
        candidates.append(stripped[first_arr : last_arr + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _backup_db(path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.disclosure-backfill.{timestamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


_SYSTEM_PROMPT = """\
You classify long-term assistant memory rows for disclosure-safe recall.
Return strict JSON only:
{"items":[{"id":"...","topics":["..."],"sensitivity":"normal|sensitive|private|taboo","disclosure_mode":"speakable|context_only|owner_only|never_initiate","subjects":["..."]}]}

Rules:
- Use short lowercase topic slugs in English or the user's language, e.g. finance, trading, health, family, funeral, work, relationship, travel, preference, humor.
- normal/speakable: ordinary facts, public chat topics, outside-world politics/war/news, public-figure death or illness, trading/news/technical topics, jokes, provocations, general preferences.
- sensitive/context_only: real personal medication/dosage or mild health/family safety context about a group member, user, known contact, or their close relative.
- private/owner_only: only use for explicit personal identifiers or private account/contact/location data, not for normal trading, politics, jokes, or public events.
- taboo/never_initiate: death, funeral, grief, severe/chronic illness, or self-harm involving a group member, user, known contact, or their close relative.
- Do not classify outside-world war, politics, public figures, public scandals, Epstein, antisemitic jokes/provocations, or financial-market discussion as sensitive/taboo merely because the topic is heavy or offensive.
- Do not over-classify normal finance/trading discussion as private unless it reveals a person's own account, debt, or personal financial vulnerability.
- Prefer normal when unsure. Prefer sensitive over private when the memory is mild.
- Return every input id exactly once.
- Do not copy the full memory text into the output.
"""
