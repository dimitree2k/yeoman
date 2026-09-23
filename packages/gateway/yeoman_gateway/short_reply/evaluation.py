"""Offline evaluation of the short-reply path on historical data (spec 2026-09-22 §10).

Read-only against the runtime databases. Nothing here sends a chat message; only
``simulate_decisions`` calls a model, and only through the decider its caller passes.
The legacy regex helpers are used only for V0 baseline reconstruction and label-sample stratification; they never select live candidates or decide a new-path action.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import random
import shlex
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from math import log2
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import regex

from yeoman_gateway.implicit_addressing import (
    looks_like_low_content_reply,
    looks_like_question_or_request,
    looks_like_repair_feedback,
    looks_like_reply_ack,
    reaction_for_reply_ack,
)
from yeoman_gateway.processing.models import RecentReaction, ShortReplyClaim
from yeoman_gateway.short_reply.reactor import ReactorRequest, ShortReplyReactor
from yeoman_gateway.short_reply.signals import compute_signals, emojis_in
from yeoman_gateway.short_reply.variety import cooldown_active

LABEL_FIELDS: tuple[str, ...] = (
    "row_id", "chat", "timestamp", "bot_text", "text", "graphemes", "question",
    "bot_asked", "media_kind", "media_text", "media_metadata_available",
    "priority_mode", "candidate",
    "legacy_mode", "legacy_emoji", "actual", "synthetic",
    "label_action", "label_emojis", "notes",
)
LABEL_ACTIONS = frozenset({"react", "answer", "none"})


def _private_text_file(path: Path, *, newline: str | None = None):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline=newline)


@dataclass(frozen=True, slots=True)
class ReplayRow:
    row_id: str
    chat: str
    chat_id: str
    message_id: str
    timestamp: int
    text: str
    bot_text: str
    graphemes: int
    question: bool
    bot_asked: bool
    media_kind: str
    media_text: str
    media_metadata_available: bool
    priority_mode: str
    candidate: bool
    legacy_mode: str
    legacy_emoji: str
    actual: str
    synthetic: bool = False


@dataclass(frozen=True, slots=True)
class Label:
    action: str
    emojis: tuple[str, ...]


def chat_hash(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _receipt_ids(processing: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in processing.execute(
            "SELECT provider_message_id FROM transport_receipts "
            "WHERE provider_message_id IS NOT NULL"
        )
    }


def derive_bot_participants(archive_db: Path, processing_db: Path) -> set[str]:
    """Arvid's identities: whoever authored an archived message we have a receipt for."""
    with closing(_ro(processing_db)) as processing:
        receipts = _receipt_ids(processing)
    identities: set[str] = set()
    with closing(_ro(archive_db)) as archive:
        for row in archive.execute("SELECT message_id, participant, sender_id FROM inbound_messages"):
            if str(row["message_id"]) in receipts:
                identities.update(str(v) for v in (row["participant"], row["sender_id"]) if v)
    return identities


def _legacy(text: str, bot_text: str) -> tuple[str, str]:
    if looks_like_reply_ack(text):
        return "reply_ack", reaction_for_reply_ack(text)
    if not looks_like_question_or_request(bot_text) and looks_like_low_content_reply(text):
        return "low_content_reply", "👀"
    return "reply_to_bot", ""


def _inbound_media_index(paths: Sequence[Path]) -> dict[str, dict[str, str]]:
    """Index only the documented media fields from inbound JSONL by provider message ID."""
    result: dict[str, dict[str, str]] = {}
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                event = record.get("event", record)
                metadata = event.get("raw_metadata") or event.get("metadata") or {}
                message_id = str(event.get("message_id") or event.get("source_message_id") or "")
                if message_id:
                    result[message_id] = {
                        key: str(metadata.get(key) or "")
                        for key in ("media_kind", "media_description", "voice_transcript")
                        if key in metadata
                    }
                    state = metadata.get("conversation_state") or event.get("conversation_state") or {}
                    result[message_id]["priority_mode"] = str(state.get("address_mode") or "")
    return result


