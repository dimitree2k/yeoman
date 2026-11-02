"""I/O for the offline reply-state evaluation: as-of prompts, labels and the runner.

The pure pieces live in :mod:`yeoman_gateway.evaluation.reply_state`. Everything here
that opens a store opens it read-only, and nothing reads a database or a clock at
import time. Generation goes through the route clients directly, with no tools, and
every arm uses exactly the same client call shape.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import random
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from yeoman_gateway.adapters.reply_archive_history import (
    HistoryReplyArchiveAdapter,
    archive_row,
)
from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.evaluation.participation_context_ab import (
    RecordedOpportunity,
    _inputs,
    load_opportunities,
    read_only_source_readers,
    to_live_opportunity,
)
from yeoman_gateway.evaluation.reply_state import (
    DEFAULT_SEED,
    ReplyStateItem,
    SampleSelection,
    bootstrap_ci,
    build_grader_messages,
    build_placebo,
    grade_with_repair,
    merge_order,
    sample_items,
    simulated_label_cut,
)
from yeoman_gateway.group_state.as_of import build_state_as_of
from yeoman_gateway.group_state.contract import ContributorRegistry, ReaderContext
from yeoman_gateway.group_state.labels.parse import (
    LabelParseError,
    ParsedLabels,
    parse_labels,
)
from yeoman_gateway.group_state.labels.prompt import LabelInput, build_label_input
from yeoman_gateway.group_state.labels.runner import TTL_MS, _build_items
from yeoman_gateway.group_state.models import GroupState, Section
from yeoman_gateway.group_state.sections import default_contributors
from yeoman_gateway.group_state.views import (
    ViewTarget,
    _choose_conversation,
    _conversation_rows,
    render_reply_view,
    render_writer_view,
)
from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.conversations import ConversationBounds, read_conversations
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
)

REPORT_DIR: tuple[str, ...] = ("eval", "reply-state")
CANDIDATES_FILE = "candidates.jsonl"
ITEMS_FILE = "items.jsonl"
LABEL_CACHE_FILE = "label-cache.jsonl"
REPLY_ROUTE = "assistant.reply"
WRITER_ROUTE = "participation.writer"
#: A models.routes key must survive the config loader's camel/snake round trip, so it has
#: no underscore and no capital.
GRADER_ROUTE = "eval.grade"
DEFAULT_LABELS_ROUTE = "conversation.labels"
TRANSCRIPT_MESSAGES = 12
MAX_TRANSCRIPT_CHARS = 500
LABEL_MAX_TOKENS = 600
GRADER_MAX_TOKENS = 600
DEFAULT_GENERATION_TOKENS = 512

PAIR_ORDER: tuple[tuple[str, str, str], ...] = (
    ("S", "A", "S_vs_A"),
    ("A2", "A", "A2_vs_A"),
    ("P", "A", "P_vs_A"),
    ("S", "P", "S_vs_P"),
)
RECALL_PAIR_ORDER: tuple[tuple[str, str, str], ...] = (
    ("R", "A", "R_vs_A"),
    ("P_R", "A", "P_R_vs_A"),
    ("R", "P_R", "R_vs_P_R"),
)
ORACLE_PAIR = ("Sx", "A", "Sx_vs_A")
#: Every arm this offline runner can generate.  ``R`` and ``P_R`` are the recall
#: layer; the older state arms stay available unchanged.
ARMS: tuple[str, ...] = ("A", "A2", "S", "P", "Sx", "R", "P_R")
DEFAULT_ARMS: tuple[str, ...] = ("A", "A2", "R", "P_R")
MEASURE_FILE = "measure.json"
DELIVERED_EFFECT_STATES = frozenset({"sent", "delivered", "transport_accepted"})
DELIVERED_DISPOSITIONS = frozenset(
    {"comment_sent", "comment_delivered", "comment_transport_accepted"}
)


class ChatRoute(Protocol):
    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        temperature: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReplyRecord:
    """One past group turn, resolved from ``processing.db`` and history."""

    item: ReplyStateItem
    history_id: str
    native_id: str
    text: str
    reply_to_history_id: str | None = None
    reply_to_native_id: str | None = None
    #: The sender's roster principal as of the item, when membership proves it.
    sender_principal: str | None = None


@dataclass(frozen=True, slots=True)
class WriterRecord:
    """One past participation opportunity whose live decision was ``comment``."""

    item: ReplyStateItem
    opportunity: RecordedOpportunity
    target_history_id: str
    target_native_id: str
    text: str


@dataclass(frozen=True, slots=True)
class PlannedCalls:
    routes: Mapping[str, int]
    prompt_tokens: int
    completion_tokens: int
    items: int


@dataclass(frozen=True, slots=True)
class RunConfig:
    days: int = 30
    replies: int = 200
    writer: int = 80
    seed: int = DEFAULT_SEED
    oracle: bool = False
    limit_calls: int | None = None
    yes: bool = False
    #: Items in flight at once; each item still runs its own calls in order.
    concurrency: int = 1
    #: Arms to generate.  Old callers keep the four state arms; the CLI defaults to
    #: the recall layer.  ``Sx`` is only generated in oracle mode.
    arms: tuple[str, ...] = ("A", "A2", "S", "P")
    #: Reply sampling target for items that need context older than the window.
    older_context_share: float = 0.6


@dataclass(frozen=True, slots=True)
class PreparedRun:
    selection: SampleSelection
    replies: tuple[ReplyRecord, ...]
    writer: tuple[WriterRecord, ...]
    planned: PlannedCalls


@dataclass(frozen=True, slots=True)
class RunOutcome:
    planned: PlannedCalls
    summary: Mapping[str, Any] | None
    report_dir: Path | None
    partial: bool


@dataclass(frozen=True, slots=True)
class _CallResult:
    text: str
    usage: Mapping[str, int]
    latency_ms: int
    model: str
    error: str | None = None


@dataclass(slots=True)
class _PreparedReply:
    record: ReplyRecord
    base_state: GroupState
    conversation: Mapping[str, Any] | None
    simulated_input: LabelInput | None
    oracle_input: LabelInput | None
    label_time_ms: int | None
    transcript: str
    ambient: list[str]
    reply_text: str | None
    reply_participant: str | None
    base_messages: list[dict[str, str]]
    recall_text: str
    rendered_ms: list[float]


@dataclass(slots=True)
class _PreparedWriter:
    record: WriterRecord
    base_state: GroupState
    conversation: Mapping[str, Any] | None
    simulated_input: LabelInput | None
    oracle_input: LabelInput | None
    label_time_ms: int | None
    transcript: str
    context: dict[str, object]
    target: ViewTarget
    base_prompt: str
    rendered_ms: list[float]


@dataclass(slots=True)
class _CallBudget:
    limit: int | None
    used: int = 0
    by_route: Counter[str] = None  # type: ignore[assignment]
    exhausted: bool = False

    def __post_init__(self) -> None:
        if self.by_route is None:
            self.by_route = Counter()

    def take(self, route: str) -> bool:
        if self.limit is not None and self.used >= max(0, int(self.limit)):
            self.exhausted = True
            return False
        self.used += 1
        self.by_route[route] += 1
        return True


class LabelCache:
    """Input-hash cache of parsed label answers in one run directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("input_hash"), str):
                    self._entries[str(entry["input_hash"])] = entry

    def get(self, input_hash: str) -> tuple[ParsedLabels | None, Mapping[str, Any] | None]:
        entry = self._entries.get(str(input_hash))
        if entry is None:
            return None, None
        raw = entry.get("labels")
        if not isinstance(raw, Mapping):
            return None, None
        try:
            parsed = ParsedLabels(
                topic=raw.get("topic"),
                mood=raw.get("mood"),
                open_questions=tuple(dict(item) for item in raw.get("open_questions") or ()),
                plans=tuple(dict(item) for item in raw.get("plans") or ()),
                dropped=int(raw.get("dropped") or 0),
            )
        except TypeError, ValueError:
            return None, None
        usage = entry.get("usage") if isinstance(entry.get("usage"), Mapping) else {}
        return parsed, dict(usage)

    def put(
        self, input_hash: str, parsed: ParsedLabels, usage: Mapping[str, int], model: str
    ) -> None:
        entry = {
            "input_hash": str(input_hash),
            "labels": asdict_labels(parsed),
            "usage": {str(key): int(value) for key, value in usage.items()},
            "model": str(model),
        }
        self._entries[str(input_hash)] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def asdict_labels(parsed: ParsedLabels) -> dict[str, Any]:
    return {
        "topic": parsed.topic,
        "mood": parsed.mood,
        "open_questions": [dict(item) for item in parsed.open_questions],
        "plans": [dict(item) for item in parsed.plans],
        "dropped": int(parsed.dropped),
    }


def arm_messages(base_messages: Sequence[Mapping[str, Any]], block: str) -> list[dict[str, Any]]:
    """A copy of the base prompt with the declared block appended as the reply wiring does."""
    messages = copy.deepcopy([dict(item) for item in base_messages])
    if block and messages:
        content = messages[-1].get("content")
        if isinstance(content, str):
            messages[-1]["content"] = ContextBuilder._with_group_state(  # noqa: SLF001
                content, {"group_state_block": str(block)}
            )
    return messages


def build_reply_base_messages(
    builder: ContextBuilder,
    *,
    text: str,
    persona_text: str | None,
    channel: str,
    chat_id: str,
    ambient: Sequence[str],
    reply_to: Mapping[str, object] | None,
) -> list[dict[str, Any]]:
    """The live reply prompt as of the item, with an empty session history."""
    metadata: dict[str, Any] = {"ambient_context_window": list(ambient)}
    if reply_to:
        metadata.update(dict(reply_to))
    return builder.build_messages(
        history=[],
        current_message=str(text),
        current_metadata=metadata,
        persona_text=persona_text,
        channel=str(channel),
        chat_id=str(chat_id),
        allowed_tools=set(),
    )


def _writer_prompt(context: Mapping[str, object]) -> str:
    from yeoman_gateway.adapters.responder_llm import build_participation_draft_prompt

    return build_participation_draft_prompt(dict(context), purpose="")


