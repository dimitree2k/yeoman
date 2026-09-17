"""Durable state for detached Hermes research polling.

This is deliberately separate from :mod:`processing.store`: research polling is a
service concern, not a processing-journal concern, and must not change that schema.
Each operation opens its own short-lived SQLite connection so the store can safely be
used by the gateway task and by a replacement process after restart.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PendingResearch:
    task_id: str
    worker: str
    skill: str
    context_id: str
    reference_task_ids: tuple[str, ...]
    channel: str
    chat_id: str
    effect_id: str
    #: What the owner actually asked, and when. A long run can finish while the chat has moved
    #: on to something else, so the delivered result is labelled as a follow-up to this request.
    question: str = ""
    created_ms: int = 0
    canonical_user_id: str = ""
    symbol: str = ""
    length: str = "short"
    thread_id: str = ""


class A2AResearchStore:
    """Small durable outbox for long-running A2A research tasks."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_research (
                    task_id TEXT PRIMARY KEY,
                    worker TEXT NOT NULL,
                    skill TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    reference_task_ids TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    question TEXT NOT NULL DEFAULT '',
                    created_ms INTEGER NOT NULL DEFAULT 0
                    ,canonical_user_id TEXT NOT NULL DEFAULT ''
                    ,symbol TEXT NOT NULL DEFAULT ''
                    ,length TEXT NOT NULL DEFAULT 'short'
                    ,thread_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS completed_reports (
                    effect_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    card TEXT NOT NULL DEFAULT '',
                    signal TEXT NOT NULL DEFAULT '',
                    completed_ms INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(pending_research)")
            }
            for column, definition in (
                ("question", "TEXT NOT NULL DEFAULT ''"),
                ("created_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("canonical_user_id", "TEXT NOT NULL DEFAULT ''"),
                ("symbol", "TEXT NOT NULL DEFAULT ''"),
                ("length", "TEXT NOT NULL DEFAULT 'short'"),
                ("thread_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE pending_research ADD COLUMN {column} {definition}"
                    )
            report_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(completed_reports)")}
            for column, definition in (
                ("canonical_user_id", "TEXT NOT NULL DEFAULT ''"),
                ("symbol", "TEXT NOT NULL DEFAULT ''"),
                ("card", "TEXT NOT NULL DEFAULT ''"),
                ("signal", "TEXT NOT NULL DEFAULT ''"),
                ("completed_ms", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in report_columns:
                    connection.execute(f"ALTER TABLE completed_reports ADD COLUMN {column} {definition}")

    def put(self, pending: PendingResearch) -> None:
        if not pending.task_id or not pending.worker or not pending.skill or not pending.context_id:
            raise ValueError("pending research task correlation is incomplete")
        if not pending.effect_id:
            raise ValueError("pending research effect correlation is required")
        references = json.dumps(list(pending.reference_task_ids), separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_research
                    (task_id, worker, skill, context_id, reference_task_ids,
                     channel, chat_id, effect_id, question, created_ms, canonical_user_id, symbol,
                     length, thread_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    worker=excluded.worker,
                    skill=excluded.skill,
                    context_id=excluded.context_id,
                    reference_task_ids=excluded.reference_task_ids,
                    channel=excluded.channel,
                    chat_id=excluded.chat_id,
                    effect_id=excluded.effect_id,
                    question=excluded.question,
                    created_ms=excluded.created_ms,
                    canonical_user_id=excluded.canonical_user_id,
                    symbol=excluded.symbol,
                    length=excluded.length,
                    thread_id=excluded.thread_id
                """,
                (
                    pending.task_id,
                    pending.worker,
                    pending.skill,
                    pending.context_id,
                    references,
                    pending.channel,
                    pending.chat_id,
                    pending.effect_id,
                    pending.question,
                    int(pending.created_ms),
                    pending.canonical_user_id,
                    pending.symbol,
                    pending.length,
                    pending.thread_id,
                ),
            )

    def pending(self) -> tuple[PendingResearch, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, worker, skill, context_id, reference_task_ids,
                       channel, chat_id, effect_id, question, created_ms, canonical_user_id, symbol,
                       length, thread_id
                FROM pending_research
                ORDER BY rowid
                """
            ).fetchall()
        result: list[PendingResearch] = []
        for row in rows:
            references = json.loads(str(row["reference_task_ids"]))
            if not isinstance(references, list) or not all(
                isinstance(item, str) for item in references
            ):
                raise ValueError("pending research reference ids are corrupt")
            result.append(
                PendingResearch(
                    task_id=str(row["task_id"]),
                    worker=str(row["worker"]),
                    skill=str(row["skill"]),
                    context_id=str(row["context_id"]),
                    reference_task_ids=tuple(references),
                    channel=str(row["channel"]),
                    chat_id=str(row["chat_id"]),
                    effect_id=str(row["effect_id"]),
                    question=str(row["question"] or ""),
                    created_ms=int(row["created_ms"] or 0),
                    canonical_user_id=str(row["canonical_user_id"] or ""),
                    symbol=str(row["symbol"] or ""),
                    length=str(row["length"] or "short"),
                    thread_id=str(row["thread_id"] or ""),
                )
            )
        return tuple(result)

    def delete(self, task_id: str, *, effect_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_research WHERE task_id = ? AND effect_id = ?",
                (str(task_id), str(effect_id)),
            )
            return cursor.rowcount == 1

    def save_report(self, effect_id: str, *, channel: str, chat_id: str, content: str,
                    canonical_user_id: str = "", symbol: str = "", card: str = "", signal: str = "") -> None:
        if not effect_id or not channel or not chat_id or not content:
            raise ValueError("completed report correlation and content are required")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO completed_reports(effect_id, channel, chat_id, content, canonical_user_id, symbol, card, signal, completed_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(effect_id) DO NOTHING",
                (effect_id, channel, chat_id, content, canonical_user_id, symbol, card, signal, int(time.time() * 1000)),
            )

    def report(self, effect_id: str, *, channel: str, chat_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content FROM completed_reports WHERE effect_id=? AND channel=? AND chat_id=?",
                (effect_id, channel, chat_id),
            ).fetchone()
        return str(row["content"]) if row is not None else None

    @staticmethod
    def _variant(content: str, card: str, length: str) -> str:
        if length == "short":
            return card or content
        if length == "long" and len(content) > 2_000:
            cutoff = 1_999
            boundary = content.rfind("\n", 0, cutoff)
            if boundary < 1_000:
                boundary = cutoff
            return content[: max(1, boundary)].rstrip() + "…"
        return content

    def cached_card(self, canonical_user_id: str, symbol: str, *, max_age_ms: int = 86_400_000) -> str | None:
        if not canonical_user_id or not symbol:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT signal FROM completed_reports WHERE canonical_user_id=? AND symbol=? AND signal<>'' "
                "AND completed_ms>=? ORDER BY completed_ms DESC LIMIT 1",
                (canonical_user_id, symbol, int(time.time() * 1000) - max_age_ms),
            ).fetchone()
        if row is None:
            return None
        signal = str(row["signal"])
        return f"{symbol}: {signal} (Signal aus der letzten Analyse)" if signal in {"BUY", "HOLD", "SELL", "KAUFEN", "HALTEN", "VERKAUFEN"} else None

    def has_recent_report(self, canonical_user_id: str, symbol: str, *, max_age_ms: int = 86_400_000) -> bool:
        if not canonical_user_id or not symbol:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM completed_reports WHERE canonical_user_id=? AND symbol=? "
                "AND completed_ms>=? LIMIT 1",
                (canonical_user_id, symbol, int(time.time() * 1000) - max_age_ms),
            ).fetchone()
        return row is not None

    def report_for_quote(self, processing_store: Any, *, channel: str, chat_id: str,
                         provider_message_id: str, quoted_text: str = "",
                         length: str = "full") -> str | None:
        for outbound_effect_id in processing_store.effects_by_provider_message(channel, chat_id, provider_message_id):
            effect = processing_store.get_effect(outbound_effect_id)
            operation_key = effect.operation_key if effect is not None else ""
            match = re.search(r"a2a-result:(a2a-[0-9a-f]{32})(?::|$)", operation_key)
            if match:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT content, card FROM completed_reports WHERE effect_id=? AND channel=? AND chat_id=?",
                        (match.group(1), channel, chat_id),
                    ).fetchone()
                if row is None:
                    return ""
                return self._variant(
                    str(row["content"]), str(row["card"] or ""), length
                )
        if quoted_text:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT content, card FROM completed_reports WHERE channel=? AND chat_id=? AND card<>''",
                    (channel, chat_id),
                ).fetchall()
            matches = [
                self._variant(str(row["content"]), str(row["card"]), length)
                for row in rows
                if str(row["card"])[:40] in quoted_text
            ]
            return matches[0] if len(matches) == 1 else ""
        return None


def sibling_path(processing_store: Any) -> str | None:
    """Derive the A2A DB beside a provided processing store, if it has a path."""

    raw_path = getattr(processing_store, "path", None)
    if not raw_path or str(raw_path) == ":memory:":
        return None
    return str(Path(str(raw_path)).with_name("a2a-research.db"))


__all__ = ["A2AResearchStore", "PendingResearch", "sibling_path"]
