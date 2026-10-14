"""Offline replay: today's live context (A) against the same context plus group notes (B).

Spec B section 8, instead of weeks of live shadow. Every recorded Judge opportunity of
the last days in a group is rebuilt from history *as of its time* in the live context
mode (A); B is the same context plus the group-notes block, fitted by the Judge's own
budget rules exactly as ``groupNotes: on`` would fit it.

No leakage: an opportunity at time ``t`` sees the note of the ISO week *before* the week
of ``t``, built from that week alone (``read_group_signals`` reads messages and
reactions up to the week's end only): what live would have had. The notes are made by
:class:`~yeoman_gateway.processing.group_digest.GroupDigestRunner` itself (same prompt,
validation and minimum signal) with an in-memory sink in place of Knowledge, so nothing
is stored anywhere; one model call per chat-week and run. A stored digest of that chat
and week may be reused instead (``use_stored``).

Reports hold ids and aggregates only, never message text, Judge reasons or purposes.
The one exception is ``notes.jsonl``: the validated notes of the run (style guidance,
checked against participant names and phone numbers), kept for the owner review, which
rebuilds both views of each disagreement for the local terminal only. Every store is
opened read-only.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import Any

from yeoman_gateway.evaluation.participation_context_ab import (
    MAX_DISAGREEMENTS,
    MAX_JUDGE_REPEATS,
    RecordedOpportunity,
    _build_variant,
    _combine_repeats,
    _fitted_view,
    _inputs,
    _judge_variant,
    _mean,
    _opportunity,
    _read_only,
)
from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.group_signals import DigestWeek, previous_week
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
    _JudgeContext,
)
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
)
from yeoman_gateway.processing.participation_group_notes import (
    is_group_chat,
    render_group_notes_block,
)

REPORT_DIR = ("eval", "participation-groupnotes-replay")
NOTES_FILE = "notes.jsonl"
NOTE_FIELDS: tuple[str, ...] = ("chat_id", "week", "status", "reason", "source", "digest_id")


# ── notes ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayDigest:
    """What the runner believes it stored: held in memory, never written."""

    digest_id: str
    chat_id: str
    week: str
    rendered: str
    confidence: str
    window_end_ms: int
    version: int = 0


class MemoryDigestSink:
    """Knowledge stand-in for :class:`GroupDigestRunner`: keeps each note in memory.

    Only ``store_group_digest`` exists; any other Knowledge call fails loudly.
    """

    def __init__(self) -> None:
        self.stored: list[ReplayDigest] = []

    def store_group_digest(self, **fields: Any) -> ReplayDigest:
        digest = ReplayDigest(
            digest_id=f"replay:{fields['chat_id']}:{fields['week']}",
            chat_id=str(fields["chat_id"]),
            week=str(fields["week"]),
            rendered=str(fields["rendered"]),
            confidence=str(fields["confidence"]),
            window_end_ms=int(fields["window_end_ms"]),
        )
        self.stored.append(digest)
        return digest


@dataclass(frozen=True, slots=True)
class ReplayNote:
    """The note one chat-week offers, or why there is none.

    ``status``: ``ready``, ``insufficient_signal``, ``failed`` (with ``reason``),
    ``paused`` or ``unavailable`` (no stored note and no generation allowed).
    """

    chat_id: str
    week: str
    status: str
    reason: str = ""
    source: str = ""
    digest_id: str = ""
    text: str = ""
    model_calls: int = 0

    def meta(self) -> dict[str, Any]:
        """Ids and states only, never the note text."""
        return {name: getattr(self, name) for name in NOTE_FIELDS}

    @property
    def skip_reason(self) -> str:
        if self.status == "failed":
            return f"digest_failed:{self.reason}"
        if self.status == "paused":
            return "history_paused"
        if self.status == "unavailable":
            return "note_unavailable"
        return self.status


def stored_note_reader(knowledge_db: Path | None) -> Callable[[str, str], ReplayNote | None]:
    """Latest non-stale stored digest of one chat and week (read-only), else ``None``."""

    def read(chat_id: str, week: str) -> ReplayNote | None:
        if knowledge_db is None or not Path(knowledge_db).expanduser().is_file():
            return None
        try:
            connection = _read_only(Path(knowledge_db))
        except sqlite3.Error:
            return None
        try:
            row = connection.execute(
                "SELECT digest_id, rendered FROM knowledge_group_digests WHERE chat_id = ?"
                " AND week = ? AND scope_key = ? AND status != 'stale' AND stale_ms IS NULL"
                " ORDER BY version DESC LIMIT 1",
                (str(chat_id), str(week), f"channel:whatsapp:chat:{chat_id}"),
            ).fetchone()
        except sqlite3.Error:  # no digest tables yet
            return None
        finally:
            connection.close()
        if row is None or not str(row["rendered"] or "").strip():
            return None
        return ReplayNote(
            chat_id=str(chat_id), week=str(week), status="ready", source="stored",
            digest_id=str(row["digest_id"]), text=str(row["rendered"]).strip(),
        )

    return read


class ReplayNotes:
    """One note per chat and week for one run; never more than one model call each.

    ``runner_for(week)`` returns a :class:`GroupDigestRunner` over a
    :class:`MemoryDigestSink`. With ``generate`` false no model is called: a stored note
    is used when there is one, otherwise the week's signal is read (no call) to tell
    ``insufficient_signal`` from ``unavailable``.
    """

    def __init__(
        self,
        *,
        runner_for: Callable[[DigestWeek], Any],
        client: Any | None = None,
        generate: bool = True,
        stored: Callable[[str, str], ReplayNote | None] | None = None,
        use_stored: bool = False,
        seeded: Mapping[tuple[str, str], ReplayNote] | None = None,
    ) -> None:
        if generate and client is None:
            raise ValueError("generating notes needs a digest client")
        self._runner_for = runner_for
        self._client = client
        self._generate = bool(generate)
        self._stored = stored
        self._use_stored = bool(use_stored) or not generate
        self._notes: dict[tuple[str, str], ReplayNote] = dict(seeded or {})

    @property
    def digest_calls(self) -> int:
        return sum(note.model_calls for note in self._notes.values())

    def notes(self) -> list[ReplayNote]:
        return [self._notes[key] for key in sorted(self._notes)]

    def get(self, chat_id: str, week: str) -> ReplayNote:
        return self._notes.get((str(chat_id), str(week))) or ReplayNote(
            chat_id=str(chat_id), week=str(week), status="unavailable"
        )

    async def prepare(self, keys: Iterable[tuple[str, DigestWeek]], *, concurrency: int = 1) -> None:
        """Resolve every distinct chat-week once; at most ``concurrency`` at a time."""
        todo: dict[tuple[str, str], tuple[str, DigestWeek]] = {}
        for chat_id, week in keys:
            key = (str(chat_id), week.label)
            if key not in self._notes:
                todo.setdefault(key, (str(chat_id), week))
        slots = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(key: tuple[str, str], chat_id: str, week: DigestWeek) -> None:
            async with slots:
                self._notes[key] = await self._resolve(chat_id, week)

        await asyncio.gather(*(_one(key, *item) for key, item in sorted(todo.items())))

    async def _resolve(self, chat_id: str, week: DigestWeek) -> ReplayNote:
        if self._use_stored and self._stored is not None:
            stored = self._stored(chat_id, week.label)
            if stored is not None:
                return stored
        runner = self._runner_for(week)
        if not self._generate:
            try:
                signals, _names, _phones = await runner.read(chat_id, week)
            except HistoryPaused:
                return ReplayNote(chat_id, week.label, "paused", "history_paused")
            status = "unavailable" if signals.meets_minimum else "insufficient_signal"
            return ReplayNote(chat_id, week.label, status)
        outcome = await runner.run(chat_id, week, client=self._client)
        calls = int(outcome.model_calls)
        if outcome.state == "skipped":
            return ReplayNote(chat_id, week.label, outcome.reason or "skipped", model_calls=calls)
        if outcome.state == "done" and outcome.digest is not None:
            return ReplayNote(
                chat_id=chat_id, week=week.label, status="ready", source="generated",
                digest_id=str(outcome.digest.digest_id), text=str(outcome.digest.rendered).strip(),
                model_calls=calls,
            )
        return ReplayNote(chat_id, week.label, outcome.state, outcome.reason, model_calls=calls)


def note_week(at_ms: int, *, tz: tzinfo | None = None) -> DigestWeek:
    """The week whose note an opportunity at ``at_ms`` sees: the one before its own."""
    return previous_week(int(at_ms), tz=tz)


# ── replay ───────────────────────────────────────────────────────────────────


def _with_note(context: Mapping[str, Any], note: ReplayNote) -> dict[str, Any]:
    """B: the A context plus the note, exactly as ``groupNotes: on`` attaches it."""
    copied = dict(context)
    for key in ("messages", "anchors"):
        if isinstance(context.get(key), list):
            copied[key] = list(context[key])  # type: ignore[call-overload]
    copied["group_notes_text"] = note.text
    copied["group_notes_week"] = note.week
    copied["group_notes_digest_id"] = note.digest_id
    return copied


def _note_variant(
    estimator: ParticipationJudge,
    opportunity: ParticipationOpportunity,
    context: Mapping[str, Any],
    a_record: Mapping[str, Any],
) -> dict[str, Any]:
    """B's record before judgment: its estimate and whether the note survives fitting."""
    record: dict[str, Any] = {
        "status": "built",
        "context_mode": a_record.get("context_mode"),
        "messages": a_record.get("messages"),
        "note_week": context.get("group_notes_week"),
    }
    fitted = dict(context)
    fitted["messages"] = list(context.get("messages") or [])  # type: ignore[call-overload]
    fitted["anchors"] = list(context.get("anchors") or [])  # type: ignore[call-overload]
    try:
        estimator._fit_context_for_judge(fitted, opportunity)  # noqa: SLF001
        view = _JudgeContext.from_mapping(fitted)
        record["estimated_prompt_tokens"] = estimator._estimate_tokens(  # noqa: SLF001
            estimator._build_messages(opportunity, view)  # noqa: SLF001
        )
    except ParticipationDecisionError as exc:
        record["estimated_prompt_tokens"] = None
        record["estimate_error"] = exc.reason
    record["group_notes_rendered"] = bool(fitted.get("group_notes_rendered_to_judge"))
    record["fitted_messages"] = len(fitted.get("messages") or ())
    return record


