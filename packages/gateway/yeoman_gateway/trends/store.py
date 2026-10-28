"""SQLite store for scored trend items and the cards that passed the bar."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .models import TrendCard, TrendItem, TrendVerdict

#: A topic scored once is not rescored while it keeps trending for a few days.
SEEN_RETENTION_MS = 7 * 24 * 3600 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scored_items (
    item_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    score INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    scored_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    card_id TEXT PRIMARY KEY,
    item_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    score INTEGER NOT NULL,
    kind TEXT NOT NULL,
    hook TEXT NOT NULL,
    line TEXT NOT NULL,
    url TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_expiry ON cards(expires_at_ms);
CREATE TABLE IF NOT EXISTS card_uses (
    card_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    used_at_ms INTEGER NOT NULL,
    PRIMARY KEY (card_id, channel, chat_id)
);
CREATE TABLE IF NOT EXISTS card_fits (
    card_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    topics_key TEXT NOT NULL,
    fits INTEGER NOT NULL,
    PRIMARY KEY (card_id, channel, chat_id, topics_key)
);
"""


def card_id_for(item_key: str) -> str:
    return hashlib.sha256(item_key.encode("utf-8")).hexdigest()[:16]


class TrendStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def unseen(self, items: Iterable[TrendItem]) -> list[TrendItem]:
        """Items not scored yet, first occurrence of each key only."""
        first: dict[str, TrendItem] = {}
        for item in items:
            first.setdefault(item.key, item)
        unique = list(first.values())
        if not unique:
            return []
        with self._lock:
            placeholders = ",".join("?" for _ in unique)
            seen = {
                row["item_key"]
                for row in self._conn.execute(
                    f"SELECT item_key FROM scored_items WHERE item_key IN ({placeholders})",
                    [item.key for item in unique],
                )
            }
        return [item for item in unique if item.key not in seen]

    def record(
        self,
        items: Iterable[TrendItem],
        verdicts: Iterable[TrendVerdict],
        *,
        min_score: int,
        now_ms: int,
        ttl_ms: int,
    ) -> list[TrendCard]:
        """Store every verdict and return the cards created for those at the bar."""
        by_key = {item.key: item for item in items}
        cards: list[TrendCard] = []
        with self._lock, self._conn:
            for verdict in verdicts:
                item = by_key.get(verdict.item_key)
                if item is None:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO scored_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item.key, item.source, item.title, verdict.score, verdict.kind,
                     verdict.reason, now_ms),
                )
                if verdict.score < min_score or not verdict.url or not verdict.line:
                    continue
                card = TrendCard(
                    card_id=card_id_for(item.key),
                    item_key=item.key,
                    source=item.source,
                    title=item.title,
                    score=verdict.score,
                    kind=verdict.kind,
                    hook=verdict.hook,
                    line=verdict.line,
                    url=verdict.url,
                    created_at_ms=now_ms,
                    expires_at_ms=now_ms + ttl_ms,
                    tags=verdict.tags,
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (card.card_id, card.item_key, card.source, card.title, card.score,
                     card.kind, card.hook, card.line, card.url, json.dumps(list(card.tags)),
                     card.created_at_ms, card.expires_at_ms),
                )
                cards.append(card)
        return cards

    def active_cards(self, *, now_ms: int, limit: int = 50) -> list[TrendCard]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE expires_at_ms > ? "
                "ORDER BY score DESC, created_at_ms DESC LIMIT ?",
                (now_ms, limit),
            ).fetchall()
        return [_card(row) for row in rows]

    def unused_cards_for(
        self, channel: str, chat_id: str, *, now_ms: int, limit: int = 5
    ) -> list[TrendCard]:
        """Fresh cards this chat has not seen yet, best first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE expires_at_ms > ? AND card_id NOT IN "
                "(SELECT card_id FROM card_uses WHERE channel = ? AND chat_id = ?) "
                "ORDER BY score DESC, created_at_ms DESC LIMIT ?",
                (now_ms, channel, chat_id, limit),
            ).fetchall()
        return [_card(row) for row in rows]

    def mark_used(self, card_id: str, channel: str, chat_id: str, *, now_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO card_uses VALUES (?, ?, ?, ?)",
                (card_id, channel, chat_id, now_ms),
            )

    def unjudged_cards_for(
        self, channel: str, chat_id: str, topics_key: str, *, now_ms: int, limit: int = 40
    ) -> list[TrendCard]:
        """Fresh, unused cards nobody has checked against this chat's topics yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE expires_at_ms > ? AND card_id NOT IN "
                "(SELECT card_id FROM card_uses WHERE channel = ? AND chat_id = ?) "
                "AND card_id NOT IN (SELECT card_id FROM card_fits WHERE channel = ? "
                "AND chat_id = ? AND topics_key = ?) "
                "ORDER BY score DESC, created_at_ms DESC LIMIT ?",
                (now_ms, channel, chat_id, channel, chat_id, topics_key, limit),
            ).fetchall()
        return [_card(row) for row in rows]

    def record_fits(
        self,
        channel: str,
        chat_id: str,
        topics_key: str,
        judged: Iterable[str],
        fitting: set[str],
    ) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO card_fits VALUES (?, ?, ?, ?, ?)",
                [
                    (card_id, channel, chat_id, topics_key, int(card_id in fitting))
                    for card_id in judged
                ],
            )

    def fitting_card_ids(self, channel: str, chat_id: str, topics_key: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_id FROM card_fits WHERE channel = ? AND chat_id = ? "
                "AND topics_key = ? AND fits = 1",
                (channel, chat_id, topics_key),
            ).fetchall()
        return {str(row["card_id"]) for row in rows}

    def last_offer_ms(self, channel: str, chat_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(used_at_ms) AS last FROM card_uses WHERE channel = ? AND chat_id = ?",
                (channel, chat_id),
            ).fetchone()
        return int(row["last"]) if row is not None and row["last"] is not None else None

    def offered_card(
        self, card_id: str, channel: str, chat_id: str
    ) -> tuple[TrendCard, int] | None:
        """A card with the time it was offered to this chat; ``None`` if it never was."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cards.*, card_uses.used_at_ms AS used_at_ms FROM cards "
                "JOIN card_uses ON card_uses.card_id = cards.card_id "
                "WHERE cards.card_id = ? AND card_uses.channel = ? AND card_uses.chat_id = ?",
                (card_id, channel, chat_id),
            ).fetchone()
        if row is None:
            return None
        return _card(row), int(row["used_at_ms"])

    def recent_scores(self, *, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM scored_items ORDER BY scored_at_ms DESC, score DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def prune(self, *, now_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM scored_items WHERE scored_at_ms < ?", (now_ms - SEEN_RETENTION_MS,)
            )
            self._conn.execute("DELETE FROM cards WHERE expires_at_ms <= ?", (now_ms,))
            self._conn.execute(
                "DELETE FROM card_uses WHERE card_id NOT IN (SELECT card_id FROM cards)"
            )
            self._conn.execute(
                "DELETE FROM card_fits WHERE card_id NOT IN (SELECT card_id FROM cards)"
            )


def _card(row: sqlite3.Row) -> TrendCard:
    return TrendCard(
        card_id=row["card_id"],
        item_key=row["item_key"],
        source=row["source"],
        title=row["title"],
        score=int(row["score"]),
        kind=row["kind"],
        hook=row["hook"],
        line=row["line"],
        url=row["url"],
        created_at_ms=int(row["created_at_ms"]),
        expires_at_ms=int(row["expires_at_ms"]),
        tags=tuple(json.loads(row["tags_json"] or "[]")),
    )