def _percentile(values: Sequence[float | int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(float(fraction) * len(ordered)) - 1)
    return round(ordered[index], 1)


def _mean(values: Sequence[float | int]) -> float | None:
    return round(sum(float(value) for value in values) / len(values), 1) if values else None


def _distribution(values: Sequence[int]) -> dict[str, Any]:
    """Counts and percentiles of a bounded integer series; never any source text."""
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(int(value) for value in values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": _percentile(ordered, 0.5),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{Path(path).expanduser().resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _reply_rows(
    processing_db: Path, *, since_ms: int, until_ms: int, chat_ids: Sequence[str] | None
) -> list[dict[str, Any]]:
    if processing_db is None or not Path(processing_db).expanduser().is_file():
        return []
    connection = _read_only(processing_db)
    try:
        rows = connection.execute(
            """
            SELECT t.turn_id, th.chat_id, ts.source_message_id, t.opened_ms, g.outcome
            FROM turns t
            JOIN threads th ON th.thread_id = t.thread_id
            JOIN turn_sources ts ON ts.turn_id = t.turn_id
            LEFT JOIN generations g ON g.generation_id = t.last_generation_id
            WHERE th.chat_id LIKE '%@g.us' AND ts.role = 'trigger'
              AND ts.source_message_id IS NOT NULL AND ts.removed_ms IS NULL
              AND t.opened_ms >= ? AND t.opened_ms <= ?
            ORDER BY t.opened_ms, t.turn_id, ts.added_ms
            """,
            (int(since_ms), int(until_ms)),
        ).fetchall()
    finally:
        connection.close()
    wanted = None if chat_ids is None else {str(item) for item in chat_ids}
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        turn_id = str(row["turn_id"])
        if turn_id in seen:
            continue
        seen.add(turn_id)
        if wanted is not None and str(row["chat_id"]) not in wanted:
            continue
        result.append(dict(row))
    return result


def _reply_stratum(outcome: object) -> str | None:
    if str(outcome or "") == "send":
        return "addressed-answered"
    if outcome is None or str(outcome) in {"error", "restart"}:
        return "addressed-unanswered"
    return None


def _history_row(queries: HistoryQueries, chat_id: str, reference: str) -> dict[str, Any] | None:
    adapter = HistoryReplyArchiveAdapter(queries)
    return adapter.row("whatsapp", chat_id, reference)


def _human_participants(
    queries: HistoryQueries, chat_id: str, *, until_ms: int, assistant: set[str]
) -> set[str]:
    rows = queries.recent(chat_id=chat_id, limit=500, until_ms=int(until_ms))
    return {
        str(row.get("sender_contact_id") or "")
        for row in rows
        if row.get("direction") != "out"
        and row.get("sender_contact_id")
        and str(row.get("sender_contact_id")) not in assistant
    }


def _reply_is_hard(
    queries: HistoryQueries,
    *,
    chat_id: str,
    history_id: str,
    at_ms: int,
    reply_to_native_id: str | None,
    assistant: set[str],
) -> bool:
    if reply_to_native_id:
        target = queries.native_message(chat_id=chat_id, native_id=reply_to_native_id)
        before = queries.recent(chat_id=chat_id, limit=1, before_id=history_id)
        newest = before[-1] if before else None
        if target is not None and (newest is None or target["message_id"] != newest["message_id"]):
            return True
    return len(_human_participants(queries, chat_id, until_ms=at_ms - 1, assistant=assistant)) >= 3


def _sender_principal(
    queries: HistoryQueries, chat_id: str, sender_id: str | None, at_ms: int
) -> str | None:
    """The roster principal of the trigger sender, if membership proves it."""
    if not sender_id:
        return None
    audience = queries.members(chat_id=chat_id, at_ms=int(at_ms))
    if audience.status != "known" or not audience.members:
        return None
    return queries.member_principal(str(sender_id), frozenset(audience.members), at_ms=int(at_ms))


def _reply_outside_window(
    queries: HistoryQueries,
    *,
    chat_id: str,
    history_id: str,
    reply_to_native_id: str | None,
    ambient_limit: int,
) -> bool:
    """True when the reply target is not in the last ``ambient_limit`` messages."""
    if not reply_to_native_id or ambient_limit <= 0:
        return False
    target = queries.native_message(chat_id=chat_id, native_id=reply_to_native_id)
    if target is None:
        return False
    rows = queries.recent(chat_id=chat_id, limit=ambient_limit, before_id=history_id)
    return str(target["message_id"]) not in {str(row["message_id"]) for row in rows}


def _earlier_conversation_link(
    queries: HistoryQueries,
    *,
    chat_id: str,
    history_id: str,
    reply_to_native_id: str | None,
    at_ms: int,
    bounds: ConversationBounds,
) -> bool:
    """True when the reply points into a conversation that ended before this one."""
    if not reply_to_native_id:
        return False
    target = queries.native_message(chat_id=chat_id, native_id=reply_to_native_id)
    if target is None:
        return False
    conversations = read_conversations(queries, chat_id, int(at_ms), bounds)
    item_conversation = next(
        (item for item in conversations if history_id in item.message_ids), None
    )
    target_id = str(target["message_id"])
    target_conversation = next(
        (item for item in conversations if target_id in item.message_ids), None
    )
    if item_conversation is None or target_conversation is None:
        return False
    return bool(
        target_conversation.id != item_conversation.id
        and target_conversation.last_ms < item_conversation.start_ms
    )


async def load_reply_records(
    history: Any,
    processing_db: Path | None,
    *,
    since_ms: int,
    until_ms: int,
    chat_ids: Sequence[str] | None = None,
    ambient_limit: int = 30,
    lull_ms: int = 1_500_000,
    burst_n: int = 8,
    burst_ms: int = 900_000,
    window_ms: int = 86_400_000,
) -> list[ReplyRecord]:
    """Group turns from ``processing.db`` joined with their as-of history message."""
    if processing_db is None:
        return []
    rows = _reply_rows(
        Path(processing_db), since_ms=int(since_ms), until_ms=int(until_ms), chat_ids=chat_ids
    )
    result: list[ReplyRecord] = []
    async with history_turn(history) as snapshot:
        queries = HistoryQueries(snapshot)
        assistant = queries.assistant_contact_ids()
        bounds = ConversationBounds(
            window_ms=max(1, int(window_ms)),
            max_rows=500,
            lull_ms=max(1, int(lull_ms)),
            burst_n=max(1, int(burst_n)),
            burst_ms=max(1, int(burst_ms)),
        )
        for row in rows:
            stratum = _reply_stratum(row.get("outcome"))
            chat_id = str(row.get("chat_id") or "")
            reference = str(row.get("source_message_id") or "")
            if stratum is None or not chat_id or not reference:
                continue
            shaped = _history_row(queries, chat_id, reference)
            if shaped is None:
                continue
            history_id = str(shaped["event_id"])
            sent_ms = shaped.get("sent_ms")
            text = str(shaped.get("text") or "").strip()
            if sent_ms is None or not text:
                continue
            at_ms = int(sent_ms)
            reply_to_native = str(shaped.get("reply_to_native_id") or "") or None
            target = (
                _history_row(queries, chat_id, reply_to_native)
                if reply_to_native is not None
                else None
            )
            signals: list[str] = []
            if _reply_outside_window(
                queries,
                chat_id=chat_id,
                history_id=history_id,
                reply_to_native_id=reply_to_native,
                ambient_limit=int(ambient_limit),
            ):
                signals.append("reply_outside_window")
            if _earlier_conversation_link(
                queries,
                chat_id=chat_id,
                history_id=history_id,
                reply_to_native_id=reply_to_native,
                at_ms=at_ms,
                bounds=bounds,
            ):
                signals.append("earlier_conversation_link")
            result.append(
                ReplyRecord(
                    item=ReplyStateItem(
                        item_id=f"reply:{row['turn_id']}:{history_id}",
                        kind="reply",
                        chat_id=chat_id,
                        at_ms=at_ms,
                        stratum=stratum,
                        hard_case=_reply_is_hard(
                            queries,
                            chat_id=chat_id,
                            history_id=history_id,
                            at_ms=at_ms,
                            reply_to_native_id=reply_to_native,
                            assistant=assistant,
                        ),
                        older_context_signals=tuple(signals),
                    ),
                    history_id=history_id,
                    native_id=str(shaped.get("message_id") or ""),
                    text=text,
                    reply_to_history_id=str(target["event_id"]) if target else None,
                    reply_to_native_id=reply_to_native,
                    sender_principal=_sender_principal(
                        queries, chat_id, str(shaped.get("sender_id") or ""), at_ms
                    ),
                )
            )
    return result


def _effect_states(processing_db: Path | None) -> dict[str, set[str]]:
    if processing_db is None or not Path(processing_db).expanduser().is_file():
        return {}
    connection = _read_only(Path(processing_db))
    try:
        rows = connection.execute(
            """
            SELECT json_extract(pa.payload_json, '$.opportunity_id') AS opportunity_id,
                   e.state AS state
            FROM participation_admissions pa
            LEFT JOIN effects e ON e.admission_id = pa.admission_id
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    result: dict[str, set[str]] = {}
    for row in rows:
        opportunity_id = str(row["opportunity_id"] or "")
        if opportunity_id:
            result.setdefault(opportunity_id, set()).add(str(row["state"] or ""))
    return result


def _comment_dispositions(speakups_db: Path | None) -> dict[str, str]:
    if speakups_db is None or not Path(speakups_db).expanduser().is_file():
        return {}
    connection = _read_only(Path(speakups_db))
    try:
        rows = connection.execute(
            "SELECT opportunity_id, disposition FROM opportunity_dispositions"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {
        str(row["opportunity_id"]): str(row["disposition"] or "")
        for row in rows
        if str(row["disposition"] or "").startswith("comment_")
    }


def _comment_delivered(state: set[str], disposition: str) -> bool:
    return bool(state & DELIVERED_EFFECT_STATES) or disposition in DELIVERED_DISPOSITIONS


async def load_writer_records(
    history: Any,
    speakups_db: Path | None,
    processing_db: Path | None,
    *,
    since_ms: int,
    until_ms: int,
    chat_ids: Sequence[str] | None = None,
) -> list[WriterRecord]:
    """Participation ``comment`` opportunities, split by their recorded effect outcome."""
    if speakups_db is None or not Path(speakups_db).expanduser().is_file():
        return []
    opportunities = [
        item
        for item in load_opportunities(
            Path(speakups_db),
            since_ms=int(since_ms),
            until_ms=int(until_ms),
            chat_ids=chat_ids,
        )
        if item.live_outcome == "comment"
    ]
    effects = _effect_states(processing_db)
    dispositions = _comment_dispositions(Path(speakups_db))
    result: list[WriterRecord] = []
    async with history_turn(history) as snapshot:
        queries = HistoryQueries(snapshot)
        for opportunity in opportunities:
            reference = str(opportunity.source_ids[-1] if opportunity.source_ids else "")
            if not reference:
                continue
            shaped = _history_row(queries, opportunity.chat_id, reference)
            if shaped is None:
                continue
            text = str(shaped.get("text") or "").strip()
            if not text:
                continue
            delivered = _comment_delivered(
                effects.get(opportunity.opportunity_id, set()),
                dispositions.get(opportunity.opportunity_id, ""),
            )
            result.append(
                WriterRecord(
                    item=ReplyStateItem(
                        item_id=f"writer:{opportunity.opportunity_id}",
                        kind="writer",
                        chat_id=opportunity.chat_id,
                        at_ms=int(opportunity.at_ms),
                        stratum="comment-delivered" if delivered else "comment-failed",
                    ),
                    opportunity=opportunity,
                    target_history_id=str(shaped["event_id"]),
                    target_native_id=str(shaped.get("message_id") or ""),
                    text=text,
                )
            )
    return result


def _requested_arms(run: "RunConfig") -> tuple[str, ...]:
    """The arms this run generates: ``Sx`` joins automatically in oracle mode."""
    arms = list(run.arms)
    if run.oracle and "Sx" not in arms:
        arms.append("Sx")
    return tuple(arms)


def _pairs_for_arms(arms: Sequence[str], *, oracle: bool) -> list[tuple[str, str, str]]:
    """The pairs both of whose arms this run can actually generate."""
    available = set(arms)
    pairs = [
        pair
        for pair in (*PAIR_ORDER, *RECALL_PAIR_ORDER)
        if pair[0] in available and pair[1] in available
    ]
    if oracle and {"Sx", "A"} <= available:
        pairs.append(ORACLE_PAIR)
    return pairs


def _plan_calls(
    replies: int,
    writer: int,
    *,
    oracle: bool,
    labels_route: str,
    arms: Sequence[str] = ("A", "A2", "S", "P"),
) -> PlannedCalls:
    reply_arms = [arm for arm in arms if arm != "Sx" or oracle]
    # The writer already has group recall live, so ``R`` is not applicable there.
    writer_arms = [arm for arm in reply_arms if arm not in {"R", "P_R"}]
    reply_pairs = _pairs_for_arms(reply_arms, oracle=oracle)
    writer_pairs = _pairs_for_arms(writer_arms, oracle=oracle)
    total = replies + writer
    needs_labels = any(arm in {"S", "P", "Sx"} for arm in reply_arms)
    routes = {
        REPLY_ROUTE: replies * len(reply_arms),
        WRITER_ROUTE: writer * len(writer_arms),
        labels_route: total * (1 if needs_labels else 0) + total * (1 if oracle else 0),
        GRADER_ROUTE: 2 * (replies * len(reply_pairs) + writer * len(writer_pairs)),
    }
    prompt_tokens = (
        total * len(reply_arms) * 900
        + routes[labels_route] * LABEL_MAX_TOKENS
        + routes[GRADER_ROUTE] * 1_100
    )
    completion_tokens = (
        total * len(reply_arms) * 90 + routes[labels_route] * 160 + routes[GRADER_ROUTE] * 140
    )
    return PlannedCalls(
        routes={key: value for key, value in routes.items() if value},
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        items=total,
    )


def _profile_max_tokens(config: Any, route: str, default: int) -> int:
    models = getattr(config, "models", None)
    routes = getattr(models, "routes", None) or {}
    profiles = getattr(models, "profiles", None) or {}
    profile = profiles.get(str(routes.get(route) or ""))
    value = getattr(profile, "max_tokens", None) if profile is not None else None
    return int(value) if isinstance(value, int) and value > 0 else int(default)


def _profile_temperature(config: Any, route: str) -> float | None:
    models = getattr(config, "models", None)
    routes = getattr(models, "routes", None) or {}
    profiles = getattr(models, "profiles", None) or {}
    profile = profiles.get(str(routes.get(route) or ""))
    value = getattr(profile, "temperature", None) if profile is not None else None
    return float(value) if isinstance(value, (int, float)) else None


class ReplyStateRunner:
    """One offline run over preloaded or freshly loaded as-of items."""

    def __init__(
        self,
        *,
        config: Any,
        history: Any,
        context_builder: ContextBuilder,
        clients: Mapping[str, ChatRoute] | None = None,
        participation_builder: ParticipationContextBuilder | None = None,
        bounds: ParticipationContextBounds | None = None,
        guidance_for: Callable[[str, str], str] = lambda channel, chat_id: "",
        persona_for: Callable[[str, str], str | None] = lambda channel, chat_id: None,
        processing_db: Path | None = None,
        speakups_db: Path | None = None,
        decisions_db: Path | None = None,
        knowledge: Any | None = None,
        people: Any | None = None,
        reply_records: Sequence[ReplyRecord] | None = None,
        writer_records: Sequence[WriterRecord] | None = None,
    ) -> None:
        self._config = config
        self._history = history
        self._context_builder = context_builder
        self._clients = dict(clients or {})
        self._participation_builder = participation_builder
        self._bounds = bounds
        self._guidance_for = guidance_for
        self._persona_for = persona_for
        self._processing_db = processing_db
        self._speakups_db = speakups_db
        self._decisions_db = decisions_db
        self._knowledge = knowledge
        self._people = people
        self._reply_records = None if reply_records is None else tuple(reply_records)
        self._writer_records = None if writer_records is None else tuple(writer_records)
        self._group_state = getattr(getattr(config, "processing", None), "group_state", None)
        self._labels = getattr(self._group_state, "labels", None)
        self._consciousness = getattr(config, "consciousness", None)
        self._lull_ms = (
            max(1, int(getattr(self._consciousness, "lull_silence_minutes", 25))) * 60_000
        )
        self._labels_route = str(
            getattr(self._labels, "route", DEFAULT_LABELS_ROUTE) or DEFAULT_LABELS_ROUTE
        )
        self._registry = ContributorRegistry()
        for contributor in default_contributors():
            self._registry.register(contributor)
        self._sources = read_only_source_readers(
            speakups_db=self._speakups_db,
            processing_db=self._processing_db,
            decisions_db=self._decisions_db,
        )
        self._budget = _CallBudget(limit=None)
        self._label_calls = 0
        self._label_prompt_tokens = 0
        self._label_completion_tokens = 0
        self._label_failures = 0
        self._label_cache_hits = 0
        self._oracle_label_calls = 0
        self._oracle_label_prompt_tokens = 0
        self._oracle_label_completion_tokens = 0
        self._partial = False
        self._prepare_failures: Counter[str] = Counter()
        self._generation_errors: Counter[str] = Counter()
        self._candidates: list[dict[str, Any]] = []
        self._grades: list[dict[str, Any]] = []
        self._items: list[dict[str, Any]] = []
        self._render_ms: list[float] = []
        self._s_items_with_derived = 0
        self._items_with_s_view = 0
        self._items_total = 0
        self._oracle = False
        self._run_arms: tuple[str, ...] = ("A", "A2", "S", "P")

    # -- loading and planning ---------------------------------------------------------

    def set_clients(self, clients: Mapping[str, ChatRoute]) -> None:
        """Attach the route clients after the cost guard has accepted the plan."""
        self._clients = dict(clients)

    async def prepare(
        self, run: RunConfig, *, chat_ids: Sequence[str] | None = None
    ) -> PreparedRun:
        """Load (or use preloaded) records and take the seeded sample; no model call."""
        if self._reply_records is None or self._writer_records is None:
            now_ms = int(time.time() * 1000)
            since_ms = now_ms - max(1, int(run.days)) * 86_400_000
            connection = getattr(getattr(self._config, "channels", None), "whatsapp", None)
            consciousness = getattr(self._config, "consciousness", None)
            replies = (
                await load_reply_records(
                    self._history,
                    self._processing_db,
                    since_ms=since_ms,
                    until_ms=now_ms,
                    chat_ids=chat_ids,
                    ambient_limit=max(0, int(getattr(connection, "ambient_window_limit", 30))),
                    lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25)))
                    * 60_000,
                    burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
                    burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15)))
                    * 60_000,
                    window_ms=max(1, int(getattr(self._group_state, "window_hours", 24)))
                    * 3_600_000,
                )
                if self._reply_records is None
                else list(self._reply_records)
            )
            writer = (
                await load_writer_records(
                    self._history,
                    self._speakups_db,
                    self._processing_db,
                    since_ms=since_ms,
                    until_ms=now_ms,
                    chat_ids=chat_ids,
                )
                if self._writer_records is None
                else list(self._writer_records)
            )
        else:
            replies = list(self._reply_records)
            writer = list(self._writer_records)
        replies = await self._annotate_person_history(replies)
        by_id = {record.item.item_id: record for record in replies}
        writer_by_id = {record.item.item_id: record for record in writer}
        selection = sample_items(
            [record.item for record in replies] + [record.item for record in writer],
            replies=run.replies,
            writer=run.writer,
            seed=run.seed,
            older_context_share=run.older_context_share,
        )
        chosen_replies = tuple(
            by_id[item.item_id] for item in selection.items if item.kind == "reply"
        )
        chosen_writer = tuple(
            writer_by_id[item.item_id] for item in selection.items if item.kind == "writer"
        )
        planned = _plan_calls(
            len(chosen_replies),
            len(chosen_writer),
            oracle=run.oracle,
            labels_route=self._labels_route,
            arms=_requested_arms(run),
        )
        return PreparedRun(
            selection=selection,
            replies=chosen_replies,
            writer=chosen_writer,
            planned=planned,
        )

    async def _annotate_person_history(self, records: Sequence[ReplyRecord]) -> list[ReplyRecord]:
        """Add the Knowledge-based ``person_with_history`` signal before sampling.

        This is a read through the same gate as a reply: group membership must be
        proven at the item time, and only a statement readable by that context counts.
        """
        if self._knowledge is None or not records:
            return list(records)
        from yeoman_gateway.adapters.responder_llm import build_group_read_context
        from yeoman_gateway.history.context import history_knowledge_scope
        from yeoman_gateway.knowledge.models import RecallQuery

        annotated: list[ReplyRecord] = []
        async with history_turn(self._history) as snapshot:
            with history_knowledge_scope(snapshot, self._knowledge):
                queries = HistoryQueries(snapshot)
                for record in records:
                    if (
                        "person_with_history" in record.item.older_context_signals
                        or not record.sender_principal
                    ):
                        annotated.append(record)
                        continue
                    context = build_group_read_context(
                        queries=queries,
                        channel="whatsapp",
                        chat_id=record.item.chat_id,
                        principal=record.sender_principal,
                        is_owner=False,
                        at_ms=record.item.at_ms,
                        policy_revision=int(self._knowledge.policy_revision),
                        purpose="reply",
                    )
                    found = False
                    if context is not None:
                        person_id = self._knowledge.person_for_principal(record.sender_principal)
                        if person_id:
                            statements = self._knowledge.recall(
                                RecallQuery(
                                    person_ids=(person_id,),
                                    limit=1,
                                    as_of_ms=record.item.at_ms,
                                ),
                                context=context,
                                group_wide=True,
                            )
                            found = bool(statements.statement_ids)
                    if found:
                        record = replace(
                            record,
                            item=replace(
                                record.item,
                                older_context_signals=(
                                    *record.item.older_context_signals,
                                    "person_with_history",
                                ),
                            ),
                        )
                    annotated.append(record)
        return annotated

    async def run(
        self,
        run: RunConfig,
        *,
        chat_ids: Sequence[str] | None = None,
        report_dir: Path | None = None,
    ) -> RunOutcome:
        prepared = await self.prepare(run, chat_ids=chat_ids)
        if not run.yes:
            return RunOutcome(
                planned=prepared.planned, summary=None, report_dir=None, partial=False
            )
        return await self.execute(prepared, run, report_dir=report_dir)

    # -- measurement only (no generation, no grading) ---------------------------------

    async def measure(
        self,
        prepared: PreparedRun,
        run: RunConfig,
        *,
        report_dir: Path | None = None,
    ) -> RunOutcome:
        """Count as-of recall availability per stratum; never calls a model."""
        report_dir = Path(report_dir) if report_dir is not None else self._report_dir()
        report_dir.mkdir(parents=True, exist_ok=True)
        self._reset_run_state(limit=None, oracle=False, arms=_requested_arms(run))
        rows: list[dict[str, Any]] = []
        from yeoman_gateway.history.context import history_knowledge_scope

        async with history_turn(self._history) as snapshot:
            queries = HistoryQueries(snapshot)
            with (
                history_knowledge_scope(snapshot, self._knowledge)
                if self._knowledge is not None
                else nullcontext()
            ):
                for record in prepared.replies:
                    rows.append(
                        self._measure_record(
                            queries,
                            item=record.item,
                            text=record.text,
                            principal=record.sender_principal,
                        )
                    )
                for record in prepared.writer:
                    shaped = _history_row(
                        queries,
                        record.item.chat_id,
                        record.target_native_id or record.target_history_id,
                    )
                    principal = (
                        _sender_principal(
                            queries,
                            record.item.chat_id,
                            str(shaped.get("sender_id") or ""),
                            record.item.at_ms,
                        )
                        if shaped is not None
                        else None
                    )
                    rows.append(
                        self._measure_record(
                            queries,
                            item=record.item,
                            text=record.text,
                            principal=principal,
                        )
                    )
        summary = self._measure_summary(rows, prepared)
        descriptor = os.open(
            report_dir / MEASURE_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return RunOutcome(
            planned=prepared.planned,
            summary=summary,
            report_dir=report_dir,
            partial=False,
        )

    def _measure_record(
        self,
        queries: HistoryQueries,
        *,
        item: ReplyStateItem,
        text: str,
        principal: str | None,
    ) -> dict[str, Any]:
        from yeoman_gateway.adapters.responder_llm import build_group_read_context
        from yeoman_gateway.knowledge.models import RecallQuery

        recall_hit = False
        turn_person_hit = False
        statements = 0
        if self._knowledge is not None and principal:
            context = build_group_read_context(
                queries=queries,
                channel="whatsapp",
                chat_id=item.chat_id,
                principal=principal,
                is_owner=False,
                at_ms=item.at_ms,
                policy_revision=int(self._knowledge.policy_revision),
                purpose="reply",
            )
            if context is not None:
                recall = self._knowledge.recall(
                    RecallQuery(text=text, limit=5, as_of_ms=item.at_ms),
                    context=context,
                    group_wide=True,
                    require_match=True,
                )
                recall_hit = bool(recall.statement_ids)
                person_id = self._knowledge.person_for_principal(principal)
                if person_id:
                    result = self._knowledge.recall(
                        RecallQuery(
                            person_ids=(person_id,),
                            limit=50,
                            as_of_ms=item.at_ms,
                        ),
                        context=context,
                        group_wide=True,
                    )
                    statements = len(result.statement_ids)
                    turn_person_hit = bool(statements)
        return {
            "item_id": item.item_id,
            "kind": item.kind,
            "stratum": item.stratum,
            "older_context": item.older_context,
            "signals": list(item.older_context_signals),
            "as_of_recall_hit": recall_hit,
            "turn_person_statement_hit": turn_person_hit,
            "statements": statements,
        }

    def _measure_summary(
        self, rows: Sequence[Mapping[str, Any]], prepared: PreparedRun
    ) -> dict[str, Any]:
        strata = sorted({str(row["stratum"]) for row in rows})
        statements = [int(row["statements"]) for row in rows]
        return {
            "report_schema": 1,
            "measure": True,
            "read_only": True,
            "no_generation": True,
            "no_text": True,
            "items": {
                "selected": len(rows),
                "reply": len(prepared.replies),
                "writer": len(prepared.writer),
                "requested": dict(prepared.selection.requested),
                "available": dict(prepared.selection.available),
                "shortfall": dict(prepared.selection.shortfall),
                "older_context": sum(row["older_context"] for row in rows),
            },
            "overall": self._measure_stats(rows),
            "strata": {
                stratum: self._measure_stats([row for row in rows if row["stratum"] == stratum])
                for stratum in strata
            },
            "statements_per_turn_person": _distribution(statements),
        }

    @staticmethod
    def _measure_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        total = len(rows)

        def share(key: str) -> float | None:
            if not total:
                return None
            return round(sum(bool(row[key]) for row in rows) / total, 3)

        return {
            "items": total,
            "as_of_recall_hit": sum(bool(row["as_of_recall_hit"]) for row in rows),
            "as_of_recall_hit_share": share("as_of_recall_hit"),
            "turn_person_statement_hit": sum(
                bool(row["turn_person_statement_hit"]) for row in rows
            ),
            "turn_person_statement_share": share("turn_person_statement_hit"),
            "earlier_conversation_link": sum(
                "earlier_conversation_link" in row["signals"] for row in rows
            ),
            "earlier_conversation_link_share": (
                round(
                    sum("earlier_conversation_link" in row["signals"] for row in rows) / total,
                    3,
                )
                if total
                else None
            ),
            "older_context": sum(bool(row["older_context"]) for row in rows),
        }

    async def _state(self, queries: HistoryQueries, chat_id: str, at_ms: int) -> GroupState:
        return await build_state_as_of(
            registry=self._registry,
            channel="whatsapp",
            chat_id=chat_id,
            at_ms=int(at_ms),
            history=queries,
            sources=self._sources,
            settings=self._group_state,
            consciousness=self._consciousness,
        )

    def _conversation(
        self, state: GroupState, target: ViewTarget, at_ms: int
    ) -> Mapping[str, Any] | None:
        return _choose_conversation(
            _conversation_rows(state), target, now_ms=int(at_ms), lull_ms=self._lull_ms
        )

    def _label_inputs(
        self,
        queries: HistoryQueries,
        state: GroupState,
        target: ViewTarget,
        at_ms: int,
    ) -> tuple[LabelInput | None, LabelInput | None, int | None]:
        conversation = self._conversation(state, target, at_ms)
        if conversation is None or self._labels is None:
            return None, None, None
        conversation_id = str(conversation.get("conversation_id") or "")
        start_ms = int(conversation.get("start_ms") or 0)
        rows = [
            row
            for row in queries.recent(
                chat_id=str(state.chat_id),
                limit=500,
                after_ms=start_ms - 1,
                until_ms=int(at_ms) - 1,
            )
            if row.get("sent_ms") is not None
        ]
        if not rows:
            return None, None, None
        assistant = queries.assistant_contact_ids()

        def build(subset: Sequence[Mapping[str, Any]]) -> LabelInput | None:
            return build_label_input(
                list(subset),
                conversation_id=conversation_id,
                assistant_ids=assistant,
                fingerprint_of=queries.content_fingerprint,
                max_chars=int(getattr(self._labels, "max_input_chars", 6_000)),
                min_messages=int(getattr(self._labels, "min_messages", 4)),
            )

        times = [int(row["sent_ms"]) for row in rows]
        cut = simulated_label_cut(
            times,
            t=int(at_ms),
            refresh_after=int(getattr(self._labels, "refresh_after_messages", 30)),
        )
        simulated = build(rows[: cut + 1]) if cut is not None else None
        produced_at = int(rows[cut]["sent_ms"]) if cut is not None else None
        oracle = build(rows)
        return simulated, oracle, produced_at

    def _label_section(
        self,
        label_input: LabelInput | None,
        parsed: ParsedLabels | None,
        produced_at_ms: int,
    ) -> Section | None:
        if label_input is None or parsed is None:
            return None
        items = _build_items(label_input, parsed, int(produced_at_ms))
        if not items:
            return None
        sources = tuple(dict.fromkeys(source for item in items for source in item.sources))
        message_ids = tuple(label_input.message_refs.values())
        return Section(
            key="conversation_labels",
            schema_version=1,
            trust="derived",
            layer="conversation",
            produced_at_ms=int(produced_at_ms),
            ttl_ms=TTL_MS,
            source_cursor=message_ids[-1] if message_ids else "",
            period_start_ms=None,
            period_end_ms=int(produced_at_ms),
            sources=sources,
            data={},
            items=items,
        )

    @staticmethod
    def _with_labels(state: GroupState, section: Section | None) -> GroupState:
        if section is None:
            return state
        return replace(state, sections={**state.sections, "conversation_labels": section})

    def _ambient(
        self, queries: HistoryQueries, chat_id: str, reference: str, at_ms: int
    ) -> list[str]:
        connection = getattr(getattr(self._config, "channels", None), "whatsapp", None)
        limit = max(0, int(getattr(connection, "ambient_window_limit", 30)))
        if limit <= 0:
            return []
        middleware = ReplyContextMiddleware(
            reply_context_window_limit=int(getattr(connection, "reply_context_window_limit", 6)),
            reply_context_line_max_chars=int(
                getattr(connection, "reply_context_line_max_chars", 256)
            ),
            ambient_window_limit=limit,
        )
        adapter = HistoryReplyArchiveAdapter(queries)
        rows = [
            row
            for row in adapter.lookup_messages_before("whatsapp", chat_id, reference, limit=limit)
            if row.timestamp is None or int(row.timestamp) * 1000 < int(at_ms)
        ]
        return middleware._format_lines(rows)  # noqa: SLF001 - the live renderer, not a copy

    def _reply_context_window(self, queries: HistoryQueries, record: ReplyRecord) -> list[str]:
        if record.reply_to_history_id is None and record.reply_to_native_id is None:
            return []
        connection = getattr(getattr(self._config, "channels", None), "whatsapp", None)
        middleware = ReplyContextMiddleware(
            reply_context_window_limit=int(getattr(connection, "reply_context_window_limit", 6)),
            reply_context_line_max_chars=int(
                getattr(connection, "reply_context_line_max_chars", 256)
            ),
            ambient_window_limit=max(0, int(getattr(connection, "ambient_window_limit", 30))),
        )
        adapter = HistoryReplyArchiveAdapter(queries)
        rows = [
            row
            for row in adapter.lookup_messages_before(
                "whatsapp",
                record.item.chat_id,
                record.reply_to_history_id or record.reply_to_native_id or "",
                limit=int(getattr(connection, "reply_context_window_limit", 6)),
            )
            if row.timestamp is None or int(row.timestamp) * 1000 < int(record.item.at_ms)
        ]
        return middleware._format_lines(rows)  # noqa: SLF001 - the live renderer, not a copy

    def _reply_target(
        self, queries: HistoryQueries, record: ReplyRecord
    ) -> tuple[str | None, str | None]:
        if record.reply_to_history_id is None and record.reply_to_native_id is None:
            return None, None
        adapter = HistoryReplyArchiveAdapter(queries)
        target = adapter.lookup_message(
            "whatsapp",
            record.item.chat_id,
            record.reply_to_native_id or record.reply_to_history_id or "",
        )
        if target is None:
            return None, None
        return target.text or None, target.sender_name or target.sender_id or None

    def _transcript(
        self, queries: HistoryQueries, chat_id: str, history_id: str, at_ms: int
    ) -> str:
        item = queries.message(history_id)
        if item is None:
            item = queries.native_message(chat_id=chat_id, native_id=history_id)
        rows = queries.recent(chat_id=chat_id, limit=TRANSCRIPT_MESSAGES, until_ms=int(at_ms) - 1)
        if item is not None and str(item["message_id"]) not in {
            str(row["message_id"]) for row in rows
        }:
            rows.append(item)
            rows.sort(key=lambda row: (row.get("sent_ms") or 0, str(row["message_id"])))
        lines: list[str] = []
        for row in rows:
            shaped = archive_row(queries, row)
            text = " ".join(str(shaped.get("text") or "").split())
            if not text:
                continue
            sender = str(shaped.get("sender_name") or shaped.get("sender_id") or "unknown")
            lines.append(f"[{sender}] {text[:MAX_TRANSCRIPT_CHARS]}")
        return "\n".join(lines)

    def _recall_text(self, queries: HistoryQueries, record: ReplyRecord) -> str:
        """The live reply recall block, built as of the item and nothing later."""
        if self._knowledge is None or not record.sender_principal:
            return ""
        from yeoman_gateway.adapters.responder_llm import build_group_read_context
        from yeoman_gateway.knowledge.models import RecallQuery

        context = build_group_read_context(
            queries=queries,
            channel="whatsapp",
            chat_id=record.item.chat_id,
            principal=record.sender_principal,
            is_owner=False,
            at_ms=record.item.at_ms,
            policy_revision=int(self._knowledge.policy_revision),
            purpose="reply",
        )
        if context is None:
            return ""
        result = self._knowledge.recall(
            RecallQuery(
                text=record.text,
                limit=5,
                as_of_ms=record.item.at_ms,
            ),
            context=context,
            group_wide=True,
            require_match=True,
        )
        return str(result.text)

    async def _prepare_reply(self, record: ReplyRecord) -> _PreparedReply | None:
        target = ViewTarget(
            message_id=record.history_id,
            reply_to_message_id=record.reply_to_history_id,
        )
        try:
            async with history_turn(self._history) as snapshot:
                queries = HistoryQueries(snapshot)
                from yeoman_gateway.history.context import history_knowledge_scope

                if self._knowledge is None:
                    recall_text = ""
                else:
                    with history_knowledge_scope(snapshot, self._knowledge):
                        recall_text = self._recall_text(queries, record)
                base_state = await self._state(queries, record.item.chat_id, record.item.at_ms)
                conversation = self._conversation(base_state, target, record.item.at_ms)
                simulated, oracle, label_time = self._label_inputs(
                    queries, base_state, target, record.item.at_ms
                )
                ambient = self._ambient(
                    queries, record.item.chat_id, record.history_id, record.item.at_ms
                )
                reply_text, participant = self._reply_target(queries, record)
                reply_to: dict[str, object] = {}
                if record.reply_to_native_id:
                    reply_to["reply_to_message_id"] = record.reply_to_native_id
                if reply_text:
                    reply_to["reply_to_text"] = reply_text
                    reply_to["reply_context_source"] = "history"
                if participant:
                    reply_to["reply_to_participant"] = participant
                window = self._reply_context_window(queries, record)
                if window:
                    reply_to["reply_context_window"] = window
                    reply_to["reply_context_source"] = "history"
                base_messages = build_reply_base_messages(
                    self._context_builder,
                    text=record.text,
                    persona_text=self._persona_for("whatsapp", record.item.chat_id),
                    channel="whatsapp",
                    chat_id=record.item.chat_id,
                    ambient=ambient,
                    reply_to=reply_to,
                )
                transcript = self._transcript(
                    queries, record.item.chat_id, record.history_id, record.item.at_ms
                )
        except Exception as exc:  # noqa: BLE001 - one item must not stop the run
            logger.warning(
                "reply_state_prepare_failed kind=reply item={} error_type={}",
                record.item.item_id,
                type(exc).__name__,
            )
            self._prepare_failures[type(exc).__name__] += 1
            return None
        return _PreparedReply(
            record=record,
            base_state=base_state,
            conversation=conversation,
            simulated_input=simulated,
            oracle_input=oracle,
            label_time_ms=label_time,
            transcript=transcript,
            ambient=ambient,
            reply_text=reply_text,
            reply_participant=participant,
            base_messages=base_messages,
            recall_text=recall_text,
            rendered_ms=[],
        )

    async def _prepare_writer(self, record: WriterRecord) -> _PreparedWriter | None:
        target = ViewTarget(message_id=record.target_history_id, reply_to_message_id=None)
        try:
            async with history_turn(self._history) as snapshot:
                queries = HistoryQueries(snapshot)
                base_state = await self._state(queries, record.item.chat_id, record.item.at_ms)
                conversation = self._conversation(base_state, target, record.item.at_ms)
                simulated, oracle, label_time = self._label_inputs(
                    queries, base_state, target, record.item.at_ms
                )
                if self._participation_builder is None or self._bounds is None:
                    return None
                context = await self._participation_builder.build(
                    to_live_opportunity(record.opportunity),
                    inputs=_inputs(
                        self._bounds,
                        self._guidance_for("whatsapp", record.item.chat_id),
                        record.opportunity.source_ids,
                    ),
                    now_ms=int(record.item.at_ms),
                )
                transcript = self._transcript(
                    queries, record.item.chat_id, record.target_history_id, record.item.at_ms
                )
            # Live sets the judge's chosen target right before drafting, as the id the
            # judge saw in the context (the native message id); the recorded target
            # stands in for it here.
            context = dict(context)
            context["target_message_id"] = record.target_native_id or record.target_history_id
            base_prompt = _writer_prompt(context)
        except Exception as exc:  # noqa: BLE001 - one item must not stop the run
            logger.warning(
                "reply_state_prepare_failed kind=writer item={} error_type={}",
                record.item.item_id,
                type(exc).__name__,
            )
            self._prepare_failures[type(exc).__name__] += 1
            return None
        return _PreparedWriter(
            record=record,
            base_state=base_state,
            conversation=conversation,
            simulated_input=simulated,
            oracle_input=oracle,
            label_time_ms=label_time,
            transcript=transcript,
            context=context,
            target=target,
            base_prompt=base_prompt,
            rendered_ms=[],
        )

    # -- model calls ------------------------------------------------------------------

    async def _call(
        self,
        route: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> _CallResult:
        client = self._clients.get(route)
        if client is None:
            return _CallResult("", {}, 0, "", "route_unavailable")
        started = time.perf_counter()
        try:
            reply = await client.chat_with_usage(
                messages,
                max_tokens=int(max_tokens),
                response_format=response_format,
                temperature=temperature,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failed call is a bounded item error
            logger.warning(
                "reply_state_call_failed route={} error_type={}", route, type(exc).__name__
            )
            return _CallResult(
                "", {}, int((time.perf_counter() - started) * 1000), "", "call_failed"
            )
        usage = getattr(reply, "usage", None) or {}
        if not isinstance(usage, Mapping):
            usage = {}
        return _CallResult(
            text=str(getattr(reply, "content", "") or ""),
            usage={
                str(key): int(value)
                for key, value in usage.items()
                if isinstance(value, int) and not isinstance(value, bool)
            },
            latency_ms=int(getattr(reply, "latency_ms", 0) or 0),
            model=str(getattr(reply, "model", "") or getattr(client, "model", "") or ""),
        )

    async def _label(
        self,
        label_input: LabelInput | None,
        cache: LabelCache,
        *,
        oracle: bool,
    ) -> tuple[ParsedLabels | None, LabelInput | None]:
        if label_input is None:
            return None, None
        parsed, usage = cache.get(label_input.input_hash)
        if parsed is not None:
            self._label_cache_hits += 1
            return parsed, label_input
        if not self._budget.take(self._labels_route):
            return None, label_input
        result = await self._call(
            self._labels_route,
            label_input.messages,
            max_tokens=LABEL_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        if oracle:
            self._oracle_label_calls += 1
            self._oracle_label_prompt_tokens += int(result.usage.get("prompt_tokens", 0))
            self._oracle_label_completion_tokens += int(result.usage.get("completion_tokens", 0))
        else:
            self._label_calls += 1
            self._label_prompt_tokens += int(result.usage.get("prompt_tokens", 0))
            self._label_completion_tokens += int(result.usage.get("completion_tokens", 0))
        if result.error:
            self._label_failures += 1
            return None, label_input
        try:
            parsed = parse_labels(result.text, label_input)
        except LabelParseError:
            self._label_failures += 1
            return None, label_input
        cache.put(label_input.input_hash, parsed, result.usage, result.model)
        return parsed, label_input

    # -- one item ---------------------------------------------------------------------

    def _render(
        self,
        state: GroupState,
        target: ViewTarget,
        *,
        chat_id: str,
        at_ms: int,
        writer: bool,
    ) -> tuple[str, tuple[str, ...], float]:
        views = getattr(self._group_state, "views", None)
        settings = getattr(views, "writer" if writer else "reply", None)
        renderer = render_writer_view if writer else render_reply_view
        started = time.perf_counter()
        rendered = renderer(
            state,
            reader=ReaderContext(
                principal="arvid",
                is_owner=False,
                channel="whatsapp",
                chat_id=chat_id,
                audience="group",
            ),
            target=target,
            now_ms=int(at_ms),
            max_chars=int(getattr(settings, "max_chars", 700)),
            max_age_ms=int(getattr(settings, "max_state_age_seconds", 900)) * 1000,
            lull_ms=self._lull_ms,
        )
        elapsed = (time.perf_counter() - started) * 1000
        return rendered.text, tuple(rendered.items), elapsed

    async def _generate(
        self, item_id: str, arm: str, route: str, messages: list[dict[str, str]]
    ) -> _CallResult:
        if not self._budget.take(route):
            return _CallResult("", {}, 0, "", "budget_exhausted")
        # Candidates are sampled like live text, at the profile's temperature; only the
        # grader and the labeler stay at 0.
        temperature = _profile_temperature(self._config, route)
        result = await self._call(
            route,
            messages,
            max_tokens=_profile_max_tokens(self._config, route, DEFAULT_GENERATION_TOKENS),
            temperature=temperature,
        )
        self._candidates.append(
            {
                "item_id": item_id,
                "arm": arm,
                "text": result.text,
                "route": route,
                "model": result.model,
                "prompt_tokens": int(result.usage.get("prompt_tokens", 0)),
                "completion_tokens": int(result.usage.get("completion_tokens", 0)),
                "latency_ms": int(result.latency_ms),
                "temperature_configured": temperature,
                "temperature_used": 0.0 if temperature is None else temperature,
                "error": result.error,
            }
        )
        if result.error:
            self._generation_errors[result.error] += 1
        return result

    async def _grade_pair(
        self,
        *,
        pair: str,
        first_arm: str,
        second_arm: str,
        item_id: str,
        stratum: str,
        hard_case: bool,
        transcript: str,
        candidates: Mapping[str, _CallResult],
    ) -> dict[str, Any] | None:
        first = candidates.get(first_arm)
        second = candidates.get(second_arm)
        if first is None or second is None or not first.text.strip() or not second.text.strip():
            return None
        sides: list[dict[str, Any]] = []
        preferences: list[str] = []
        for order, (one, two) in enumerate(((first, second), (second, first)), start=1):
            messages = build_grader_messages(
                transcript=transcript, candidate_one=one.text, candidate_two=two.text
            )

            async def call(sent: list[dict[str, str]]) -> str:
                if not self._budget.take(GRADER_ROUTE):
                    return ""
                result = await self._call(
                    GRADER_ROUTE,
                    sent,
                    max_tokens=GRADER_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
                return result.text

            parsed, _ = await grade_with_repair(call, messages)
            if parsed is None:
                self._grades.append(
                    {
                        "pair": pair,
                        "item_id": item_id,
                        "stratum": stratum,
                        "hard_case": bool(hard_case),
                        "outcome": "grade_failed",
                        "error": "invalid_grade",
                    }
                )
                return None
            preferences.append(parsed.preference)
            labels = ("1", "2")
            arms = (first_arm, second_arm) if order == 1 else (second_arm, first_arm)
            for label, arm in zip(labels, arms, strict=True):
                side = parsed.sides[label]
                sides.append({"arm": arm, **side.as_mapping()})
            if self._budget.exhausted:
                self._partial = True
                break
        if len(preferences) != 2:
            return None
        merged = merge_order(preferences[0], preferences[1])
        outcome = "win" if merged == "A" else "loss" if merged == "B" else "tie"
        record = {
            "pair": pair,
            "item_id": item_id,
            "stratum": stratum,
            "hard_case": bool(hard_case),
            "outcome": outcome,
            "sides": sides,
        }
        self._grades.append(record)
        return record

    # -- execution --------------------------------------------------------------------

    async def execute(
        self, prepared: PreparedRun, run: RunConfig, *, report_dir: Path | None = None
    ) -> RunOutcome:
        report_dir = Path(report_dir) if report_dir is not None else self._report_dir()
        report_dir.mkdir(parents=True, exist_ok=True)
        cache = LabelCache(report_dir / LABEL_CACHE_FILE)
        self._reset_run_state(
            limit=run.limit_calls, oracle=bool(run.oracle), arms=_requested_arms(run)
        )
        self._items_total = len(prepared.replies) + len(prepared.writer)
        slots = asyncio.Semaphore(max(1, int(run.concurrency)))
        stopped = False

        async def _reply(record: ReplyRecord) -> None:
            nonlocal stopped
            async with slots:
                if stopped or self._budget.exhausted:
                    self._partial = self._partial or self._budget.exhausted
                    return
                prepared_reply = await self._prepare_reply(record)
                if prepared_reply is not None and not await self._run_reply(
                    prepared_reply, cache, run
                ):
                    stopped = True

        async def _writer(record: WriterRecord) -> None:
            nonlocal stopped
            async with slots:
                if stopped or self._budget.exhausted:
                    self._partial = self._partial or self._budget.exhausted
                    return
                prepared_writer = await self._prepare_writer(record)
                if prepared_writer is not None and not await self._run_writer(
                    prepared_writer, cache, run
                ):
                    stopped = True

        # One event loop, so the shared candidate/grade lists need no lock.
        await asyncio.gather(
            *(_reply(record) for record in prepared.replies),
            *(_writer(record) for record in prepared.writer),
        )
        if self._budget.exhausted:
            self._partial = True
        # Concurrent items finish in any order; the seeded bootstrap needs a stable one.
        self._grades.sort(key=lambda grade: (str(grade["item_id"]), str(grade["pair"])))
        self._candidates.sort(key=lambda row: (str(row["item_id"]), str(row["arm"])))
        summary = self._summarize(prepared)
        self._write_outputs(report_dir, summary)
        return RunOutcome(
            planned=prepared.planned,
            summary=summary,
            report_dir=report_dir,
            partial=bool(summary["partial"]),
        )

    def _reset_run_state(
        self,
        *,
        limit: int | None,
        oracle: bool,
        arms: Sequence[str] | None = None,
    ) -> None:
        """Start a clean run; the label cache is the only state that survives a rerun."""
        self._budget = _CallBudget(limit=limit)
        self._partial = False
        self._oracle = oracle
        self._run_arms = tuple(arms or ("A", "A2", "S", "P"))
        self._label_calls = 0
        self._label_prompt_tokens = 0
        self._label_completion_tokens = 0
        self._label_failures = 0
        self._label_cache_hits = 0
        self._oracle_label_calls = 0
        self._oracle_label_prompt_tokens = 0
        self._oracle_label_completion_tokens = 0
        self._prepare_failures = Counter()
        self._generation_errors = Counter()
        self._candidates = []
        self._grades = []
        self._items = []
        self._render_ms = []
        self._s_items_with_derived = 0
        self._items_with_s_view = 0
        self._items_total = 0

    def _report_dir(self) -> Path:
        from datetime import UTC, datetime

        from yeoman_shared.utils.helpers import get_var_path

        root = get_var_path().joinpath(*REPORT_DIR)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for suffix in range(1000):
            candidate = root / (stamp if suffix == 0 else f"{stamp}.{suffix}")
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            return candidate
        raise RuntimeError("could not allocate a unique report directory")

    async def _run_reply(self, prepared: _PreparedReply, cache: LabelCache, run: RunConfig) -> bool:
        record = prepared.record
        requested = _requested_arms(run)
        need_state = any(arm in {"S", "P", "Sx"} for arm in requested)
        s_text = ""
        s_items: tuple[str, ...] = ()
        if need_state:
            simulated, _ = await self._label(prepared.simulated_input, cache, oracle=False)
            if self._budget.exhausted:
                self._partial = True
                return False
            oracle = None
            if run.oracle:
                oracle, _ = await self._label(prepared.oracle_input, cache, oracle=True)
                if self._budget.exhausted:
                    self._partial = True
                    return False
            s_section = self._label_section(
                prepared.simulated_input,
                simulated,
                prepared.label_time_ms or record.item.at_ms,
            )
            s_state = self._with_labels(prepared.base_state, s_section)
            s_text, s_items, s_ms = self._render(
                s_state,
                ViewTarget(record.history_id, record.reply_to_history_id),
                chat_id=record.item.chat_id,
                at_ms=record.item.at_ms,
                writer=False,
            )
            oracle_text = ""
            if run.oracle:
                sx_section = self._label_section(prepared.oracle_input, oracle, record.item.at_ms)
                sx_state = self._with_labels(prepared.base_state, sx_section)
                oracle_text, _, sx_ms = self._render(
                    sx_state,
                    ViewTarget(record.history_id, record.reply_to_history_id),
                    chat_id=record.item.chat_id,
                    at_ms=record.item.at_ms,
                    writer=False,
                )
                prepared.rendered_ms.append(sx_ms)
            prepared.rendered_ms.append(s_ms)
            self._render_ms.extend(prepared.rendered_ms)
            self._items_with_s_view += int(bool(s_text.strip()))
            self._s_items_with_derived += int(bool(s_items))
        placebo = build_placebo(s_text, seed=int(record.item.at_ms) + int(run.seed))
        arms: dict[str, list[dict[str, str]]] = {}
        if "A" in requested:
            arms["A"] = arm_messages(prepared.base_messages, "")
        if "A2" in requested:
            arms["A2"] = arm_messages(prepared.base_messages, "")
        if "S" in requested:
            arms["S"] = arm_messages(prepared.base_messages, s_text)
        if "P" in requested:
            arms["P"] = arm_messages(prepared.base_messages, placebo)
        if "Sx" in requested and run.oracle:
            arms["Sx"] = arm_messages(prepared.base_messages, oracle_text)
        if "R" in requested:
            arms["R"] = arm_messages(prepared.base_messages, prepared.recall_text)
        if "P_R" in requested:
            arms["P_R"] = arm_messages(
                prepared.base_messages,
                build_placebo(prepared.recall_text, seed=int(record.item.at_ms) + int(run.seed)),
            )
        generated: dict[str, _CallResult] = {}
        for arm in requested:
            if arm not in arms:
                continue
            if self._budget.exhausted:
                self._partial = True
                break
            generated[arm] = await self._generate(record.item.item_id, arm, REPLY_ROUTE, arms[arm])
        self._record_item(
            record.item,
            record.history_id,
            record.native_id,
            record.reply_to_history_id,
            record.reply_to_native_id,
        )
        if self._budget.exhausted:
            self._partial = True
            return False
        await self._grade_item(
            item=record.item,
            transcript=prepared.transcript,
            generated=generated,
            run=run,
        )
        return not self._budget.exhausted

    async def _run_writer(
        self, prepared: _PreparedWriter, cache: LabelCache, run: RunConfig
    ) -> bool:
        record = prepared.record
        # The writer already has group recall in production, so R/P_R are not generated.
        requested = tuple(arm for arm in _requested_arms(run) if arm not in {"R", "P_R"})
        need_state = any(arm in {"S", "P", "Sx"} for arm in requested)
        s_text = ""
        s_items: tuple[str, ...] = ()
        if need_state:
            simulated, _ = await self._label(prepared.simulated_input, cache, oracle=False)
            if self._budget.exhausted:
                self._partial = True
                return False
            oracle = None
            if run.oracle:
                oracle, _ = await self._label(prepared.oracle_input, cache, oracle=True)
                if self._budget.exhausted:
                    self._partial = True
                    return False
            s_section = self._label_section(
                prepared.simulated_input,
                simulated,
                prepared.label_time_ms or record.item.at_ms,
            )
            s_state = self._with_labels(prepared.base_state, s_section)
            s_text, s_items, s_ms = self._render(
                s_state,
                prepared.target,
                chat_id=record.item.chat_id,
                at_ms=record.item.at_ms,
                writer=True,
            )
            oracle_text = ""
            if run.oracle:
                sx_section = self._label_section(prepared.oracle_input, oracle, record.item.at_ms)
                sx_state = self._with_labels(prepared.base_state, sx_section)
                oracle_text, _, sx_ms = self._render(
                    sx_state,
                    prepared.target,
                    chat_id=record.item.chat_id,
                    at_ms=record.item.at_ms,
                    writer=True,
                )
                prepared.rendered_ms.append(sx_ms)
            prepared.rendered_ms.append(s_ms)
            self._render_ms.extend(prepared.rendered_ms)
            self._items_with_s_view += int(bool(s_text.strip()))
            self._s_items_with_derived += int(bool(s_items))
        placebo = build_placebo(s_text, seed=int(record.item.at_ms) + int(run.seed))

        def prompt(block: str) -> str:
            context = copy.deepcopy(prepared.context)
            if block:
                context["writer_state_text"] = block
            return _writer_prompt(context)

        prompts: dict[str, str] = {}
        if "A" in requested:
            prompts["A"] = prepared.base_prompt
        if "A2" in requested:
            prompts["A2"] = prepared.base_prompt
        if "S" in requested:
            prompts["S"] = prompt(s_text)
        if "P" in requested:
            prompts["P"] = prompt(placebo)
        if "Sx" in requested and run.oracle:
            prompts["Sx"] = prompt(oracle_text)
        generated: dict[str, _CallResult] = {}
        for arm in requested:
            if arm not in prompts:
                continue
            if self._budget.exhausted:
                self._partial = True
                break
            generated[arm] = await self._generate(
                record.item.item_id,
                arm,
                WRITER_ROUTE,
                [{"role": "user", "content": prompts[arm]}],
            )
        self._record_item(
            record.item, record.target_history_id, record.target_native_id, None, None
        )
        if self._budget.exhausted:
            self._partial = True
            return False
        await self._grade_item(
            item=record.item,
            transcript=prepared.transcript,
            generated=generated,
            run=run,
        )
        return not self._budget.exhausted

    async def _grade_item(
        self,
        *,
        item: ReplyStateItem,
        transcript: str,
        generated: Mapping[str, _CallResult],
        run: RunConfig,
    ) -> None:
        pairs = _pairs_for_arms(_requested_arms(run), oracle=run.oracle)
        for first_arm, second_arm, pair in pairs:
            if self._budget.exhausted:
                self._partial = True
                return
            await self._grade_pair(
                pair=pair,
                first_arm=first_arm,
                second_arm=second_arm,
                item_id=item.item_id,
                stratum=item.stratum,
                hard_case=item.hard_case,
                transcript=transcript,
                candidates=generated,
            )

    def _record_item(
        self,
        item: ReplyStateItem,
        history_id: str,
        native_id: str,
        reply_to_history_id: str | None,
        reply_to_native_id: str | None,
    ) -> None:
        self._items.append(
            {
                "item_id": item.item_id,
                "kind": item.kind,
                "chat_id": item.chat_id,
                "at_ms": int(item.at_ms),
                "stratum": item.stratum,
                "hard_case": bool(item.hard_case),
                "history_id": history_id,
                "native_id": native_id,
                "reply_to_history_id": reply_to_history_id,
                "reply_to_native_id": reply_to_native_id,
            }
        )

    # -- summary and outputs ----------------------------------------------------------

    def _summarize(self, prepared: PreparedRun) -> dict[str, Any]:
        pair_records: dict[str, list[dict[str, Any]]] = {}
        for grade in self._grades:
            pair_records.setdefault(str(grade["pair"]), []).append(grade)
        pairs: dict[str, Any] = {}
        pair_order = _pairs_for_arms(self._run_arms, oracle=self._oracle)
        for first_arm, second_arm, pair in pair_order:
            records = pair_records.get(pair, [])
            pairs[pair] = {
                "first": first_arm,
                "second": second_arm,
                "overall": self._pair_stats(
                    [
                        record["outcome"]
                        for record in records
                        if record["outcome"] != "grade_failed"
                    ],
                    seed=int(prepared.selection.requested.get("replies", 0)) + len(records),
                ),
                "strata": self._pair_strata(records),
                "hard_cases": self._pair_stats(
                    [
                        record["outcome"]
                        for record in records
                        if record.get("hard_case") and record["outcome"] != "grade_failed"
                    ],
                    seed=len(records) + 1,
                ),
                "grade_failed": sum(record["outcome"] == "grade_failed" for record in records),
            }
        arms = self._arm_summary()
        labels = {
            "s_view_rendered": self._items_with_s_view,
            "s_view_with_derived_items": self._s_items_with_derived,
            "items": self._items_total,
            "s_view_with_derived_share": (
                round(self._s_items_with_derived / self._items_total, 3)
                if self._items_total
                else None
            ),
            "label_calls": self._label_calls,
            "label_prompt_tokens": self._label_prompt_tokens,
            "label_completion_tokens": self._label_completion_tokens,
            "label_failures": self._label_failures,
            "label_cache_hits": self._label_cache_hits,
            "oracle_label_calls": self._oracle_label_calls,
            "oracle_label_prompt_tokens": self._oracle_label_prompt_tokens,
            "oracle_label_completion_tokens": self._oracle_label_completion_tokens,
        }
        a2 = pairs.get("A2_vs_A", {}).get("overall", {}).get("win_rate")
        s_a = pairs.get("S_vs_A", {}).get("overall", {}).get("win_rate")
        s_p = pairs.get("S_vs_P", {}).get("overall", {}).get("win_rate")
        band = abs(float(a2) - 0.5) if isinstance(a2, (int, float)) else None
        threshold = 0.5 + band if band is not None else None
        s_contradicts = arms.get("S", {}).get("contradicts_rate")
        a_contradicts = arms.get("A", {}).get("contradicts_rate")
        no_more_contradictions = (
            isinstance(s_contradicts, (int, float))
            and isinstance(a_contradicts, (int, float))
            and s_contradicts <= a_contradicts
        )
        r_a_ci = pairs.get("R_vs_A", {}).get("overall", {}).get("ci95")
        r_a_low = r_a_ci[0] if isinstance(r_a_ci, (list, tuple)) and r_a_ci else None
        r_gt_a = bool(
            isinstance(r_a_low, (int, float)) and threshold is not None and r_a_low > threshold
        )
        r_p_r = pairs.get("R_vs_P_R", {}).get("overall", {}).get("win_rate")
        r_gt_p_r = bool(isinstance(r_p_r, (int, float)) and r_p_r > 0.5)
        r_contradicts = arms.get("R", {}).get("contradicts_rate")
        no_more_contradictions_r = (
            isinstance(r_contradicts, (int, float))
            and isinstance(a_contradicts, (int, float))
            and r_contradicts <= a_contradicts
        )
        gate = {
            "s_gt_a": bool(
                isinstance(s_a, (int, float)) and threshold is not None and s_a > threshold
            ),
            "s_gt_p": bool(
                isinstance(s_p, (int, float)) and threshold is not None and s_p > threshold
            ),
            "no_more_contradictions": bool(no_more_contradictions),
            "r_gt_a": r_gt_a,
            "r_gt_p_r": r_gt_p_r,
            "no_more_contradictions_r": bool(no_more_contradictions_r),
            "owner_sample_pending": True,
            "s_vs_a_win_rate": s_a,
            "s_vs_p_win_rate": s_p,
            "a2_vs_a_win_rate": a2,
            "r_vs_a_lower_ci": r_a_low,
            "r_vs_p_r_win_rate": r_p_r,
            "noise_band": round(band, 3) if band is not None else None,
            "threshold": round(threshold, 3) if threshold is not None else None,
            "view_render_p95_ms": _percentile(self._render_ms, 0.95),
            "view_render_p95_source": "this run",
        }
        return {
            "report_schema": 1,
            "partial": bool(self._partial),
            "oracle": bool(self._oracle),
            "read_only": True,
            "no_tools": True,
            "note": (
                "Every store was opened read-only; generation used the route clients "
                "directly with no tools, identically for every arm. Session history is "
                "not reconstructable and is empty in the reply prompt. The grader sees "
                "only the as-of transcript and the two candidates."
            ),
            "items": {
                "selected": len(self._items),
                "reply": len(prepared.replies),
                "writer": len(prepared.writer),
                "requested": dict(prepared.selection.requested),
                "available": dict(prepared.selection.available),
                "shortfall": dict(prepared.selection.shortfall),
                "hard_cases": prepared.selection.hard_cases,
                "hard_share": (
                    round(prepared.selection.hard_cases / len(prepared.replies), 3)
                    if prepared.replies
                    else None
                ),
                "older_context": sum(item.older_context for item in prepared.selection.items),
                "older_context_signals": dict(
                    Counter(
                        signal
                        for item in prepared.selection.items
                        for signal in item.older_context_signals
                    )
                ),
                "prepare_failures": dict(self._prepare_failures),
            },
            "planned": {
                "routes": dict(prepared.planned.routes),
                "prompt_tokens": prepared.planned.prompt_tokens,
                "completion_tokens": prepared.planned.completion_tokens,
                "items": prepared.planned.items,
            },
            "calls": {
                "routes": dict(self._budget.by_route),
                "total": int(self._budget.used),
            },
            "models": self._model_summary(),
            "pairs": pairs,
            "arms": arms,
            "arm_notes": (
                {
                    "R": "not applicable to writer items: the live writer already renders "
                    "group recall"
                }
                if any(arm in {"R", "P_R"} for arm in self._run_arms)
                else {}
            ),
            "labels": labels,
            "gate": gate,
            "generation_errors": dict(self._generation_errors),
            "grade_failed": sum(grade["outcome"] == "grade_failed" for grade in self._grades),
        }

    def _model_summary(self) -> dict[str, Any]:
        """Route/profile settings, with the temperature each route is called at."""
        models = getattr(self._config, "models", None)
        routes = getattr(models, "routes", None) or {}
        profiles = getattr(models, "profiles", None) or {}
        result: dict[str, Any] = {}
        for route in (REPLY_ROUTE, WRITER_ROUTE, self._labels_route, GRADER_ROUTE):
            profile_name = str(routes.get(route) or "")
            profile = profiles.get(profile_name)
            result[route] = {
                "profile": profile_name,
                "model": getattr(profile, "model", None),
                "temperature_configured": getattr(profile, "temperature", None),
                "temperature_used": (
                    getattr(profile, "temperature", None) or 0.0
                    if route in (REPLY_ROUTE, WRITER_ROUTE)
                    else 0.0
                ),
                "structured_output": getattr(profile, "structured_output", None),
            }
        return result

    def _pair_strata(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        strata = sorted({str(record["stratum"]) for record in records})
        return {
            stratum: self._pair_stats(
                [
                    record["outcome"]
                    for record in records
                    if record["stratum"] == stratum and record["outcome"] != "grade_failed"
                ],
                seed=sum(ord(char) for char in stratum) + len(records),
            )
            for stratum in strata
        }

    @staticmethod
    def _pair_stats(outcomes: Sequence[str], *, seed: int) -> dict[str, Any]:
        wins = sum(item == "win" for item in outcomes)
        ties = sum(item == "tie" for item in outcomes)
        losses = sum(item == "loss" for item in outcomes)
        decisive = wins + losses
        interval = bootstrap_ci(outcomes, seed=int(seed)) if outcomes else None
        return {
            "wins": wins,
            "ties": ties,
            "losses": losses,
            "win_rate": round(wins / decisive, 3) if decisive else None,
            "ci95": list(interval) if interval is not None else None,
        }

    def _arm_summary(self) -> dict[str, Any]:
        candidates = [
            entry for entry in self._candidates if not str(entry["arm"]).startswith("grade:")
        ]
        sides: dict[str, list[Mapping[str, Any]]] = {}
        for grade in self._grades:
            for side in grade.get("sides") or ():
                if isinstance(side, Mapping) and side.get("arm"):
                    sides.setdefault(str(side["arm"]), []).append(side)
        arms: dict[str, Any] = {}
        for arm in ARMS:
            generated = [entry for entry in candidates if entry["arm"] == arm]
            if not generated and arm not in sides:
                continue
            arm_sides = sides.get(arm, [])
            lengths = [len(str(entry["text"])) for entry in generated if entry["text"]]
            prompt_tokens = [int(entry["prompt_tokens"]) for entry in generated]
            completion_tokens = [int(entry["completion_tokens"]) for entry in generated]
            latencies = [int(entry["latency_ms"]) for entry in generated]
            arms[arm] = {
                "generated": len(generated),
                "errors": sum(bool(entry["error"]) for entry in generated),
                "length_mean": _mean(lengths),
                "length_p95": _percentile(lengths, 0.95),
                "prompt_tokens": sum(prompt_tokens),
                "completion_tokens": sum(completion_tokens),
                "latency_p50_ms": _percentile(latencies, 0.5),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "sides": len(arm_sides),
                "accurate_rate": self._side_rate(arm_sides, "accurate"),
                "repeats_arvid_rate": self._side_rate(arm_sides, "repeats_arvid"),
                "picks_up_open_rate": self._side_rate(arm_sides, "picks_up_open"),
                "contradicts_rate": self._side_rate(arm_sides, "contradicts"),
                "tone_mean": _mean([int(side["tone"]) for side in arm_sides]),
            }
        return arms

    @staticmethod
    def _side_rate(sides: Sequence[Mapping[str, Any]], key: str) -> float | None:
        values = [bool(side.get(key)) for side in sides]
        return round(sum(values) / len(values), 3) if values else None

    def _write_outputs(self, report_dir: Path, summary: Mapping[str, Any]) -> None:
        with (report_dir / ITEMS_FILE).open("w", encoding="utf-8") as handle:
            for item in self._items:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        descriptor = os.open(
            report_dir / CANDIDATES_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for candidate in self._candidates:
                handle.write(json.dumps(candidate, ensure_ascii=False, sort_keys=True) + "\n")
        (report_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (report_dir / "report.md").write_text(render_report(summary), encoding="utf-8")


def read_run_items(report_dir: Path) -> list[dict[str, Any]]:
    path = Path(report_dir) / ITEMS_FILE
    if not path.is_file():
        raise ValueError(f"{ITEMS_FILE} missing")
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def read_run_candidates(report_dir: Path) -> dict[tuple[str, str], str]:
    path = Path(report_dir) / CANDIDATES_FILE
    if not path.is_file():
        raise ValueError(f"{CANDIDATES_FILE} missing")
    result: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        arm = str(entry.get("arm") or "")
        if not arm.startswith("grade:"):
            result[(str(entry.get("item_id")), arm)] = str(entry.get("text") or "")
    return result


def _review_pick(
    items: Sequence[Mapping[str, Any]], *, sample: int, seed: int
) -> list[Mapping[str, Any]]:
    """Seeded proportional pick across strata, for the owner's sample."""
    wanted = min(max(0, int(sample)), len(items))
    if wanted <= 0:
        return []
    rng = random.Random(int(seed))
    by_stratum: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        by_stratum.setdefault(str(item.get("stratum") or ""), []).append(item)
    for values in by_stratum.values():
        rng.shuffle(values)
    total = len(items)
    quotas = {stratum: wanted * len(values) / total for stratum, values in by_stratum.items()}
    floors = {stratum: int(quota) for stratum, quota in quotas.items()}
    remaining = wanted - sum(floors.values())
    order = sorted(quotas, key=lambda stratum: (-(quotas[stratum] - floors[stratum]), stratum))
    for stratum in order[:remaining]:
        floors[stratum] += 1
    picked = [
        item for stratum in sorted(by_stratum) for item in by_stratum[stratum][: floors[stratum]]
    ]
    picked.sort(key=lambda item: (int(item.get("at_ms") or 0), str(item.get("item_id"))))
    return picked


def _review_line(queries: HistoryQueries, row: Mapping[str, Any]) -> str:
    shaped = archive_row(queries, dict(row))
    sender = str(shaped.get("sender_name") or shaped.get("sender_id") or "unknown")
    text = " ".join(str(shaped.get("text") or "").split())[:300]
    return f"- [{sender}] {text}"


async def build_review(
    report_dir: Path, history: Any, *, sample: int = 20, seed: int = 7
) -> tuple[str, dict[str, Any]]:
    """The owner review markdown and its separate blind X/Y key."""
    from datetime import datetime

    items = read_run_items(report_dir)
    candidates = read_run_candidates(report_dir)
    eligible = [
        item
        for item in items
        if (str(item.get("item_id")), "A") in candidates
        and (str(item.get("item_id")), "S") in candidates
    ]
    picked = _review_pick(eligible, sample=sample, seed=seed)
    assign_rng = random.Random(int(seed) ^ 0x5A5A)
    lines = [
        "# Reply-state owner review",
        "",
        "Two candidates per item, blind. Mark your preference on the last line of each item.",
        "The X/Y key is in a separate file.",
        "",
    ]
    key_items: list[dict[str, Any]] = []
    async with history_turn(history) as snapshot:
        queries = HistoryQueries(snapshot)
        subjects = {
            str(row["chat_id"]): str(row.get("subject") or "")
            for row in queries.chats()
            if row.get("subject")
        }
        for index, item in enumerate(picked, start=1):
            item_id = str(item.get("item_id"))
            first = candidates.get((item_id, "S"))
            second = candidates.get((item_id, "A"))
            if first is None or second is None:
                continue
            chat_id = str(item.get("chat_id") or "")
            history_id = str(item.get("history_id") or "")
            at_ms = int(item.get("at_ms") or 0)
            current = queries.message(history_id) if history_id else None
            if current is None and history_id:
                current = queries.native_message(chat_id=chat_id, native_id=history_id)
            if current is not None:
                rows = queries.recent(
                    chat_id=chat_id, limit=8, before_id=str(current["message_id"])
                )
            else:
                rows = queries.recent(chat_id=chat_id, limit=8, until_ms=at_ms - 1)
            coin = assign_rng.random() < 0.5
            x_arm, y_arm = ("S", "A") if coin else ("A", "S")
            x_text = candidates[(item_id, x_arm)]
            y_text = candidates[(item_id, y_arm)]
            moment = datetime.fromtimestamp(at_ms / 1000).astimezone()
            lines.extend(
                [
                    f"## {index}. {subjects.get(chat_id) or chat_id}",
                    "",
                    f"time: {moment:%Y-%m-%d %H:%M %Z}  item: {item_id}",
                    "",
                ]
            )
            lines.extend(_review_line(queries, row) for row in rows)
            if current is not None:
                lines.extend(["", f"Item: {_review_line(queries, current)[2:]}"])
            lines.extend(
                [
                    "",
                    "Candidate X:",
                    x_text,
                    "",
                    "Candidate Y:",
                    y_text,
                    "",
                    "Better: X / Y / same",
                    "",
                ]
            )
            key_items.append(
                {
                    "item_id": item_id,
                    "chat_id": chat_id,
                    "at_ms": at_ms,
                    "x": x_arm,
                    "y": y_arm,
                }
            )
    return "\n".join(lines), {"seed": int(seed), "sample": len(key_items), "items": key_items}


def write_review(out: Path, review: str, key: Mapping[str, Any]) -> Path:
    """Write the review and its separate key, both mode 0600."""
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(review)
    os.chmod(path, 0o600)
    key_path = Path(f"{path}.key.json")
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(key, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(key_path, 0o600)
    return key_path


def render_report(summary: Mapping[str, Any]) -> str:
    """The human report: ids and aggregates only, never a candidate or message text."""
    lines = [
        "# Reply-state evaluation",
        "",
        f"Partial: {summary.get('partial')}  Oracle: {summary.get('oracle')}  "
        f"Read-only stores: {summary.get('read_only')}  No tools: {summary.get('no_tools')}",
        "",
        f"{summary.get('note')}",
        "",
        "## Sample",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key, value in (summary.get("items") or {}).items():
        lines.append(f"| {key} | {json.dumps(value, ensure_ascii=False, sort_keys=True)} |")
    lines.extend(
        [
            "",
            "## Models",
            "",
            "| route | profile | model | temperature configured/used | structured output |",
            "|---|---|---|---|---|",
        ]
    )
    for route, model in (summary.get("models") or {}).items():
        lines.append(
            f"| {route} | {model.get('profile')} | {model.get('model')} | "
            f"{model.get('temperature_configured')}/{model.get('temperature_used')} | "
            f"{model.get('structured_output')} |"
        )
    lines.extend(
        [
            "",
            "## Pairs",
            "",
            "| pair | first | second | wins | ties | losses | win rate | 95% CI | failed |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for pair, entry in (summary.get("pairs") or {}).items():
        overall = entry.get("overall") or {}
        lines.append(
            f"| {pair} | {entry.get('first')} | {entry.get('second')} | "
            f"{overall.get('wins')} | {overall.get('ties')} | {overall.get('losses')} | "
            f"{overall.get('win_rate')} | {overall.get('ci95')} | {entry.get('grade_failed')} |"
        )
    lines.extend(
        [
            "",
            "## Arms",
            "",
            "| arm | generated | accurate | repeats | picks up | contradicts | tone | length mean | length p95 | tokens p/c | latency p50/p95 ms |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for arm, entry in (summary.get("arms") or {}).items():
        lines.append(
            f"| {arm} | {entry.get('generated')} | {entry.get('accurate_rate')} | "
            f"{entry.get('repeats_arvid_rate')} | {entry.get('picks_up_open_rate')} | "
            f"{entry.get('contradicts_rate')} | {entry.get('tone_mean')} | "
            f"{entry.get('length_mean')} | {entry.get('length_p95')} | "
            f"{entry.get('prompt_tokens')}/{entry.get('completion_tokens')} | "
            f"{entry.get('latency_p50_ms')}/{entry.get('latency_p95_ms')} |"
        )
    labels = summary.get("labels") or {}
    lines.extend(["", "## Labels", "", "| metric | value |", "|---|---|"])
    for key, value in labels.items():
        lines.append(f"| {key} | {json.dumps(value, ensure_ascii=False)} |")
    gate = summary.get("gate") or {}
    lines.extend(["", "## Gate", "", "| check | value |", "|---|---|"])
    for key, value in gate.items():
        lines.append(f"| {key} | {json.dumps(value, ensure_ascii=False)} |")
    lines.extend(
        [
            "",
            "Owner review: `yeoman eval reply-state-review <run dir> --sample 20 --out <file.md>`.",
            "The review file holds the only message text and a separate X/Y key.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ARMS",
    "CANDIDATES_FILE",
    "DEFAULT_ARMS",
    "DEFAULT_LABELS_ROUTE",
    "GRADER_ROUTE",
    "ITEMS_FILE",
    "LABEL_CACHE_FILE",
    "MEASURE_FILE",
    "ORACLE_PAIR",
    "PAIR_ORDER",
    "RECALL_PAIR_ORDER",
    "REPLY_ROUTE",
    "REPORT_DIR",
    "WRITER_ROUTE",
    "LabelCache",
    "PlannedCalls",
    "PreparedRun",
    "ReplyRecord",
    "ReplyStateRunner",
    "RunConfig",
    "RunOutcome",
    "WriterRecord",
    "arm_messages",
    "asdict_labels",
    "build_reply_base_messages",
    "build_review",
    "load_reply_records",
    "load_writer_records",
    "read_run_candidates",
    "read_run_items",
    "render_report",
    "write_review",
]