async def replay(
    opportunities: list[RecordedOpportunity],
    *,
    history: Any,
    builder: ParticipationContextBuilder,
    bounds: ParticipationContextBounds,
    estimator: ParticipationJudge,
    notes: ReplayNotes,
    judge: ParticipationJudge | None = None,
    usage: Callable[[], Mapping[str, Any]] | None = None,
    guidance_for: Callable[[str, str], str] = lambda channel, chat_id: "",
    set_as_of: Callable[[int], None] | None = None,
    concurrency: int = 1,
    judge_repeats: int = 1,
    tz: tzinfo | None = None,
) -> list[dict[str, Any]]:
    """One JSON-compatible record per opportunity: A (live context) and B (plus note).

    First every distinct chat-week note is resolved once (digest calls overlap up to
    ``concurrency``); then contexts are built one after another as of each opportunity;
    then only the Judge calls overlap. An opportunity without a ready note is skipped
    with the note's reason and costs no Judge call. With ``judge_repeats`` above 1 the B
    Judge decides that many times (majority decides, as in Spec A's replay).
    """
    repeats = max(1, min(int(judge_repeats), MAX_JUDGE_REPEATS))
    weeks: dict[str, DigestWeek] = {}
    for item in opportunities:
        if item.channel == "whatsapp" and item.source_ids and is_group_chat(item.channel, item.chat_id):
            weeks[item.opportunity_id] = note_week(item.at_ms, tz=tz)
    await notes.prepare(
        ((item.chat_id, weeks[item.opportunity_id]) for item in opportunities
         if item.opportunity_id in weeks),
        concurrency=concurrency,
    )
    records: list[dict[str, Any]] = []
    pending: list[tuple[dict[str, Any], ParticipationOpportunity, dict[str, Any]]] = []
    repeated: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for item in opportunities:
        record: dict[str, Any] = {
            "opportunity_id": item.opportunity_id, "attempt_id": item.attempt_id,
            "channel": item.channel, "chat_id": item.chat_id, "at_ms": item.at_ms,
            "live_outcome": item.live_outcome, "source_ids": list(item.source_ids),
            "trigger": item.trigger, "lane": item.lane,
        }
        records.append(record)
        if item.channel != "whatsapp" or not item.source_ids:
            record["skipped"] = "no_history_sources"
            continue
        if item.opportunity_id not in weeks:
            record["skipped"] = "not_group_chat"
            continue
        note = notes.get(item.chat_id, weeks[item.opportunity_id].label)
        record["note"] = note.meta()
        if note.status != "ready":
            record["skipped"] = note.skip_reason
            continue
        opportunity = _opportunity(item)
        if set_as_of is not None:
            set_as_of(item.at_ms)
        try:
            async with history_turn(history):
                a_record, context = await _build_variant(
                    builder, opportunity,
                    _inputs(bounds, guidance_for(item.channel, item.chat_id), item.source_ids),
                    at_ms=item.at_ms, estimator=estimator,
                )
        except HistoryPaused as exc:
            record["skipped"] = f"history_paused:{exc}"
            continue
        a_record.pop("message_ids", None)
        record["A"] = a_record
        if context is None:
            record["B"] = {"status": "context_error", "error": a_record.get("error")}
            continue
        b_context = _with_note(context, note)
        record["B"] = _note_variant(estimator, opportunity, b_context, a_record)
        if judge is None:
            continue
        pending.append((a_record, opportunity, context))
        if repeats > 1:
            runs: list[dict[str, Any]] = [{} for _ in range(repeats)]
            repeated.append((record["B"], runs))
            pending.extend((run, opportunity, _with_note(context, note)) for run in runs)
        else:
            pending.append((record["B"], opportunity, b_context))
    if judge is not None and pending:
        slots = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(job: tuple[dict[str, Any], ParticipationOpportunity, dict[str, Any]]) -> None:
            async with slots:
                await _judge_variant(*job, judge=judge, usage=usage)

        await asyncio.gather(*(_one(job) for job in pending))
    for variant, runs in repeated:
        _combine_repeats(variant, runs)
    return records


