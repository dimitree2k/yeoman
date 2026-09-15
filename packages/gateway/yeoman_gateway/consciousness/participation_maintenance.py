"""Participation maintenance: receipts, evidence-based outcomes, advisory taste.

Maintenance runs as its own bounded task with its own exception handling. It is not
a phase of a participation tick, so a blocked classifier can never block a
participation callback, and disabling judging never loses receipt reconciliation.

Learning inputs are strict (spec section 10):

* only **recipient-evidenced deliveries** with a fully elapsed observation window;
* exact quoted replies and reactions are *explicit* evidence, semantic association is
  *inferred* and carries source ids, and the absence of any reply is
  ``no_observed_feedback`` - never a rejection;
* preview, shadow, accepted-only, unknown and historical-unverified rows never enter
  a sample set, and a record without provenance is never promoted to authoritative
  guidance.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: Evidence kinds attached to a classified outcome.
EVIDENCE_EXPLICIT = "explicit"
EVIDENCE_INFERRED = "inferred"
EVIDENCE_NONE = "none"
EVIDENCE_UNCERTAIN = "uncertain"

#: Outcome labels accepted from the classifier.
OUTCOME_LABELS: tuple[str, ...] = (
    "replied",
    "reacted",
    "topic_changed",
    "pushback",
    "mixed",
    "no_observed_feedback",
)


def eligible_for_outcome(
    *, delivered_at_ms: int, now_ms: int, window_ms: int
) -> bool:
    """Whether a delivery's observation window has fully elapsed.

    Kept here rather than in a new utility module: it is the arithmetic of one
    predicate that the query boundary also enforces.
    """
    return int(now_ms) - int(delivered_at_ms) >= int(window_ms)


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    reconciled: dict[str, int]
    deliveries_seen: int = 0
    outcomes_classified: int = 0
    classified_without_a_call: int = 0
    failures: int = 0


class ParticipationMaintenance:
    """One bounded maintenance pass: reconcile receipts, then classify outcomes."""

    def __init__(
        self,
        *,
        ledger: Any,
        reconciler: Any | None = None,
        archive: Any | None = None,
        classifier: Any | None = None,
        observation_window_minutes: int = 120,
        batch_size: int = 20,
        clock_ms: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._reconciler = reconciler
        self._archive = archive
        self._classifier = classifier
        self._window_ms = max(1, int(observation_window_minutes)) * 60_000
        self._batch_size = max(1, int(batch_size))
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def run_once(self, *, now_ms: int | None = None) -> MaintenanceReport:
        moment = int(now_ms if now_ms is not None else self._clock_ms())
        reconciled: dict[str, int] = {}
        if self._reconciler is not None:
            try:
                reconciled = dict(
                    await self._reconciler.reconcile(limit=self._batch_size, now_ms=moment)
                )
            except Exception as exc:  # noqa: BLE001 - maintenance is best effort
                logger.warning(
                    "participation_reconcile_failed error_type={}", type(exc).__name__
                )

        window_start = moment - self._window_ms
        rows = await self._ledger.pending_outcome_deliveries(
            before_ms=window_start, limit=self._batch_size
        )
        classified = 0
        without_call = 0
        failures = 0
        for row in rows:
            try:
                result = await self._classify(row, now_ms=moment)
            except Exception as exc:  # noqa: BLE001 - one bad sample must not stop the batch
                failures += 1
                logger.warning(
                    "participation_outcome_failed effect_id={} error_type={}",
                    str(row.get("effect_id"))[:32],
                    type(exc).__name__,
                )
                continue
            if result is None:
                failures += 1
                continue
            outcome, evidence_kind, evidence_ids, used_call = result
            if not used_call:
                without_call += 1
            await self._ledger.mark_delivery_outcome(
                effect_id=str(row["effect_id"]),
                outcome=outcome,
                evidence_kind=evidence_kind,
                evidence_ids=evidence_ids,
                now_ms=moment,
            )
            classified += 1
        return MaintenanceReport(
            reconciled=reconciled,
            deliveries_seen=len(rows),
            outcomes_classified=classified,
            classified_without_a_call=without_call,
            failures=failures,
        )

    async def _classify(
        self, row: dict[str, Any], *, now_ms: int
    ) -> tuple[str, str, tuple[str, ...], bool] | None:
        """Classify one delivered statement. ``None`` means "failed, retry later"."""
        explicit = await self._explicit_feedback(row)
        if explicit is not None:
            outcome, evidence_ids = explicit
            return (outcome, EVIDENCE_EXPLICIT, evidence_ids, False)
        if self._classifier is None:
            # No classifier configured: silence is not a rejection, and no provider
            # call is spent to discover that. The archive, when present, is only
            # additional window material for the classifier.
            return ("no_observed_feedback", EVIDENCE_NONE, (), False)
        payload = self._prompt(row, now_ms=now_ms)
        raw = self._classifier(payload)
        if hasattr(raw, "__await__"):
            raw = await raw
        parsed = _parse_outcome(raw)
        if parsed is None:
            return None
        outcome, evidence_ids = parsed
        if outcome == "no_observed_feedback":
            return (outcome, EVIDENCE_NONE, (), True)
        return (outcome, EVIDENCE_INFERRED, evidence_ids, True)

    async def _explicit_feedback(
        self, row: dict[str, Any]
    ) -> tuple[str, tuple[str, ...]] | None:
        """Exact quotes and reactions are strong feedback and need no classifier."""
        reader = getattr(self._ledger, "explicit_feedback", None)
        if reader is None:
            return None
        try:
            found = reader(
                channel=str(row.get("channel")),
                chat_id=str(row.get("chat_id")),
                provider_message_id=str(row.get("provider_message_id") or ""),
            )
            if hasattr(found, "__await__"):
                found = await found
        except Exception:
            return None
        if not isinstance(found, dict) or not found:
            return None
        kind = str(found.get("kind") or "")
        evidence_id = str(found.get("event_id") or "")
        if kind == "reaction":
            return ("reacted", (evidence_id,) if evidence_id else ())
        if kind == "reply":
            return ("replied", (evidence_id,) if evidence_id else ())
        return None

    def _prompt(self, row: dict[str, Any], *, now_ms: int) -> str:
        import json

        delivered_at = int(row.get("delivered_at_ms") or 0)
        after: list[Any] = []
        if self._archive is not None:
            from datetime import UTC, datetime

            try:
                after = list(
                    self._archive.lookup_messages_in_range(
                        str(row.get("channel")),
                        str(row.get("chat_id")),
                        datetime.fromtimestamp(delivered_at / 1000, UTC),
                        datetime.fromtimestamp(now_ms / 1000, UTC),
                        limit=50,
                    )
                )
            except Exception:
                after = []
        return json.dumps(
            {
                "instruction": (
                    "Return JSON with outcome and evidence_ids. Allowed outcomes: "
                    + ", ".join(OUTCOME_LABELS)
                    + ". Use no_observed_feedback when nothing in the window refers to "
                    "the message. Absence of a reply is not rejection. Do not invent ids."
                ),
                "message": str(row.get("message") or ""),
                "post_delivery_window": after,
            },
            ensure_ascii=False,
            default=str,
        )


def _parse_outcome(raw: Any) -> tuple[str, tuple[str, ...]] | None:
    import json

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    outcome = str(parsed.get("outcome") or "").strip()
    if outcome not in OUTCOME_LABELS:
        return None
    evidence = parsed.get("evidence_ids")
    evidence_ids: tuple[str, ...] = ()
    if isinstance(evidence, (list, tuple)):
        evidence_ids = tuple(
            str(item).strip() for item in evidence[:8] if str(item).strip()
        )
    return outcome, evidence_ids


__all__ = [
    "EVIDENCE_EXPLICIT",
    "EVIDENCE_INFERRED",
    "EVIDENCE_NONE",
    "EVIDENCE_UNCERTAIN",
    "OUTCOME_LABELS",
    "MaintenanceReport",
    "ParticipationMaintenance",
    "eligible_for_outcome",
]
