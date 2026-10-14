"""Paired shadow of the weekly group notes (Spec B section 8, step 2).

With ``processing.participation.groupNotes = shadow`` every live Judge opportunity in a
group that has a renderable note gets a second Judge call *with* the note. The second
call is judge-only: no quota, no ledger row, no effect. Both decisions land here, one
JSON line per opportunity in ``var/eval/participation-groupnotes-ab/<UTC day>.jsonl``.

This recorder is separate from Spec A's context replay (``participation_context_ab``):
the two experiments are compared against the same live baseline and never combined.
Records hold ids, actions and token estimates only, never message text, Judge reasons,
purposes or the note itself.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_gateway.processing.participation import (
    ParticipationDecision,
    ParticipationDecisionError,
    ParticipationOpportunity,
)

REPORT_DIR = ("eval", "participation-groupnotes-ab")
RECORD_VERSION = "groupnotes-ab-v1"


def decision_record(
    outcome: ParticipationDecision | BaseException | None,
    *,
    prompt_tokens: int | None,
    rendered: bool | None = None,
) -> dict[str, Any]:
    """One variant: the Judge's decision or failure, ids only (as Spec A's records)."""
    record: dict[str, Any] = {"estimated_prompt_tokens": prompt_tokens}
    if rendered is not None:
        record["group_notes_rendered"] = bool(rendered)
    if isinstance(outcome, ParticipationDecision):
        record.update(
            status="decided",
            action=outcome.action,
            intent=outcome.intent,
            emoji=outcome.emoji,
            target_message_id=outcome.target_message_id,
            evidence_ids=list(outcome.evidence_ids),
        )
    elif isinstance(outcome, ParticipationDecisionError):
        record.update(status="judge_error", error=outcome.reason)
    elif isinstance(outcome, BaseException):
        record.update(status="judge_error", error="provider_error")
    else:
        record.update(status="not_judged")
    return record


class GroupNotesShadowRecorder:
    """Append-only JSONL of paired live / with-note Judge decisions."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()

    def path_for(self, at_ms: int) -> Path:
        day = datetime.fromtimestamp(int(at_ms) / 1000, UTC).strftime("%Y-%m-%d")
        return self.directory / f"{day}.jsonl"

    def record(
        self,
        opportunity: ParticipationOpportunity,
        *,
        note: Mapping[str, Any],
        live: Mapping[str, Any],
        with_notes: Mapping[str, Any],
        at_ms: int,
    ) -> dict[str, Any]:
        """Write one pair and return it. ``note`` contributes its id and week only."""
        entry: dict[str, Any] = {
            "version": RECORD_VERSION,
            "opportunity_id": opportunity.opportunity_id,
            "channel": opportunity.channel,
            "chat_id": opportunity.chat_id,
            "trigger": opportunity.trigger,
            "lane": opportunity.lane,
            "at_ms": int(at_ms),
            "digest_id": str(note.get("digest_id") or ""),
            "week": str(note.get("week") or ""),
            "live": dict(live),
            "with_notes": dict(with_notes),
            "same_action": (
                live.get("status") == "decided"
                and with_notes.get("status") == "decided"
                and live.get("action") == with_notes.get("action")
            ),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        path = self.path_for(at_ms)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        return entry


def read_records(directory: Path) -> list[dict[str, Any]]:
    """Every recorded pair, oldest day first."""
    records: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


__all__ = [
    "RECORD_VERSION",
    "REPORT_DIR",
    "GroupNotesShadowRecorder",
    "decision_record",
    "read_records",
]
