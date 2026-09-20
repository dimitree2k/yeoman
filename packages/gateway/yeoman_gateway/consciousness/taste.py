"""Taste distillation for proactive speakup behavior."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from yeoman_gateway.consciousness.log import SpeakupLog

TasteDistillerFn = Callable[[str], dict[str, Any] | str | Awaitable[dict[str, Any] | str]]

#: Provenance stamped on every pattern the participation lane writes. Recall for the
#: autonomous path requires it, so an old pattern without provenance is never served
#: as authoritative new guidance.
PARTICIPATION_TASTE_PROVENANCE = "participation:v1"

_PARTICIPATION_META: dict[str, object] = {
    "provenance": PARTICIPATION_TASTE_PROVENANCE,
    "pipeline": "participation-maintenance",
}


class ParticipationTasteDistiller:
    """Advisory taste from verified participation outcomes. Never mutates policy.

    Only delivered, provenance-tagged samples are eligible, distillation needs at
    least ``min_samples`` of them, and an identical sample set is never distilled
    twice. Confidence stays descriptive metadata; it grants no send capacity, no
    tool right and no budget.
    """

    def __init__(
        self,
        *,
        log: SpeakupLog,
        memory: object,
        distiller: TasteDistillerFn,
        min_samples: int = 10,
    ) -> None:
        self._log = log
        self._memory = memory
        self._distiller = distiller
        self._min_samples = max(1, int(min_samples))

    async def run_once(self, *, channel: str, chat_id: str) -> dict[str, object]:
        samples = await self._log.participation_outcome_samples(
            channel=channel, chat_id=chat_id, limit=max(self._min_samples, 50)
        )
        if len(samples) < self._min_samples:
            return {
                "distilled": False,
                "reason": "not_enough_samples",
                "samples": len(samples),
            }
        fingerprint = _participation_fingerprint(samples)
        claimed = await self._log.claim_taste_distillation(
            channel=channel,
            chat_id=chat_id,
            sample_fingerprint=fingerprint,
        )
        if not claimed:
            return {
                "distilled": False,
                "reason": "already_distilled",
                "samples": len(samples),
            }
        try:
            raw = self._distiller(_participation_prompt(samples))
            if inspect.isawaitable(raw):
                raw = await raw
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, dict):
                raise ValueError("distiller did not return an object")
            pattern = " ".join(str(parsed.get("pattern") or "").split()).strip()
            if not pattern:
                raise ValueError("empty pattern")
            confidence = float(parsed.get("confidence", 0.8))
            if not math.isfinite(confidence):
                raise ValueError("non-finite confidence")
        except Exception:
            await self._log.delete_taste_distillation(
                channel=channel,
                chat_id=chat_id,
                sample_fingerprint=fingerprint,
            )
            logger.warning(
                "participation taste distillation failed channel={} chat={}", channel, chat_id
            )
            return {
                "distilled": False,
                "reason": "distiller_failed",
                "samples": len(samples),
            }
        evidence_mix = sorted(
            {str(sample.get("outcome_kind") or "") for sample in samples if sample.get("outcome_kind")}
        )
        try:
            self._memory.record_manual(
                channel=channel,
                chat_id=chat_id,
                sender_id=None,
                scope_type="chat",
                kind="preference",
                text=f"Proactive speakup taste pattern: {pattern}",
                importance=0.75,
                confidence=max(0.0, min(1.0, confidence)),
                extra_meta={
                    **_PARTICIPATION_META,
                    "sample_count": len(samples),
                    "evidence_mix": evidence_mix,
                    "sample_fingerprint": fingerprint,
                },
            )
        except Exception:
            await self._log.delete_taste_distillation(
                channel=channel,
                chat_id=chat_id,
                sample_fingerprint=fingerprint,
            )
            raise
        return {"distilled": True, "samples": len(samples), "provenance": PARTICIPATION_TASTE_PROVENANCE}


def _participation_fingerprint(samples: list[dict[str, Any]]) -> str:
    payload = [
        {
            "effect_id": str(sample.get("effect_id") or ""),
            "outcome": str(sample.get("outcome") or ""),
            "outcome_kind": str(sample.get("outcome_kind") or ""),
        }
        for sample in samples
    ]
    payload.sort(key=lambda item: (item["effect_id"], item["outcome"], item["outcome_kind"]))
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _participation_prompt(samples: list[dict[str, Any]]) -> str:
    """Aggregate prompt. Raw participant text is not copied into the pattern."""
    return json.dumps(
        {
            "instruction": (
                "Return JSON with pattern and confidence. Describe aggregate chat taste "
                "for autonomous participation from verified delivery outcomes only. "
                "Never copy raw participant messages into the pattern."
            ),
            "samples": [
                {
                    "outcome": sample.get("outcome"),
                    "outcome_kind": sample.get("outcome_kind"),
                    "action_type": sample.get("category"),
                }
                for sample in samples
            ],
        },
        ensure_ascii=False,
        default=str,
    )