async def rebuild_views(
    record: Mapping[str, Any],
    note: ReplayNote,
    *,
    history: Any,
    builder: ParticipationContextBuilder,
    bounds: ParticipationContextBounds,
    estimator: ParticipationJudge,
    guidance_for: Callable[[str, str], str] = lambda channel, chat_id: "",
    set_as_of: Callable[[int], None] | None = None,
) -> dict[str, str]:
    """Both views of one record as the Judge saw them, for the owner's terminal only."""
    from yeoman_gateway.evaluation.participation_context_ab import record_opportunity

    item = record_opportunity(record)
    opportunity = _opportunity(item)
    if set_as_of is not None:
        set_as_of(item.at_ms)
    async with history_turn(history):
        try:
            context = await builder.build(
                opportunity,
                inputs=_inputs(bounds, guidance_for(item.channel, item.chat_id), item.source_ids),
                now_ms=item.at_ms,
            )
        except ParticipationDecisionError as exc:
            failed = f"(context error: {exc.reason})"
            return {"A": failed, "B": failed}
    views: dict[str, str] = {}
    for key, variant in (("A", dict(context)), ("B", _with_note(context, note))):
        try:
            views[key] = _fitted_view(estimator, opportunity, variant).render()
        except ParticipationDecisionError as exc:
            views[key] = f"(context error: {exc.reason})"
    return views


