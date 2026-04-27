"""Taste distillation for proactive speakup behavior."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable
from typing import Any

from yeoman_gateway.consciousness.log import SpeakupLog

TasteDistillerFn = Callable[[str], dict[str, Any] | str | Awaitable[dict[str, Any] | str]]


class TasteDistiller:
    """Write compact chat-scope taste memories after enough labeled samples."""

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
        samples = await self._log.outcome_samples(
            channel=channel,
            chat_id=chat_id,
            limit=max(self._min_samples, 50),
        )
        if len(samples) < self._min_samples:
            return {
                "distilled": False,
                "reason": "not_enough_samples",
                "samples": len(samples),
            }

        sample_fingerprint = self._sample_fingerprint(samples)
        claimed = await self._log.claim_taste_distillation(
            channel=channel,
            chat_id=chat_id,
            sample_fingerprint=sample_fingerprint,
        )
        if not claimed:
            return {
                "distilled": False,
                "reason": "already_distilled",
                "samples": len(samples),
            }

        async def rollback() -> None:
            await self._log.delete_taste_distillation(
                channel=channel,
                chat_id=chat_id,
                sample_fingerprint=sample_fingerprint,
            )

        try:
            raw = self._distiller(self._build_prompt(samples))
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception:
            await rollback()
            raise

        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            await rollback()
            return {
                "distilled": False,
                "reason": "invalid_distiller_response",
                "samples": len(samples),
            }

        try:
            if not isinstance(parsed, dict):
                await rollback()
                return {"distilled": False, "reason": "invalid_distiller_response", "samples": len(samples)}
            pattern = " ".join(str(parsed.get("pattern") or "").split()).strip()
            if not pattern:
                await rollback()
                return {"distilled": False, "reason": "empty_pattern", "samples": len(samples)}
            raw_confidence = parsed.get("confidence", 0.8)
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                await rollback()
                return {
                    "distilled": False,
                    "reason": "invalid_distiller_response",
                    "samples": len(samples),
                }
            if not math.isfinite(confidence):
                await rollback()
                return {
                    "distilled": False,
                    "reason": "invalid_distiller_response",
                    "samples": len(samples),
                }
            text = f"Proactive speakup taste pattern: {pattern}"
            self._memory.record_manual(
                channel=channel,
                chat_id=chat_id,
                sender_id=None,
                scope_type="chat",
                kind="preference",
                text=text,
                importance=0.75,
                confidence=max(0.0, min(1.0, confidence)),
            )
        except Exception:
            await rollback()
            raise
        return {"distilled": True, "samples": len(samples)}

    @staticmethod
    def _sample_fingerprint(samples: list[dict[str, Any]]) -> str:
        payload = [
            {
                "action_type": str(sample.get("action_type") or ""),
                "profile": str(sample.get("profile") or ""),
                "message": str(sample.get("message") or ""),
                "outcome": str(sample.get("outcome") or ""),
            }
            for sample in samples
        ]
        payload.sort(
            key=lambda sample: (
                sample["action_type"],
                sample["profile"],
                sample["message"],
                sample["outcome"],
            )
        )
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _build_prompt(samples: list[dict[str, Any]]) -> str:
        payload = {
            "instruction": (
                "Return JSON with pattern and confidence. Describe aggregate chat taste "
                "for proactive speakups. Do not copy raw messages into the pattern."
            ),
            "samples": [
                {
                    "action_type": sample["action_type"],
                    "profile": sample["profile"],
                    "message": sample["message"],
                    "outcome": sample["outcome"],
                }
                for sample in samples
            ],
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
