"""Offline replay: today's window context (A) against the conversation view (B).

Spec A section 7 step 1. Every recorded judge opportunity of the last days is rebuilt
from history *as of its time* (later messages and reactions excluded) in both context
modes; optionally the Judge decides on both. Reports hold ids and aggregates only,
never message text, Judge reasons or purposes. Every store is opened read-only.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
    _JudgeContext,
)
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
    ParticipationDecisionInputs,
)

REPORT_DIR = ("eval", "participation-context-ab")
MAX_DISAGREEMENTS = 30
#: The replay grants every action; live grants depend on budgets that are not stored.
REPLAY_ACTIONS: tuple[str, ...] = ("silence", "react", "comment")
REPLAY_INTENTS: frozenset[str] = frozenset({"initiate", "continue"})


@dataclass(frozen=True, slots=True)
class RecordedOpportunity:
    """One live judge attempt with the sources it was offered."""

    attempt_id: str
    opportunity_id: str
    channel: str
    chat_id: str
    at_ms: int
    live_outcome: str
    trigger: str
    lane: str
    observed_revision: int
    activation_epoch: int
    source_ids: tuple[str, ...]


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{Path(path).expanduser().resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_opportunities(speakups_db: Path, *, since_ms: int, until_ms: int) -> list[RecordedOpportunity]:
    """First judge attempt per opportunity in the period, oldest first."""
    connection = _read_only(speakups_db)
    try:
        rows = connection.execute(
            """
            SELECT a.attempt_id, a.opportunity_id, a.channel, a.chat_id, a.created_at_ms,
                   a.outcome, d.trigger, d.lane, d.observed_revision, d.activation_epoch,
                   d.source_ids_json
            FROM judge_attempts a
            LEFT JOIN opportunity_dispositions d ON d.opportunity_id = a.opportunity_id
            WHERE a.evaluation_index = 0 AND a.created_at_ms >= ? AND a.created_at_ms <= ?
            ORDER BY a.created_at_ms, a.attempt_id
            """,
            (int(since_ms), int(until_ms)),
        ).fetchall()
    finally:
        connection.close()
    result: list[RecordedOpportunity] = []
    for row in rows:
        try:
            sources = json.loads(str(row["source_ids_json"] or "[]"))
        except ValueError:
            sources = []
        result.append(RecordedOpportunity(
            attempt_id=str(row["attempt_id"]),
            opportunity_id=str(row["opportunity_id"]),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            at_ms=int(row["created_at_ms"]),
            live_outcome=str(row["outcome"] or ""),
            trigger=str(row["trigger"] or "inbound"),
            lane=str(row["lane"] or "production"),
            observed_revision=max(0, int(row["observed_revision"] or 0)),
            activation_epoch=max(0, int(row["activation_epoch"] or 0)),
            source_ids=tuple(str(item) for item in sources if isinstance(item, str) and item),
        ))
    return result


class FrozenHistory:
    """Read-only history snapshots for an offline process; never a writer."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self._reader = HistoryReader(self.db_path)
        self.generation = self._current_generation()

    def _current_generation(self) -> int:
        connection = _read_only(self.db_path)
        try:
            row = connection.execute(
                "SELECT state_json FROM projector_state WHERE file='@runtime'"
            ).fetchone()
        finally:
            connection.close()
        state = json.loads(row[0]) if row is not None else None
        if not isinstance(state, dict) or not isinstance(state.get("generation"), int):
            raise HistoryPaused("reader_unavailable")
        return int(state["generation"])

    async def read_turn(self):
        try:
            return self._reader.open_snapshot(HistoryBoundary(self.generation, ()))
        except HistoryPaused:
            # The live gateway may have published a new generation meanwhile.
            self.generation = self._current_generation()
            return self._reader.open_snapshot(HistoryBoundary(self.generation, ()))

    def health(self) -> dict[str, object]:
        return {"status": "ready", "generation": self.generation}

    def close(self) -> None:
        self._reader.close()


def registry_lookup(processing_db: Path | None) -> Callable[[str, int], object] | None:
    """As-of thread-registry lookup by provider message id (read-only)."""
    if processing_db is None or not Path(processing_db).expanduser().is_file():
        return None
    path = Path(processing_db)

    def lookup(message_id: str, as_of_ms: int) -> object:
        connection = _read_only(path)
        try:
            return connection.execute(
                "SELECT thread_id FROM thread_messages WHERE message_id = ?"
                " AND confirmed_ms IS NOT NULL AND confirmed_ms <= ? LIMIT 1",
                (str(message_id), int(as_of_ms)),
            ).fetchone()
        finally:
            connection.close()

    return lookup