# ── summary and outputs ──────────────────────────────────────────────────────


def summarize(
    records: list[dict[str, Any]],
    notes: Iterable[ReplayNote],
    *,
    judged: bool,
    context_mode: str,
    seed: int = 0,
) -> dict[str, Any]:
    """Agreement, token delta, decision mix, skips and note counts. Ids and aggregates only."""
    note_list = list(notes)
    usable = {"built", "decided", "judge_error"}
    built = [r for r in records if r.get("A", {}).get("status") in usable
             and r.get("B", {}).get("status") in usable]
    estimates = [(r["A"].get("estimated_prompt_tokens"), r["B"].get("estimated_prompt_tokens"))
                 for r in built]
    estimates = [(a, b) for a, b in estimates if isinstance(a, int) and isinstance(b, int)]
    ready_weeks = [note for note in note_list if note.status == "ready"]
    needed = [note for note in note_list if note.status == "unavailable"]
    summary: dict[str, Any] = {
        "context_mode": context_mode,
        "opportunities": len(records),
        "skipped": dict(Counter(str(r["skipped"]).split(":", 1)[0] for r in records
                                if "skipped" in r)),
        "skipped_detail": dict(Counter(str(r["skipped"]) for r in records if "skipped" in r)),
        "notes": {
            "chat_weeks": len(note_list),
            "by_status": dict(Counter(note.status for note in note_list)),
            "by_source": dict(Counter(note.source for note in ready_weeks)),
            "failed_reasons": dict(Counter(note.reason for note in note_list
                                           if note.status == "failed")),
            "digest_calls": sum(note.model_calls for note in note_list),
            "digest_calls_needed": len(needed),
            "chars_mean": _mean([len(note.text) for note in ready_weeks]),
        },
        "context_errors": dict(Counter(r["A"].get("error") for r in records
                                       if r.get("A", {}).get("status") == "context_error")),
        "built_both": len(built),
        "estimated_tokens_mean": {"A": _mean([a for a, _ in estimates]),
                                  "B": _mean([b for _, b in estimates])},
        "estimated_token_delta_mean": _mean([b - a for a, b in estimates]),
        "note_rendered_share": (round(sum(bool(r["B"].get("group_notes_rendered"))
                                          for r in built) / len(built), 3) if built else None),
        "judged": judged,
    }
    if not judged:
        summary["judge_calls_needed"] = 2 * (
            len(built) + sum(1 for r in records if r.get("skipped") == "note_unavailable")
        )
        return summary
    decided = [r for r in built if r["A"]["status"] == "decided" and r["B"]["status"] == "decided"]
    disagreements = [r["opportunity_id"] for r in decided if r["A"]["action"] != r["B"]["action"]]
    prompt = [(r["A"].get("prompt_tokens"), r["B"].get("prompt_tokens")) for r in decided]
    prompt = [(a, b) for a, b in prompt if isinstance(a, int) and isinstance(b, int)]
    sample = (sorted(random.Random(seed).sample(disagreements, MAX_DISAGREEMENTS))
              if len(disagreements) > MAX_DISAGREEMENTS else disagreements)
    summary.update({
        "judge_calls": sum(1 + int(r["B"].get("judge_repeats") or 1) for r in built),
        "decided_both": len(decided),
        "judge_errors": {key: dict(Counter(r[key]["error"] for r in built
                                           if r[key]["status"] == "judge_error"))
                         for key in ("A", "B")},
        "agreement_rate": (round(1 - len(disagreements) / len(decided), 3) if decided else None),
        "actions": {key: dict(Counter(r[key]["action"] for r in decided)) for key in ("A", "B")},
        "decision_mix": dict(Counter(f"{r['A']['action']}->{r['B']['action']}" for r in decided)),
        "prompt_tokens_mean": {"A": _mean([a for a, _ in prompt]), "B": _mean([b for _, b in prompt])},
        "prompt_token_delta_mean": _mean([b - a for a, b in prompt]),
        "disagreement_count": len(disagreements),
        "disagreement_ids": sample,
    })
    repeated = [r["B"] for r in built if r["B"].get("judge_repeats")]
    if repeated:
        summary.update({
            "judge_repeats": max(int(variant["judge_repeats"]) for variant in repeated),
            "b_unanimous_share": round(
                sum(bool(variant.get("unanimous")) for variant in repeated) / len(repeated), 3
            ),
        })
    return summary


