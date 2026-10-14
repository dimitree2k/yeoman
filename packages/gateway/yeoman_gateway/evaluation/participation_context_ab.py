"""Offline replay: today's window context (A) against the conversation view (B).

Spec A section 7 step 1. Every recorded judge opportunity of the last days is rebuilt
from history *as of its time* (later messages and reactions excluded) in both context
modes; optionally the Judge decides on both. Reports hold ids and aggregates only,
never message text, Judge reasons or purposes. Every store is opened read-only.

The owner review rebuilds the two views of each disagreement for the local terminal
only; ``labels.jsonl`` holds ids, labels and times.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
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
DEFAULT_CONCURRENCY = 6
LABELS_FILE = "labels.jsonl"
#: Owner review keys and the labels they write; skip and quit write nothing.
LABEL_KEYS: dict[str, str] = {"a": "a_better", "b": "b_better", "s": "same"}
#: Spec A section 7 step 3: B better or same in at least 70 % of labelled disagreements.
B_BETTER_OR_SAME_THRESHOLD = 0.7


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


def load_opportunities(
    speakups_db: Path, *, since_ms: int, until_ms: int, chat_ids: Collection[str] | None = None
) -> list[RecordedOpportunity]:
    """First judge attempt per opportunity in the period, oldest first (optionally per chat)."""
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
    wanted = None if chat_ids is None else {str(item) for item in chat_ids}
    result: list[RecordedOpportunity] = []
    for row in rows:
        if wanted is not None and str(row["chat_id"]) not in wanted:
            continue
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


def looks_like_chat_id(value: str) -> bool:
    """WhatsApp JIDs carry an ``@``; Telegram chat ids are (signed) integers."""
    text = str(value).strip()
    return "@" in text or text.lstrip("-").isdigit()


async def chat_subjects(history: Any) -> dict[str, str]:
    """Current group subject per chat id, from history (chats without one are left out)."""
    async with history_turn(history) as snapshot:
        rows = HistoryQueries(snapshot).chats()
    return {str(row["chat_id"]): str(row["subject"]) for row in rows if row.get("subject")}


def resolve_chat(value: str, subjects: Mapping[str, str]) -> str:
    """A chat id as given, or the one chat whose subject matches exactly (any case).

    Raises :class:`ValueError` with the match count only; other chats' subjects are
    never part of the message.
    """
    text = str(value).strip()
    if looks_like_chat_id(text):
        return text
    wanted = text.casefold()
    matches = sorted(chat for chat, subject in subjects.items() if subject.strip().casefold() == wanted)
    if len(matches) != 1:
        problem = "no chat" if not matches else f"{len(matches)} chats"
        raise ValueError(f"{problem} with that subject ({len(matches)} matches); pass the chat id")
    return matches[0]


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


_CALL_USAGE: ContextVar[dict[str, Any] | None] = ContextVar("replay_call_usage", default=None)


class UsageCapture:
    """Wrap a route client; each reply's usage goes to the replay task that asked.

    A shared "last reply" is wrong once Judge calls overlap, so the usage lands in a
    holder the calling task set (child tasks share the holder object).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.route_key = getattr(inner, "route_key", "")
        self.structured_output = getattr(inner, "structured_output", "json_schema_strict")
        self.model = getattr(inner, "model", "")

    async def chat_with_usage(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        reply = await self._inner.chat_with_usage(messages, **kwargs)
        holder = _CALL_USAGE.get()
        if holder is not None:
            holder.clear()
            holder.update(dict(getattr(reply, "usage", None) or {}))
        return reply


def captured_usage() -> Mapping[str, Any]:
    """Usage of the latest reply in this Judge call (with :class:`UsageCapture`)."""
    return dict(_CALL_USAGE.get() or {})


def _fitted_view(
    estimator: ParticipationJudge, opportunity: ParticipationOpportunity, context: Mapping[str, Any]
) -> _JudgeContext:
    """The view the Judge sees after its own budget fitting."""
    fitted = dict(context)
    fitted["messages"] = list(context.get("messages") or [])  # type: ignore[call-overload]
    fitted["anchors"] = list(context.get("anchors") or [])  # type: ignore[call-overload]
    estimator._fit_context_for_judge(fitted, opportunity)  # noqa: SLF001
    return _JudgeContext.from_mapping(fitted)


def _estimate_tokens(
    estimator: ParticipationJudge, opportunity: ParticipationOpportunity, context: Mapping[str, Any]
) -> int:
    """The Judge's own prompt estimate after its own budget fitting."""
    view = _fitted_view(estimator, opportunity, context)
    return estimator._estimate_tokens(estimator._build_messages(opportunity, view))  # noqa: SLF001


def _opportunity(item: RecordedOpportunity) -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id=item.opportunity_id, channel=item.channel, chat_id=item.chat_id,
        trigger=item.trigger,  # type: ignore[arg-type]
        source_event_ids=item.source_ids, observed_revision=item.observed_revision,
        activation_epoch=item.activation_epoch, created_at_ms=item.at_ms, lane=item.lane,
    )


