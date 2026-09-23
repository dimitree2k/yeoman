"""Offline evaluation of the short-reply path on historical data (spec 2026-09-22 §10).

Read-only against the runtime databases. Nothing here sends a chat message; only
``simulate_decisions`` calls a model, and only through the decider its caller passes.
The legacy regex helpers are used only for V0 baseline reconstruction and label-sample stratification; they never select live candidates or decide a new-path action.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from yeoman_gateway.implicit_addressing import (
    looks_like_low_content_reply,
    looks_like_question_or_request,
    looks_like_repair_feedback,
    looks_like_reply_ack,
    reaction_for_reply_ack,
)
from yeoman_gateway.processing.models import RecentReaction, ShortReplyClaim
from yeoman_gateway.short_reply.reactor import ReactorRequest, ShortReplyReactor
from yeoman_gateway.short_reply.signals import compute_signals
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
