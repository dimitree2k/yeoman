"""Private ``ops/sentiment.db``: outbound metadata, weekly numbers and labels.

No message text is ever written here. Person references are contact ids, message
references are history/provider ids, and the callers keep live chat text outside the
database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
#: One alert per chat per day is a hard rate limit, not a schedule.
ALERT_WINDOW_MS = 24 * 60 * 60 * 1000

_DDL = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS outbound_meta (
        chat_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        sent_ms INTEGER NOT NULL,
        path TEXT NOT NULL,
        action_type TEXT NOT NULL DEFAULT 'unknown',
        persona_sha TEXT NOT NULL DEFAULT 'unknown',
        PRIMARY KEY (chat_id, message_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbound_meta_chat_time "
    "ON outbound_meta(chat_id, sent_ms)",
    """CREATE TABLE IF NOT EXISTS weekly (
        chat_id TEXT NOT NULL,
        iso_week TEXT NOT NULL,
        computed_at_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        persona_file TEXT NOT NULL DEFAULT '',
        persona_sha TEXT NOT NULL DEFAULT 'unknown',
        policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
        metrics_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (chat_id, iso_week)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weekly_week ON weekly(iso_week)",
    """CREATE TABLE IF NOT EXISTS labels (
        chat_id TEXT NOT NULL,
        iso_week TEXT NOT NULL,
        arvid_message_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        responds_to_arvid TEXT NOT NULL,
        stance TEXT NOT NULL,
        signals_json TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        prompt_version TEXT NOT NULL DEFAULT '',
        labeled_at_ms INTEGER NOT NULL,
        PRIMARY KEY (chat_id, iso_week, arvid_message_id, message_id, prompt_version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_labels_chat_week ON labels(chat_id, iso_week)",
    "CREATE INDEX IF NOT EXISTS idx_labels_message ON labels(message_id)",
    """CREATE TABLE IF NOT EXISTS alerts (
        chat_id TEXT PRIMARY KEY,
        last_alert_ms INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS persona_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        persona_sha TEXT NOT NULL,
        changed_ms INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_persona_changes_chat "
    "ON persona_changes(chat_id, changed_ms)",
    """CREATE TABLE IF NOT EXISTS persona_reports (
        chat_id TEXT NOT NULL,
        persona_sha TEXT NOT NULL,
        reported_ms INTEGER NOT NULL,
        PRIMARY KEY (chat_id, persona_sha)
    )""",
)


@dataclass(frozen=True, slots=True)
class WeeklyRow:
    chat_id: str
    iso_week: str
    computed_at_ms: int
    status: str
    persona_file: str
    persona_sha: str
    policy_snapshot: dict[str, Any]
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LabelRow:
    chat_id: str
    iso_week: str
    arvid_message_id: str
    message_id: str
    responds_to_arvid: str
    stance: str
    signals: tuple[str, ...]
    confidence: float
    model: str
    prompt_version: str
    labeled_at_ms: int


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _weekly(row: sqlite3.Row) -> WeeklyRow:
    return WeeklyRow(
        chat_id=str(row["chat_id"]),
        iso_week=str(row["iso_week"]),
        computed_at_ms=int(row["computed_at_ms"]),
        status=str(row["status"]),
        persona_file=str(row["persona_file"]),
        persona_sha=str(row["persona_sha"]),
        policy_snapshot=_decode(row["policy_snapshot_json"], {}),
        metrics=_decode(row["metrics_json"], {}),
    )


def _label(row: sqlite3.Row) -> LabelRow:
    signals = _decode(row["signals_json"], [])
    return LabelRow(
        chat_id=str(row["chat_id"]),
        iso_week=str(row["iso_week"]),
        arvid_message_id=str(row["arvid_message_id"]),
        message_id=str(row["message_id"]),
        responds_to_arvid=str(row["responds_to_arvid"]),
        stance=str(row["stance"]),
        signals=tuple(str(item) for item in signals if isinstance(item, str)),
        confidence=float(row["confidence"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        labeled_at_ms=int(row["labeled_at_ms"]),
    )


class SentimentStore:
    """Open ``ops/sentiment.db`` lazily: 0600, WAL, versioned schema."""

    def __init__(self, path: Path | None = None, *, read_only: bool = False) -> None:
        self._path = Path(path) if path is not None else None
        self._read_only = bool(read_only)
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            from yeoman_shared.utils.helpers import get_operational_store_path

            self._path = get_operational_store_path("sentiment")
        return self._path

    def _db(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self._read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            self._conn = conn
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            _migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- outbound metadata ---------------------------------------------------------

    def record_outbound(
        self,
        *,
        chat_id: str,
        message_id: str,
        sent_ms: int,
        path: str,
        action_type: str = "unknown",
        persona_sha: str = "unknown",
    ) -> None:
        """One row per provider message id; a retry updates instead of duplicating."""
        if not chat_id or not message_id:
            return
        self._db().execute(
            "INSERT INTO outbound_meta(chat_id, message_id, sent_ms, path, action_type, "
            "persona_sha) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, message_id) DO UPDATE SET sent_ms=excluded.sent_ms, "
            "path=excluded.path, action_type=excluded.action_type, "
            "persona_sha=excluded.persona_sha",
            (chat_id, message_id, int(sent_ms), path, action_type, persona_sha),
        )

    def outbound_meta(
        self, chat_id: str | None = None, *, since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Provider message id to metadata, for one chat or all chats."""
        conditions: list[str] = []
        parameters: list[Any] = []
        if chat_id is not None:
            conditions.append("chat_id=?")
            parameters.append(chat_id)
        if since_ms is not None:
            conditions.append("sent_ms>=?")
            parameters.append(int(since_ms))
        if until_ms is not None:
            conditions.append("sent_ms<?")
            parameters.append(int(until_ms))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._db().execute(
            f"SELECT * FROM outbound_meta{where}", parameters
        ).fetchall()
        return {
            str(row["message_id"]): {
                "chat_id": str(row["chat_id"]),
                "sent_ms": int(row["sent_ms"]),
                "path": str(row["path"]),
                "action_type": str(row["action_type"]),
                "persona_sha": str(row["persona_sha"]),
            }
            for row in rows
        }

    # -- weekly numbers ------------------------------------------------------------

    def store_weekly(
        self,
        *,
        chat_id: str,
        iso_week: str,
        computed_at_ms: int,
        status: str,
        persona_file: str,
        persona_sha: str,
        policy_snapshot: dict[str, Any],
        metrics: dict[str, Any],
    ) -> None:
        self._db().execute(
            "INSERT INTO weekly(chat_id, iso_week, computed_at_ms, status, persona_file, "
            "persona_sha, policy_snapshot_json, metrics_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, iso_week) DO UPDATE SET "
            "computed_at_ms=excluded.computed_at_ms, status=excluded.status, "
            "persona_file=excluded.persona_file, persona_sha=excluded.persona_sha, "
            "policy_snapshot_json=excluded.policy_snapshot_json, "
            "metrics_json=excluded.metrics_json",
            (
                chat_id, iso_week, int(computed_at_ms), status, persona_file, persona_sha,
                json.dumps(policy_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            ),
        )

    def weekly(
        self, chat_id: str | None = None, *, weeks: int | None = None
    ) -> tuple[WeeklyRow, ...]:
        conditions = " WHERE chat_id=?" if chat_id is not None else ""
        parameters: list[Any] = [chat_id] if chat_id is not None else []
        rows = self._db().execute(
            f"SELECT * FROM weekly{conditions} ORDER BY iso_week DESC, chat_id",
            parameters,
        ).fetchall()
        result = [_weekly(row) for row in rows]
        if weeks is not None:
            result = result[: max(0, int(weeks))]
        return tuple(result)

    def has_weekly(self, *, chat_id: str, iso_week: str) -> bool:
        row = self._db().execute(
            "SELECT 1 FROM weekly WHERE chat_id=? AND iso_week=?", (chat_id, iso_week)
        ).fetchone()
        return row is not None

    # -- labels --------------------------------------------------------------------

    def store_label(
        self,
        *,
        chat_id: str,
        iso_week: str,
        arvid_message_id: str,
        message_id: str,
        responds_to_arvid: str,
        stance: str,
        signals: tuple[str, ...] | list[str],
        confidence: float,
        model: str,
        prompt_version: str,
        labeled_at_ms: int,
    ) -> None:
        self._db().execute(
            "INSERT INTO labels(chat_id, iso_week, arvid_message_id, message_id, "
            "responds_to_arvid, stance, signals_json, confidence, model, prompt_version, "
            "labeled_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, iso_week, arvid_message_id, message_id, prompt_version) "
            "DO UPDATE SET responds_to_arvid=excluded.responds_to_arvid, "
            "stance=excluded.stance, signals_json=excluded.signals_json, "
            "confidence=excluded.confidence, model=excluded.model, "
            "labeled_at_ms=excluded.labeled_at_ms",
            (
                chat_id, iso_week, arvid_message_id, message_id, responds_to_arvid, stance,
                json.dumps(list(signals), ensure_ascii=False), float(confidence), model,
                prompt_version, int(labeled_at_ms),
            ),
        )

    def labels(
        self, chat_id: str | None = None, *, iso_week: str | None = None,
        prompt_version: str | None = None,
    ) -> tuple[LabelRow, ...]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("chat_id", chat_id), ("iso_week", iso_week),
            ("prompt_version", prompt_version),
        ):
            if value is not None:
                conditions.append(f"{column}=?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._db().execute(
            f"SELECT * FROM labels{where} ORDER BY labeled_at_ms, message_id", parameters
        ).fetchall()
        return tuple(_label(row) for row in rows)

    def labeled_message_ids(
        self, *, chat_id: str, iso_week: str, prompt_version: str
    ) -> set[str]:
        rows = self._db().execute(
            "SELECT DISTINCT message_id FROM labels WHERE chat_id=? AND iso_week=? "
            "AND prompt_version=?",
            (chat_id, iso_week, prompt_version),
        ).fetchall()
        return {str(row["message_id"]) for row in rows}

    # -- alerts --------------------------------------------------------------------

    def claim_alert(
        self, *, chat_id: str, now_ms: int, window_ms: int = ALERT_WINDOW_MS
    ) -> bool:
        """Atomically claim the one alert this chat may receive per window."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT last_alert_ms FROM alerts WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is not None and int(now_ms) - int(row["last_alert_ms"]) < int(window_ms):
                db.execute("COMMIT")
                return False
            db.execute(
                "INSERT INTO alerts(chat_id, last_alert_ms) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_alert_ms=excluded.last_alert_ms",
                (chat_id, int(now_ms)),
            )
            db.execute("COMMIT")
            return True
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def claim_digest(self, iso_week: str) -> bool:
        """Claim the owner digest for ``iso_week`` once, across restarts."""
        cursor = self._db().execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (f"digest_sent:{iso_week}", str(now_ms())),
        )
        return cursor.rowcount == 1

    def last_alert_ms(self, chat_id: str) -> int | None:
        row = self._db().execute(
            "SELECT last_alert_ms FROM alerts WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return int(row["last_alert_ms"]) if row is not None else None

    # -- persona history -----------------------------------------------------------

    def note_persona(self, *, chat_id: str, persona_sha: str, now_ms: int) -> bool:
        """Record a persona change; true only when the sha actually changed."""
        db = self._db()
        row = db.execute(
            "SELECT persona_sha FROM persona_changes WHERE chat_id=? "
            "ORDER BY changed_ms DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row is not None and str(row["persona_sha"]) == persona_sha:
            return False
        db.execute(
            "INSERT INTO persona_changes(chat_id, persona_sha, changed_ms) VALUES (?, ?, ?)",
            (chat_id, persona_sha, int(now_ms)),
        )
        return True

    def persona_changes(self, chat_id: str) -> tuple[tuple[str, int], ...]:
        rows = self._db().execute(
            "SELECT persona_sha, changed_ms FROM persona_changes WHERE chat_id=? "
            "ORDER BY changed_ms, id",
            (chat_id,),
        ).fetchall()
        return tuple((str(row["persona_sha"]), int(row["changed_ms"])) for row in rows)

    def persona_report_pending(self, *, chat_id: str, persona_sha: str) -> bool:
        row = self._db().execute(
            "SELECT 1 FROM persona_reports WHERE chat_id=? AND persona_sha=?",
            (chat_id, persona_sha),
        ).fetchone()
        return row is None

    def mark_persona_reported(
        self, *, chat_id: str, persona_sha: str, now_ms: int
    ) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO persona_reports(chat_id, persona_sha, reported_ms) "
            "VALUES (?, ?, ?)",
            (chat_id, persona_sha, int(now_ms)),
        )


def _migrate(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None:
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"sentiment.db schema {version} is newer than {SCHEMA_VERSION}"
                )
            return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "ALERT_WINDOW_MS",
    "SCHEMA_VERSION",
    "LabelRow",
    "SentimentStore",
    "WeeklyRow",
    "now_ms",
]