async def _build_variant(
    builder: ParticipationContextBuilder,
    opportunity: ParticipationOpportunity,
    inputs: ParticipationDecisionInputs,
    *,
    at_ms: int,
    estimator: ParticipationJudge,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The variant record and the built context (``None`` on a context error)."""
    try:
        context = await builder.build(opportunity, inputs=inputs, now_ms=at_ms)
    except ParticipationDecisionError as exc:
        return {"status": "context_error", "error": exc.reason}, None
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
    return record, dict(context)


async def _judge_variant(
    record: dict[str, Any],
    opportunity: ParticipationOpportunity,
    context: dict[str, Any],
    *,
    judge: ParticipationJudge,
    usage: Callable[[], Mapping[str, Any]] | None,
) -> None:
    """Decide on one built variant and fill its record in place."""
    _CALL_USAGE.set({})  # this task's holder; set inside the task, so never shared
    try:
        decision = await judge.decide(opportunity, context)
    except ParticipationDecisionError as exc:
        record.update(status="judge_error", error=exc.reason)
        return
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
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """One JSON-compatible record per opportunity: A (window) and B (conversation).

    Contexts are built one after another (history reads, as-of registry lookups);
    only the Judge calls overlap, at most ``concurrency`` at a time. Records come
    back in opportunity order with the same content as a sequential run.
    """
    records: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], ParticipationOpportunity, dict[str, Any]]] = []
    for item in opportunities:
        record: dict[str, Any] = {
            "opportunity_id": item.opportunity_id, "attempt_id": item.attempt_id,
            "channel": item.channel, "chat_id": item.chat_id, "at_ms": item.at_ms,
            "live_outcome": item.live_outcome, "source_ids": list(item.source_ids),
            "trigger": item.trigger, "lane": item.lane,
        }
        if item.channel != "whatsapp" or not item.source_ids:
            record["skipped"] = "no_history_sources"
            records.append(record)
            continue
        opportunity = _opportunity(item)
        guidance = guidance_for(item.channel, item.chat_id)
        if set_as_of is not None:
            set_as_of(item.at_ms)
        try:
            async with history_turn(history):
                for key, bounds in (("A", window_bounds), ("B", conversation_bounds)):
                    variant, context = await _build_variant(
                        builder, opportunity, _inputs(bounds, guidance, item.source_ids),
                        at_ms=item.at_ms, estimator=estimator,
                    )
                    record[key] = variant
                    if judge is not None and context is not None:
                        pending.append((variant, opportunity, context))
        except HistoryPaused as exc:
            record["skipped"] = f"history_paused:{exc}"
        records.append(record)
    if judge is not None and pending:
        slots = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(job: tuple[dict[str, Any], ParticipationOpportunity, dict[str, Any]]) -> None:
            async with slots:
                await _judge_variant(*job, judge=judge, usage=usage)

        await asyncio.gather(*(_one(job) for job in pending))
    return records


def record_opportunity(record: Mapping[str, Any]) -> RecordedOpportunity:
    """The opportunity a report record was replayed from (older reports: default lane)."""
    return RecordedOpportunity(
        attempt_id=str(record.get("attempt_id") or ""),
        opportunity_id=str(record["opportunity_id"]),
        channel=str(record.get("channel") or ""),
        chat_id=str(record.get("chat_id") or ""),
        at_ms=int(record["at_ms"]),
        live_outcome=str(record.get("live_outcome") or ""),
        trigger=str(record.get("trigger") or "inbound"),
        lane=str(record.get("lane") or "production"),
        observed_revision=0,
        activation_epoch=0,
        source_ids=tuple(str(item) for item in record.get("source_ids") or () if item),
    )


async def rebuild_views(
    record: Mapping[str, Any],
    *,
    history: Any,
    builder: ParticipationContextBuilder,
    window_bounds: ParticipationContextBounds,
    conversation_bounds: ParticipationContextBounds,
    estimator: ParticipationJudge,
    guidance_for: Callable[[str, str], str] = lambda channel, chat_id: "",
    set_as_of: Callable[[int], None] | None = None,
) -> dict[str, str]:
    """Both views of one record as the Judge saw them, for the owner's terminal only.

    Same as-of rebuild as :func:`replay`; the B header line is part of the view.
    """
    item = record_opportunity(record)
    opportunity = _opportunity(item)
    guidance = guidance_for(item.channel, item.chat_id)
    if set_as_of is not None:
        set_as_of(item.at_ms)
    views: dict[str, str] = {}
    async with history_turn(history):
        for key, bounds in (("A", window_bounds), ("B", conversation_bounds)):
            try:
                context = await builder.build(
                    opportunity, inputs=_inputs(bounds, guidance, item.source_ids), now_ms=item.at_ms
                )
            except ParticipationDecisionError as exc:
                views[key] = f"(context error: {exc.reason})"
                continue
            views[key] = _fitted_view(estimator, opportunity, context).render()
    return views


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


def disagreement_ids(records: list[dict[str, Any]], summary: Mapping[str, Any], *,
                     every: bool = False) -> list[str]:
    """The summary's sample (up to 30), or every disagreement in record order."""
    if not every:
        return [str(item) for item in summary.get("disagreement_ids") or ()]
    return [
        str(r["opportunity_id"]) for r in records
        if r.get("A", {}).get("status") == "decided" and r.get("B", {}).get("status") == "decided"
        and r["A"].get("action") != r["B"].get("action")
    ]


def read_labels(path: Path) -> dict[str, str]:
    """Opportunity id -> label; the first label of an id counts."""
    labels: dict[str, str] = {}
    if not path.is_file():
        return labels
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("label") in LABEL_KEYS.values():
            labels.setdefault(str(entry.get("opportunity_id")), str(entry["label"]))
    return labels


def append_label(path: Path, opportunity_id: str, label: str, *, labelled_at: str) -> None:
    if label not in LABEL_KEYS.values():
        raise ValueError(f"unknown label: {label}")
    entry = {"opportunity_id": opportunity_id, "label": label, "labelled_at": labelled_at}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def label_summary(labels: Mapping[str, str]) -> dict[str, Any]:
    """Label counts and the share "B better or same" against the switch threshold."""
    counts = Counter(labels.values())
    labelled = sum(counts[label] for label in LABEL_KEYS.values())
    share = (round((counts["b_better"] + counts["same"]) / labelled, 3) if labelled else None)
    return {
        "labelled": labelled,
        **{label: counts[label] for label in LABEL_KEYS.values()},
        "b_better_or_same_share": share,
        "threshold": B_BETTER_OR_SAME_THRESHOLD,
        "meets_threshold": share is not None and share >= B_BETTER_OR_SAME_THRESHOLD,
    }


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
    "UsageCapture",
    "append_label",
    "captured_usage",
    "chat_subjects",
    "disagreement_ids",
    "label_summary",
    "load_opportunities",
    "looks_like_chat_id",
    "read_labels",
    "rebuild_views",
    "record_opportunity",
    "registry_lookup",
    "render_markdown",
    "replay",
    "resolve_chat",
    "summarize",
    "write_outputs",
]