def read_notes(path: Path) -> dict[tuple[str, str], ReplayNote]:
    """The ready notes of a report's ``notes.jsonl``, by chat and week (source ``report``)."""
    notes: dict[tuple[str, str], ReplayNote] = {}
    if not path.is_file():
        return notes
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("status") != "ready" or not entry.get("text"):
            continue
        key = (str(entry["chat_id"]), str(entry["week"]))
        notes[key] = ReplayNote(
            chat_id=key[0], week=key[1], status="ready", source="report",
            digest_id=str(entry.get("digest_id") or ""), text=str(entry["text"]),
        )
    return notes


def render_markdown(summary: Mapping[str, Any], *, days: int,
                    source_report: str | None = None) -> str:
    period = (
        f"Opportunities: exactly those of report `{source_report}`."
        if source_report else f"Period: last {days} days."
    )
    lines = [
        "# Group notes replay (live context vs live context + group notes)",
        "",
        f"{period} A = context as live today ({summary.get('context_mode')}); B = A plus the "
        "note of the week before the opportunity's week (budget rules of `groupNotes: on`). "
        "Ids and aggregates only; label disagreements with "
        "`yeoman eval participation-groupnotes-review` (A better / B better / same).",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines.extend(
        f"| {key} | {json.dumps(value, ensure_ascii=False)} |"
        for key, value in summary.items() if key != "label_check"
    )
    check = summary.get("label_check")
    if isinstance(check, Mapping):
        lines.extend([
            "",
            f"Label check (new B against the source report's labels): estimated B better or "
            f"same {check['estimated_b_better_or_same_share']} of {check['checked']} checked "
            f"(threshold {check['threshold']:.0%}); {json.dumps(check['estimated'])}.",
        ])
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, records: list[dict[str, Any]], summary: Mapping[str, Any],
                  markdown: str, notes: Iterable[ReplayNote]) -> None:
    """Records and summary (ids only), and the run's notes in an owner-only file."""
    with (directory / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "summary.md").write_text(markdown, encoding="utf-8")
    path = directory / NOTES_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for note in notes:
            entry = {**note.meta(), "model_calls": note.model_calls}
            if note.status == "ready":
                entry["text"] = note.text
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def note_block(note: ReplayNote) -> str:
    return render_group_notes_block(note.week, note.text)


__all__ = [
    "NOTES_FILE",
    "REPORT_DIR",
    "MemoryDigestSink",
    "ReplayDigest",
    "ReplayNote",
    "ReplayNotes",
    "note_block",
    "note_week",
    "read_notes",
    "rebuild_views",
    "render_markdown",
    "replay",
    "stored_note_reader",
    "summarize",
    "write_outputs",
]