def _inputs(
    bounds: ParticipationContextBounds, guidance: str, sources: tuple[str, ...]
) -> ParticipationDecisionInputs:
    return ParticipationDecisionInputs(
        snapshot={"guidance": guidance, "allowed_contribution_types": ()},
        bounds=bounds, allowed_actions=REPLAY_ACTIONS, allowed_intents=REPLAY_INTENTS,
        remaining_budgets=(), reservation_limits_by_intent=(), approval_required=False,
        arbitration_revision=0, current_source_ids=sources, continuation_candidate=False,
    )


def _estimate_tokens(
    estimator: ParticipationJudge, opportunity: ParticipationOpportunity, context: Mapping[str, Any]
) -> int:
    """The Judge's own prompt estimate after its own budget fitting."""
    fitted = dict(context)
    fitted["messages"] = list(context.get("messages") or [])  # type: ignore[call-overload]
    fitted["anchors"] = list(context.get("anchors") or [])  # type: ignore[call-overload]
    estimator._fit_context_for_judge(fitted, opportunity)  # noqa: SLF001
    view = _JudgeContext.from_mapping(fitted)
    return estimator._estimate_tokens(estimator._build_messages(opportunity, view))  # noqa: SLF001


async def _variant(
    builder: ParticipationContextBuilder,
    opportunity: ParticipationOpportunity,
    inputs: ParticipationDecisionInputs,
    *,
    at_ms: int,
    estimator: ParticipationJudge,
    judge: ParticipationJudge | None,
    usage: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    try:
        context = await builder.build(opportunity, inputs=inputs, now_ms=at_ms)
    except ParticipationDecisionError as exc:
        return {"status": "context_error", "error": exc.reason}
    record: dict[str, Any] = {
        "status": "built",
        "context_mode": context.get("context_mode"),
        "conversation_id": context.get("conversation_id") or None,
        "conversation": context.get("conversation"),
        "messages": len(context.get("messages") or ()),  # type: ignore[arg-type]
        "message_ids": [str(row.get("event_id")) for row in context.get("messages") or ()],  # type: ignore[union-attr]
        "truncated_messages": context.get("truncated_messages"),
        "dropped_source_count": context.get("dropped_source_count"),
    }
    try:
        record["estimated_prompt_tokens"] = _estimate_tokens(estimator, opportunity, context)
    except ParticipationDecisionError as exc:
        record["estimated_prompt_tokens"] = None
        record["estimate_error"] = exc.reason
    if judge is None:
        return record
    try:
        decision = await judge.decide(opportunity, dict(context))
    except ParticipationDecisionError as exc:
        record.update(status="judge_error", error=exc.reason)
        return record
    reply_usage = dict(usage() or {}) if usage is not None else {}
    record.update(
        status="decided",
        action=decision.action,
        intent=decision.intent,
        emoji=decision.emoji,
        target_message_id=decision.target_message_id,
        evidence_ids=list(decision.evidence_ids),
        prompt_tokens=reply_usage.get("prompt_tokens"),
    )
    return record


async def replay(
    opportunities: list[RecordedOpportunity],
    *,
    history: Any,
    builder: ParticipationContextBuilder,
    window_bounds: ParticipationContextBounds,
    conversation_bounds: ParticipationContextBounds,
    estimator: ParticipationJudge,
    judge: ParticipationJudge | None = None,
    usage: Callable[[], Mapping[str, Any]] | None = None,
    guidance_for: Callable[[str, str], str] = lambda channel, chat_id: "",
    set_as_of: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """One JSON-compatible record per opportunity: A (window) and B (conversation)."""
    records: list[dict[str, Any]] = []
    for item in opportunities:
        record: dict[str, Any] = {
            "opportunity_id": item.opportunity_id, "attempt_id": item.attempt_id,
            "channel": item.channel, "chat_id": item.chat_id, "at_ms": item.at_ms,
            "live_outcome": item.live_outcome, "source_ids": list(item.source_ids),
        }
        if item.channel != "whatsapp" or not item.source_ids:
            record["skipped"] = "no_history_sources"
            records.append(record)
            continue
        opportunity = ParticipationOpportunity(
            opportunity_id=item.opportunity_id, channel=item.channel, chat_id=item.chat_id,
            trigger=item.trigger,  # type: ignore[arg-type]
            source_event_ids=item.source_ids, observed_revision=item.observed_revision,
            activation_epoch=item.activation_epoch, created_at_ms=item.at_ms, lane=item.lane,
        )
        guidance = guidance_for(item.channel, item.chat_id)
        if set_as_of is not None:
            set_as_of(item.at_ms)
        try:
            async with history_turn(history):
                for key, bounds in (("A", window_bounds), ("B", conversation_bounds)):
                    record[key] = await _variant(
                        builder, opportunity, _inputs(bounds, guidance, item.source_ids),
                        at_ms=item.at_ms, estimator=estimator, judge=judge, usage=usage,
                    )
        except HistoryPaused as exc:
            record["skipped"] = f"history_paused:{exc}"
        records.append(record)
    return records


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def summarize(records: list[dict[str, Any]], *, judged: bool, seed: int = 0) -> dict[str, Any]:
    """Agreement, token delta and up to 30 disagreement ids. Ids and aggregates only."""
    built = [r for r in records if r.get("A", {}).get("status") in {"built", "decided", "judge_error"}
             and r.get("B", {}).get("status") in {"built", "decided", "judge_error"}]
    estimates = [(r["A"].get("estimated_prompt_tokens"), r["B"].get("estimated_prompt_tokens"))
                 for r in built]
    estimates = [(a, b) for a, b in estimates if isinstance(a, int) and isinstance(b, int)]
    summary: dict[str, Any] = {
        "opportunities": len(records),
        "skipped": dict(Counter(str(r["skipped"]).split(":", 1)[0] for r in records if "skipped" in r)),
        "context_errors": {
            key: dict(Counter(r[key]["error"] for r in records
                              if r.get(key, {}).get("status") == "context_error"))
            for key in ("A", "B")
        },
        "built_both": len(built),
        "estimated_tokens_mean": {"A": _mean([a for a, _ in estimates]),
                                  "B": _mean([b for _, b in estimates])},
        "estimated_token_delta_mean": _mean([b - a for a, b in estimates]),
        "messages_mean": {key: _mean([r[key]["messages"] for r in built]) for key in ("A", "B")},
        "b_narrower_share": (round(sum(r["B"]["messages"] < r["A"]["messages"] for r in built)
                             / len(built), 3) if built else None),
        "b_fallbacks": sum(r["B"].get("context_mode") != "conversation" for r in built),
        "conversation_states": dict(Counter(
            (r["B"].get("conversation") or {}).get("state", "none") for r in built
        )),
        "judged": judged,
    }
    if not judged:
        return summary
    decided = [r for r in built if r["A"]["status"] == "decided" and r["B"]["status"] == "decided"]
    disagreements = [r["opportunity_id"] for r in decided if r["A"]["action"] != r["B"]["action"]]
    prompt = [(r["A"].get("prompt_tokens"), r["B"].get("prompt_tokens")) for r in decided]
    prompt = [(a, b) for a, b in prompt if isinstance(a, int) and isinstance(b, int)]
    sample = (sorted(random.Random(seed).sample(disagreements, MAX_DISAGREEMENTS))
              if len(disagreements) > MAX_DISAGREEMENTS else disagreements)
    summary.update({
        "decided_both": len(decided),
        "judge_errors": {key: dict(Counter(r[key]["error"] for r in built
                                           if r[key]["status"] == "judge_error"))
                         for key in ("A", "B")},
        "agreement_rate": (round(1 - len(disagreements) / len(decided), 3) if decided else None),
        "actions": {key: dict(Counter(r[key]["action"] for r in decided)) for key in ("A", "B")},
        "prompt_tokens_mean": {"A": _mean([a for a, _ in prompt]), "B": _mean([b for _, b in prompt])},
        "prompt_token_delta_mean": _mean([b - a for a, b in prompt]),
        "disagreement_count": len(disagreements),
        "disagreement_ids": sample,
    })
    return summary


def render_markdown(summary: Mapping[str, Any], *, days: int) -> str:
    lines = [
        "# Participation context A/B replay (window vs conversation)",
        "",
        f"Period: last {days} days. Ids and aggregates only; label disagreements in the "
        "owner's local review view (A better / B better / same).",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines.extend(
        f"| {key} | {json.dumps(value, ensure_ascii=False)} |" for key, value in summary.items()
    )
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, records: list[dict[str, Any]], summary: Mapping[str, Any],
                  markdown: str) -> None:
    with (directory / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "summary.md").write_text(markdown, encoding="utf-8")


__all__ = [
    "FrozenHistory",
    "RecordedOpportunity",
    "load_opportunities",
    "registry_lookup",
    "render_markdown",
    "replay",
    "summarize",
    "write_outputs",
]