def _effect_text(processing: sqlite3.Connection, provider_id: str) -> str:
    row = processing.execute(
        "SELECT e.payload_json FROM transport_receipts tr JOIN effects e USING (effect_id) "
        "WHERE tr.provider_message_id = ? LIMIT 1",
        (provider_id,),
    ).fetchone()
    try:
        payload = json.loads(row["payload_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        payload = {}
    return str(payload.get("text") or "") if isinstance(payload, dict) else ""


def _actual(processing: sqlite3.Connection, message_id: str, since_ms: int, ts_ms: int) -> str:
    if ts_ms < since_ms:
        return "unknown"
    rows = processing.execute(
        "SELECT e.capability, e.payload_json, tr.receipt_id FROM effects e "
        "LEFT JOIN transport_receipts tr USING (effect_id) "
        "WHERE e.state = 'sent' AND (e.trace_id = ? OR "
        "json_extract(e.payload_json, '$.message_id') = ? OR (e.turn_id != '' AND e.turn_id IN ("
        "SELECT turn_id FROM events WHERE source_message_id = ? AND turn_id IS NOT NULL)))",
        (message_id, message_id, message_id),
    ).fetchall()
    confirmed = [row for row in rows if row["receipt_id"] is not None]
    if any(row["capability"] == "send_text" for row in confirmed):
        return "text"
    for row in confirmed:
        if row["capability"] == "send_reaction":
            try:
                emoji = json.loads(row["payload_json"] or "{}").get("emoji")
            except (TypeError, ValueError, AttributeError):
                emoji = None
            return f"reaction:{emoji or '?'}"
    if rows:
        return "unknown"
    return "none"


def load_replay_rows(
    archive_db: Path,
    processing_db: Path,
    *,
    since_ts: int,
    inbound_jsonl: Sequence[Path] = (),
    max_chars: int = 80,
    bot_participants: Collection[str] | None = None,
) -> list[ReplayRow]:
    bots = set(bot_participants or derive_bot_participants(archive_db, processing_db))
    media_by_id = _inbound_media_index(inbound_jsonl)
    with closing(_ro(archive_db)) as archive, closing(_ro(processing_db)) as processing:
        receipts = _receipt_ids(processing)
        first = processing.execute("SELECT MIN(created_ms) FROM effects").fetchone()[0]
        history_since_ms = int(first) if first is not None else 2**62
        messages = {
            str(row["message_id"]): row
            for row in archive.execute("SELECT * FROM inbound_messages")
        }
        bot_ids = receipts | {
            message_id
            for message_id, row in messages.items()
            if row["participant"] in bots or row["sender_id"] in bots
        }
        replays: list[ReplayRow] = []
        for row in sorted(messages.values(), key=lambda r: (int(r["timestamp"]), str(r["message_id"]))):
            target = str(row["reply_to_message_id"] or "")
            if int(row["timestamp"]) < since_ts or not target or target not in bot_ids:
                continue
            if row["participant"] in bots or row["sender_id"] in bots:
                continue
            text = str(row["text"] or "")
            media = media_by_id.get(str(row["message_id"]), {})
            bot_row = messages.get(target)
            bot_text = str(bot_row["text"] or "") if bot_row is not None else _effect_text(processing, target)
            signals = compute_signals(
                content=text, reply_to_bot=True, reply_to_text=bot_text, metadata=media
            )
            mode, emoji = _legacy(text, bot_text)
            chat_id = str(row["chat_id"])
            replays.append(
                ReplayRow(
                    row_id=(f"{chat_hash(chat_id)}:{hashlib.sha256((chat_id + ':' + str(row['message_id'])).encode()).hexdigest()}"),
                    chat=chat_hash(chat_id),
                    chat_id=chat_id,
                    message_id=str(row["message_id"]),
                    timestamp=int(row["timestamp"]),
                    text=text,
                    bot_text=bot_text,
                    graphemes=signals.graphemes,
                    question=signals.has_question_punct,
                    bot_asked=signals.bot_asked,
                    media_kind=signals.media_kind,
                    media_text=signals.media_text,
                    media_metadata_available=(
                        any(key in media for key in ("media_kind", "media_description", "voice_transcript"))
                        or not signals.has_media
                    ),
                    # The inbound JSONL files are session logs without conversation_state;
                    # live precedence for a reply to Arvid is exactly this legacy check.
                    priority_mode=(
                        str(media.get("priority_mode") or "")
                        or ("repair_feedback" if looks_like_repair_feedback(text) else "")
                    ),
                    candidate=signals.is_candidate(max_chars=max_chars),
                    legacy_mode=mode,
                    legacy_emoji=emoji,
                    actual=_actual(
                        processing, str(row["message_id"]), history_since_ms,
                        int(row["timestamp"]) * 1000,
                    ),
                )
            )
    return replays


def sample_for_labels(
    rows: Sequence[ReplayRow], *, size: int = 150, max_chars: int = 80, seed: int = 20260922
) -> list[ReplayRow]:
    """Every bot-question reply, up to 30 near-limit rows, the rest proportionally."""
    rng = random.Random(seed)
    strata: dict[str, list[ReplayRow]] = defaultdict(list)
    for row in rows:
        if row.priority_mode in {"repair_feedback", "group_member_bait"}:
            continue
        if row.bot_asked:
            strata["bot_asked"].append(row)
        elif row.media_kind and not row.media_text:
            continue
        elif max_chars < row.graphemes <= max_chars + 30:
            strata["near_limit"].append(row)
        elif row.question or row.graphemes > max_chars:
            continue
        elif row.legacy_mode in {"reply_ack", "low_content_reply"}:
            strata[row.legacy_mode].append(row)
        else:
            strata["other_candidate"].append(row)
    chosen = list(strata.pop("bot_asked", []))
    near = strata.pop("near_limit", [])
    chosen += rng.sample(near, min(30, len(near)))
    remaining = max(0, size - len(chosen))
    total = sum(len(pool) for pool in strata.values())
    for key in sorted(strata):
        pool = strata[key]
        take = min(len(pool), round(remaining * len(pool) / total)) if total else 0
        chosen += rng.sample(pool, take)
    return sorted(chosen, key=lambda row: (row.timestamp, row.row_id))


def write_label_sheet(rows: Sequence[ReplayRow], path: Path) -> None:
    with _private_text_file(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LABEL_FIELDS))
        writer.writeheader()
        for row in rows:
            record = asdict(row)
            record.pop("chat_id")
            record.pop("message_id")
            writer.writerow({**record, "label_action": "", "label_emojis": "", "notes": ""})


def _label(action: str, emojis: str) -> Label | None:
    action = str(action or "").strip().lower()
    if action not in LABEL_ACTIONS:
        return None
    return Label(action=action, emojis=tuple(str(emojis or "").split()))


def read_labels(path: Path) -> dict[str, Label]:
    # Spreadsheet apps may replace the file with their default permissions.
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    labels: dict[str, Label] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            row_id = str(record.get("row_id") or "").strip()
            label = _label(record.get("label_action", ""), record.get("label_emojis", ""))
            if row_id and label is not None:
                labels[row_id] = label
    return labels


def load_synthetic(path: Path) -> tuple[list[ReplayRow], dict[str, Label]]:
    """Owner-written translations, clearly marked synthetic (spec §10 step 2)."""
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    rows: list[ReplayRow] = []
    labels: dict[str, Label] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for index, record in enumerate(csv.DictReader(handle)):
            lang = str(record.get("lang") or "xx").strip()
            text = str(record.get("text") or "").strip()
            bot_text = str(record.get("bot_text") or "").strip()
            signals = compute_signals(content=text, reply_to_bot=True, reply_to_text=bot_text, metadata={})
            mode, emoji = _legacy(text, bot_text)
            row = ReplayRow(
                row_id=f"synthetic:{index}", chat=f"synthetic-{lang}", chat_id=f"synthetic-{lang}",
                message_id=f"synthetic-{index}", timestamp=index * 600, text=text,
                bot_text=bot_text, graphemes=signals.graphemes,
                question=signals.has_question_punct, bot_asked=signals.bot_asked,
                media_kind=signals.media_kind, media_text=signals.media_text,
                media_metadata_available=True,
                priority_mode="",
                candidate=signals.is_candidate(max_chars=80),
                legacy_mode=mode, legacy_emoji=emoji, actual="synthetic", synthetic=True,
            )
            rows.append(row)
            label = _label(record.get("label_action", ""), record.get("label_emojis", ""))
            if label is not None:
                labels[row.row_id] = label
    return rows, labels


def rows_to_jsonl(rows: Sequence[ReplayRow], path: Path) -> None:
    with _private_text_file(path) as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def rows_from_jsonl(path: Path) -> list[ReplayRow]:
    with path.open(encoding="utf-8") as handle:
        return [ReplayRow(**json.loads(line)) for line in handle if line.strip()]


def _row_is_candidate(row: ReplayRow, *, max_chars: int) -> bool:
    if row.priority_mode in {"repair_feedback", "group_member_bait"}:
        return False
    if row.question or row.bot_asked:
        return False
    if row.media_kind == "sticker":
        return row.media_text == "sticker"
    if row.media_kind and not row.media_text:
        return False
    return 1 <= row.graphemes <= max_chars


def load_effect_sequences(processing_db: Path, *, since_ms: int, chat_id: str | None = None):
    """Read confirmed send_reaction/send_text effects for the complete time window."""
    reactions, texts = [], []
    with closing(_ro(processing_db)) as processing:
        rows = processing.execute(
            "SELECT e.capability, e.target_json, e.payload_json, e.created_ms "
            "FROM effects e WHERE e.state = 'sent' AND e.created_ms >= ? "
            "AND e.capability IN ('send_reaction', 'send_text') "
            "AND EXISTS (SELECT 1 FROM transport_receipts tr "
            "WHERE tr.effect_id = e.effect_id) ORDER BY e.created_ms, e.effect_id",
            (int(since_ms),),
        ).fetchall()
    for row in rows:
        try:
            target = json.loads(row["target_json"] or "{}")
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        target_chat_id = str(target.get("chat_id") or "")
        if chat_id is not None and target_chat_id != chat_id:
            continue
        timestamp = int(row["created_ms"]) // 1000
        if not target_chat_id or not isinstance(payload, dict):
            continue
        if row["capability"] == "send_reaction" and payload.get("emoji"):
            reactions.append((target_chat_id, timestamp, str(payload["emoji"])))
        elif row["capability"] == "send_text" and payload.get("text"):
            texts.append((target_chat_id, timestamp, str(payload["text"])))
    return reactions, texts


@dataclass(frozen=True, slots=True)
class SimDecision:
    row_id: str
    timestamp: int
    kind: str
    chosen: str
    source: str
    reason: str
    verdict_action: str
    verdict_emojis: tuple[str, ...]
    error: str
    model_called: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    model_latency_ms: int


class _SimHistory:
    """The chat history the simulated reactor itself produced, newest first."""

    def __init__(self) -> None:
        self._rows: dict[str, list[RecentReaction]] = defaultdict(list)
        self._claims: set[tuple[str, str, str]] = set()

    def claim_short_reply(self, *, channel, chat_id, message_id, now_ms,
                          count, window_seconds, cooldown_seconds, mode="live"):
        key = (channel, chat_id, message_id)
        if key in self._claims:
            return ShortReplyClaim("duplicate")
        self._claims.add(key)
        if cooldown_active(
            [row.created_ms for row in self._rows[chat_id]], now_ms=now_ms,
            count=count, window_seconds=window_seconds, cooldown_seconds=cooldown_seconds,
        ):
            return ShortReplyClaim("cooldown")
        return ShortReplyClaim("claimed")

    def recent_reactions(self, *, channel: str, chat_id: str, since_ms: int, limit: int):
        return tuple(r for r in self._rows[chat_id] if r.created_ms >= since_ms)[:limit]

    def add(self, chat_id: str, emoji: str, created_ms: int) -> None:
        self._rows[chat_id].insert(0, RecentReaction(emoji=emoji, created_ms=created_ms))

    def complete_short_reply(self, *, channel, chat_id, message_id, outcome, emoji, now_ms):
        if outcome == "react" and emoji:
            self.add(chat_id, emoji, now_ms)


class _Recording:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.last: Any = None
        self.calls = 0

    async def decide(self, request: Any) -> Any:
        self.calls += 1
        self.last = await self.inner.decide(request)
        return self.last


async def simulate_decisions(
    rows: Sequence[ReplayRow], decider: Any, settings: Any
) -> list[SimDecision]:
    """Run the real reactor over the rows in time order, one simulated history per chat."""
    history = _SimHistory()
    recorder = _Recording(decider)
    now = [0]
    reactor = ShortReplyReactor(
        decider=recorder, history=history, settings=settings, clock=lambda: now[0]
    )
    decisions: list[SimDecision] = []
    for row in sorted(rows, key=lambda item: (item.timestamp, item.row_id)):
        now[0] = row.timestamp * 1000
        signals = compute_signals(
            content=row.text,
            reply_to_bot=True,
            reply_to_text=row.bot_text,
            metadata={
                "media_kind": row.media_kind,
                "media_description": row.media_text if row.media_kind != "audio" else "",
                "voice_transcript": row.media_text if row.media_kind == "audio" else "",
            },
        )
        if signals.bot_asked or signals.has_question_punct:
            decisions.append(
                SimDecision(
                    row_id=row.row_id, timestamp=row.timestamp, kind="answer",
                    chosen="", source="rule",
                    reason="deterministic", verdict_action="-", verdict_emojis=(),
                    error="", model_called=False, prompt_tokens=None,
                    completion_tokens=None, model_latency_ms=0,
                )
            )
            continue
        if not _row_is_candidate(row, max_chars=reactor.max_chars):
            protected = row.priority_mode in {"repair_feedback", "group_member_bait"}
            decisions.append(
                SimDecision(
                    row_id=row.row_id, timestamp=row.timestamp,
                    kind="silence" if protected else "answer", chosen="",
                    source="legacy" if protected else "rule",
                    reason="legacy_precedence" if protected else "not_candidate",
                    verdict_action="-", verdict_emojis=(), error="",
                    model_called=False, prompt_tokens=None,
                    completion_tokens=None, model_latency_ms=0,
                )
            )
            continue
        recorder.last = None
        calls_before = recorder.calls
        outcome = await reactor.decide(
            ReactorRequest(
                channel="whatsapp", chat_id=row.chat_id, message_id=row.message_id,
                thread_id="", turn_id="", signals=signals, bot_text=row.bot_text,
            )
        )
        verdict = recorder.last
        model_called = recorder.calls > calls_before
        usage = dict(getattr(verdict, "usage", {}) or {})
        decisions.append(
            SimDecision(
                row_id=row.row_id,
                timestamp=row.timestamp,
                kind=outcome.kind,
                chosen=outcome.emoji or "",
                source=outcome.source,
                reason=outcome.reason,
                verdict_action=str(getattr(verdict, "action", "-")),
                verdict_emojis=tuple(getattr(verdict, "emojis", ()) or ()),
                error=str(getattr(verdict, "error", "") or ""),
                model_called=model_called,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                model_latency_ms=int(getattr(verdict, "latency_ms", 0) or 0),
            )
        )
    return decisions


def decisions_to_jsonl(decisions: Sequence[SimDecision], path: Path) -> None:
    with _private_text_file(path) as handle:
        for decision in decisions:
            handle.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")


def decisions_from_jsonl(path: Path) -> list[SimDecision]:
    with path.open(encoding="utf-8") as handle:
        items = [json.loads(line) for line in handle if line.strip()]
    return [SimDecision(**{**item, "verdict_emojis": tuple(item["verdict_emojis"])}) for item in items]


@dataclass(frozen=True, slots=True)
class VariantMetrics:
    name: str
    labelled: int
    action_accuracy: float
    emoji_fit: float | None
    lost_answers: float | None
    unnecessary_answers: float
    unnecessary_reactions: float
    known_baseline_rows: int


@dataclass(frozen=True, slots=True)
class SequenceMetrics:
    top_share_by_chat_week: Mapping[str, float]
    entropy_bits_by_chat_week: Mapping[str, float]
    triple_repeats: int
    max_reactions_per_10min: int
    max_reactions_per_hour: int
    reaction_count: int
    text_top_share_by_chat_week: Mapping[str, float]
    text_triple_repeats: int
    text_emoji_count: int


@dataclass(frozen=True, slots=True)
class RolloutMetrics:
    baseline: SequenceMetrics
    shadow: SequenceMetrics
    baseline_receipts_complete: bool
    offline_failures: int
    observed_days: int
    expected_candidates: int
    logged_candidates: int
    shadow_dropped: int
    provider_errors: int
    invalid_json: int
    truncated: int
    fallbacks: int


def _predictions(
    rows: Sequence[ReplayRow], decisions: Sequence[SimDecision], *, max_chars: int
) -> dict[str, dict[str, tuple[str, str]]]:
    """Per variant: row_id -> (action, emoji). Non-candidates take the answer path."""
    by_id = {decision.row_id: decision for decision in decisions}
    variants: dict[str, dict[str, tuple[str, str]]] = {
        "V0_legacy": {}, "V1_first_candidate": {}, "V2_varied": {},
    }
    for row in rows:
        if row.actual.startswith("reaction:"):
            variants["V0_legacy"][row.row_id] = ("react", row.actual.removeprefix("reaction:"))
        elif row.actual == "text":
            variants["V0_legacy"][row.row_id] = ("answer", "")
        elif row.actual == "none":
            variants["V0_legacy"][row.row_id] = ("none", "")
        else:
            variants["V0_legacy"][row.row_id] = ("unknown", "")
        decision = by_id.get(row.row_id)
        non_candidate = not _row_is_candidate(row, max_chars=max_chars)
        if non_candidate or decision is None or decision.kind == "answer":
            predicted = ("answer", "")
            first = predicted
        elif decision.kind == "react":
            predicted = ("react", decision.chosen)
            first_emoji = (
                decision.verdict_emojis[0]
                if decision.source == "model" and decision.verdict_emojis
                else decision.chosen
            )
            first = ("react", first_emoji)
        else:
            predicted = ("none", "")
            first = predicted
        variants["V2_varied"][row.row_id] = predicted
        variants["V1_first_candidate"][row.row_id] = first
    return variants


#: List punctuation that may precede a line-leading status marker ("- 🟢 …", "1. ⚠️ …").
_LIST_PREFIX = regex.compile(r"^\s*(?:[-*•·–—]\s*|\d+[.)]\s*)?")


def message_emoji(text: str) -> str | None:
    """The one emoji a sent text contributes to the monotony metrics, if any.

    Owner decision 23.09. (spec §6.6): list and status markers do not count. An emoji run
    at the start of a line that is followed by more text on that line ("🟢 Kaufen",
    "- ⚠️ Risiko") is a marker. Of the remaining emojis a text counts at most one - its
    first - so emphasis like "😂😂😂" inside one message is not a triple.
    """
    for line in str(text or "").splitlines():
        body = _LIST_PREFIX.sub("", line, count=1)
        clusters = regex.findall(r"\X", body)
        index = 0
        while index < len(clusters) and (clusters[index].isspace() or emojis_in(clusters[index])):
            index += 1
        rest = "".join(clusters[index:])
        found = emojis_in(rest) if index and rest.strip() else emojis_in(line)
        if found:
            return found[0]
    return None


def sequence_metrics(reaction_events, text_events) -> SequenceMetrics:
    """Full chronological effects, grouped by chat/ISO week; never call a provider."""
    reactions: dict[str, list[tuple[int, str]]] = defaultdict(list)
    texts: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for chat_id, timestamp, emoji in reaction_events:
        reactions[str(chat_id)].append((int(timestamp), str(emoji)))
    for chat_id, timestamp, text in text_events:
        texts[str(chat_id)].append((int(timestamp), str(text)))

    def weekly_shares(events: dict[str, list[tuple[int, str]]]):
        by_week: dict[tuple[str, int, int], list[str]] = defaultdict(list)
        for chat_id, entries in events.items():
            for timestamp, value in sorted(entries, key=lambda item: item[0]):
                iso = datetime.fromtimestamp(
                    timestamp, ZoneInfo("Europe/Berlin")
                ).isocalendar()
                by_week[(chat_id, iso.year, iso.week)].append(value)
        shares: dict[str, float] = {}
        entropy: dict[str, float] = {}
        for (chat_id, year, week), values in by_week.items():
            key = f"{chat_id}:{year}-W{week:02d}"
            counts = Counter(values)
            if len(values) >= 5:
                shares[key] = max(counts.values()) / len(values)
            total = len(values)
            entropy[key] = -sum((n / total) * log2(n / total) for n in counts.values())
        return shares, entropy

    reaction_values = {
        chat: sorted(items, key=lambda item: item[0]) for chat, items in reactions.items()
    }
    top_share, entropy = weekly_shares(reaction_values)
    triples = max_10m = max_hour = 0
    count = 0
    for entries in reaction_values.values():
        count += len(entries)
        emojis = [emoji for _, emoji in entries]
        triples += sum(
            int(emojis[i] == emojis[i - 1] == emojis[i - 2])
            for i in range(2, len(emojis))
        )
        times = [timestamp for timestamp, _ in entries]
        for index, start in enumerate(times):
            max_10m = max(max_10m, sum(ts - start < 600 for ts in times[index:]))
            max_hour = max(max_hour, sum(ts - start < 3600 for ts in times[index:]))

    text_emojis: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for chat_id, entries in texts.items():
        for timestamp, text in sorted(entries, key=lambda item: item[0]):
            emoji = message_emoji(text)
            if emoji:
                text_emojis[chat_id].append((timestamp, emoji))
    text_share, _ = weekly_shares(text_emojis)
    text_count = sum(len(items) for items in text_emojis.values())
    text_triples = 0
    for entries in text_emojis.values():
        emojis = [emoji for _, emoji in sorted(entries, key=lambda item: item[0])]
        text_triples += sum(
            int(emojis[i] == emojis[i - 1] == emojis[i - 2])
            for i in range(2, len(emojis))
        )
    return SequenceMetrics(
        top_share_by_chat_week=top_share,
        entropy_bits_by_chat_week=entropy,
        triple_repeats=triples,
        max_reactions_per_10min=max_10m,
        max_reactions_per_hour=max_hour,
        reaction_count=count,
        text_top_share_by_chat_week=text_share,
        text_triple_repeats=text_triples,
        text_emoji_count=text_count,
    )


def evaluate(
    rows: Sequence[ReplayRow],
    labels: dict[str, Label],
    decisions: Sequence[SimDecision],
    *,
    max_chars: int,
) -> dict[str, VariantMetrics]:
    labelled = [
        row for row in rows
        if row.row_id in labels
        and row.priority_mode not in {"repair_feedback", "group_member_bait"}
    ]
    results: dict[str, VariantMetrics] = {}
    for name, predicted in _predictions(labelled, decisions, max_chars=max_chars).items():
        scored = [
            row for row in labelled
            if name != "V0_legacy" or predicted[row.row_id][0] != "unknown"
        ]
        total = len(scored) or 1
        correct = fit_hits = fit_total = lost = answer_labels = 0
        unnecessary_answers = unnecessary_reactions = 0
        for row in scored:
            label = labels[row.row_id]
            action, emoji = predicted[row.row_id]
            correct += int(action == label.action)
            if action == "react" and label.action == "react":
                fit_total += 1
                fit_hits += int(emoji in label.emojis)
            if label.action == "answer":
                answer_labels += 1
                lost += int(action != "answer")
            unnecessary_answers += int(action == "answer" and label.action != "answer")
            unnecessary_reactions += int(action == "react" and label.action == "none")
        results[name] = VariantMetrics(
            name=name,
            labelled=len(scored),
            action_accuracy=correct / total,
            emoji_fit=(fit_hits / fit_total) if fit_total else None,
            lost_answers=(lost / answer_labels) if answer_labels else None,
            unnecessary_answers=unnecessary_answers / total,
            unnecessary_reactions=unnecessary_reactions / total,
            known_baseline_rows=len(scored) if name == "V0_legacy" else 0,
        )
    return results


def load_shadow_decisions(
    log_paths: Sequence[Path], rows: Sequence[ReplayRow], *, chat_id: str,
) -> tuple[list[SimDecision], int, int, int, int]:
    """Return decisions, expected/logged in-scope replies, dropped count and observed days.

    The middleware logs exactly one ``reaction_decision`` line per direct reply in scope
    (decided, bypassed or not a candidate) with every provider id of a debounced batch in
    ``source_ids``; each replayed reply to Arvid is therefore expected once.
    """
    by_source = {(row.chat_id, row.message_id): row for row in rows if row.chat_id == chat_id}
    found: dict[str, SimDecision] = {}
    dropped = 0
    for path in log_paths:
        opener = (
            gzip.open(path, "rt", encoding="utf-8", errors="replace")
            if path.suffix == ".gz"
            else path.open(encoding="utf-8", errors="replace")
        )
        with opener as handle:
            for line in handle:
                marker = "reaction_decision mode=shadow "
                start = line.find(marker)
                if start < 0:
                    continue
                fields = {}
                for token in shlex.split(line[start + len("reaction_decision "):]):
                    if "=" in token:
                        key, value = token.split("=", 1)
                        fields[key] = value
                ids = {fields.get("message_id", "")} | set(
                    item for item in fields.get("source_ids", "").split(",") if item
                )
                matched = [
                    by_source[(fields.get("chat", ""), item)]
                    for item in sorted(ids)
                    if (fields.get("chat", ""), item) in by_source
                ]
                if not matched:
                    continue
                reason = fields.get("reason", "")
                if reason == "shadow_dropped":
                    dropped += 1
                outcome = fields.get("outcome", "silence")
                for row in matched:
                    found[row.row_id] = SimDecision(
                        row_id=row.row_id,
                        timestamp=row.timestamp,
                        kind=outcome if outcome in {"react", "answer"} else "silence",
                        chosen="" if fields.get("chosen", "-") == "-" else fields.get("chosen", ""),
                        source=fields.get("source", "none"),
                        reason=reason,
                        verdict_action=fields.get("decider_action", "-"),
                        verdict_emojis=(),
                        error=fields.get("error", ""),
                        model_called=fields.get("decider_action", "-") != "-",
                        prompt_tokens=None,
                        completion_tokens=None,
                        model_latency_ms=int(fields.get("model_latency_ms", "0") or 0),
                    )
    expected_rows = [row for row in by_source.values() if not row.synthetic]
    observed_days = len({
        datetime.fromtimestamp(row.timestamp, ZoneInfo("Europe/Berlin")).date()
        for row in expected_rows
    })
    return list(found.values()), len(expected_rows), len(found), dropped, observed_days


def go_no_go(
    metrics: dict[str, VariantMetrics], rollout: RolloutMetrics | None = None,
    probe: Sequence[SimDecision] = (), variant: str = "V2_varied",
) -> tuple[bool, list[str]]:
    """Final GO requires labeled quality, a complete shadow timeline and a clean probe."""
    candidate = metrics[variant]
    reasons: list[str] = []
    if candidate.emoji_fit is None or candidate.emoji_fit < 0.80:
        reasons.append(f"emoji_fit {candidate.emoji_fit} < 0.80")
    if candidate.lost_answers is None:
        reasons.append("labeled sample has no answer labels")
    elif candidate.lost_answers > 0.05:
        reasons.append(f"lost_answers {candidate.lost_answers:.2f} > 0.05")
    if len(probe) != 5 or any(
        not row.model_called or row.error or row.source == "fallback"
        or row.verdict_action not in {"react", "answer", "none"}
        for row in probe
    ):
        reasons.append("Gate B probe must contain five model calls with no errors or fallback")
    if rollout is None:
        reasons.append("complete shadow sequence metrics are required before Gate D")
        return False, reasons
    if not rollout.baseline_receipts_complete:
        reasons.append("baseline send_reaction/send_text receipt coverage is incomplete")
    if rollout.offline_failures:
        reasons.append(f"offline model errors/fallbacks={rollout.offline_failures} must be zero")
    if rollout.observed_days < 3:
        reasons.append("shadow observation must cover at least 3 local calendar days")
    if rollout.logged_candidates != rollout.expected_candidates:
        reasons.append(
            f"shadow candidate coverage {rollout.logged_candidates}/{rollout.expected_candidates} is incomplete"
        )
    if rollout.shadow_dropped:
        reasons.append(f"shadow dropped {rollout.shadow_dropped} candidate decisions")
    for name, count_value in (
        ("provider_errors", rollout.provider_errors), ("invalid_json", rollout.invalid_json),
        ("truncated", rollout.truncated), ("fallbacks", rollout.fallbacks),
    ):
        if count_value:
            reasons.append(f"shadow {name}={count_value} must be zero before Gate D")
    if rollout.shadow.reaction_count < 5:
        reasons.append("shadow has fewer than five reactions; extend observation before Gate D")
    if any(value > 0.35 for value in rollout.shadow.top_share_by_chat_week.values()):
        reasons.append("shadow top-emoji share exceeds 0.35 in a chat/week")
    if rollout.shadow.triple_repeats:
        reasons.append(f"shadow triple_repeats {rollout.shadow.triple_repeats} > 0")
    if rollout.shadow.max_reactions_per_10min > rollout.baseline.max_reactions_per_10min:
        reasons.append("shadow 10-minute reaction rate exceeds the legacy baseline")
    if rollout.shadow.max_reactions_per_hour > rollout.baseline.max_reactions_per_hour:
        reasons.append("shadow hourly reaction rate exceeds the legacy baseline")
    if rollout.baseline.text_emoji_count >= 5 and any(
        value > 0.35 for value in rollout.baseline.text_top_share_by_chat_week.values()
    ):
        reasons.append("text top-emoji share exceeds 0.35; complete Phase 3 before Gate D")
    if rollout.baseline.text_triple_repeats:
        reasons.append("text triple repeats exist; complete Phase 3 before Gate D")
    return (not reasons), reasons


def _percentile(values: Sequence[int], share: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def usage_summary(
    decisions: Sequence[SimDecision],
    *,
    price_in_per_mtok: float | None = None,
    price_out_per_mtok: float | None = None,
) -> dict[str, object]:
    called = [decision for decision in decisions if decision.model_called]
    prompt = [d.prompt_tokens for d in called if d.prompt_tokens is not None]
    completion = [d.completion_tokens for d in called if d.completion_tokens is not None]
    latency = [d.model_latency_ms for d in called if d.model_latency_ms]
    calls_by_day = Counter(
        datetime.fromtimestamp(d.timestamp, ZoneInfo("Europe/Berlin")).date().isoformat()
        for d in called
    )
    summary: dict[str, object] = {
        "calls": len(called),
        "model_errors": sum(bool(d.error) for d in called),
        "fallbacks": sum(d.source == "fallback" for d in called),
        "calls_per_day_europe_berlin": dict(sorted(calls_by_day.items())),
        "errors": dict(Counter(d.error for d in called if d.error)),
        "prompt_tokens_sum": sum(prompt),
        "completion_tokens_sum": sum(completion),
        "prompt_tokens_p50": _percentile(prompt, 0.5),
        "prompt_tokens_p95": _percentile(prompt, 0.95),
        "completion_tokens_p50": _percentile(completion, 0.5),
        "completion_tokens_p95": _percentile(completion, 0.95),
        "latency_ms_p50": _percentile(latency, 0.5),
        "latency_ms_p95": _percentile(latency, 0.95),
    }
    if price_in_per_mtok is not None and price_out_per_mtok is not None:
        summary["cost_usd"] = (
            sum(prompt) * price_in_per_mtok + sum(completion) * price_out_per_mtok
        ) / 1_000_000
    return summary


def receipt_coverage(
    processing_db: Path, *, since_ms: int, chat_id: str | None = None
) -> dict[str, tuple[int, int]]:
    """Per capability: (sent effects, sent effects with a transport receipt)."""
    chat_clause = ""
    parameters: list[object] = [int(since_ms)]
    if chat_id is not None:
        chat_clause = " AND json_extract(e.target_json, '$.chat_id') = ?"
        parameters.append(chat_id)
    with closing(_ro(processing_db)) as processing:
        rows = processing.execute(
            "SELECT e.capability, COUNT(DISTINCT e.effect_id), "
            "COUNT(DISTINCT CASE WHEN tr.receipt_id IS NOT NULL THEN e.effect_id END) "
            "FROM effects e "
            "LEFT JOIN transport_receipts tr USING (effect_id) "
            "WHERE e.state = 'sent' AND e.created_ms >= ?" + chat_clause +
            " GROUP BY e.capability",
            parameters,
        ).fetchall()
    return {str(row[0]): (int(row[1]), int(row[2] or 0)) for row in rows}


def render_markdown(
    by_max_chars: dict[int, dict[str, VariantMetrics]],
    usage: dict[str, object],
    coverage: dict[str, tuple[int, int]],
    rollout: RolloutMetrics | None,
    probe: Sequence[SimDecision],
    media_coverage: tuple[int, int],
) -> str:
    def fmt(value: float | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    lines = ["# Short-reply evaluation", ""]
    media_rows, media_missing = media_coverage
    lines += [
        "## Replay media metadata", "",
        f"- Media rows: {media_rows}",
        f"- Rows without joined inbound media metadata: {media_missing}",
        "- Missing-metadata media rows are excluded from short-reply candidate labels; this report does not claim full media-population coverage." if media_missing else "- Joined metadata covers every replayed media row.",
        "",
    ]
    for max_chars, metrics in sorted(by_max_chars.items()):
        ok, reasons = go_no_go(metrics, rollout=rollout, probe=probe)
        lines += [
            f"## maxChars = {max_chars}: {'FINAL GO' if ok else 'NO-GO'}",
            "",
            "| Variant | n | action acc. | emoji fit | lost answers | unnecessary answers "
            "| unnecessary reactions | known V0 rows |",
            "|---|---|---|---|---|---|---|",
        ]
        for m in metrics.values():
            lines.append(
                f"| {m.name} | {m.labelled} | {fmt(m.action_accuracy)} | {fmt(m.emoji_fit)} "
                f"| {fmt(m.lost_answers)} | {fmt(m.unnecessary_answers)} "
                f"| {fmt(m.unnecessary_reactions)} | {m.known_baseline_rows} |"
            )
        if reasons:
            lines += ["", "Failed: " + "; ".join(reasons)]
        lines.append("")
    if rollout is None:
        lines += ["## Full shadow sequence", "", "Unavailable; offline labeled metrics cannot authorize Gate D.", ""]
    else:
        lines += ["## Full shadow sequence", "", "```", json.dumps(asdict(rollout), indent=2), "```", ""]
    lines += ["## Model usage (provider-reported)", "", "```", json.dumps(usage, indent=2), "```", ""]
    lines += ["## Effect/receipt coverage (sent → with receipt)", ""]
    lines += [f"- {cap}: {sent} → {receipted}" for cap, (sent, receipted) in sorted(coverage.items())]
    return "\n".join(lines) + "\n"
