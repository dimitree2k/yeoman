"""Durable processing store: event journal, decisions, effect outbox and lineage.

One purpose-built SQLite database (``data/processing/processing.db``, spec R06) next to
the existing archives. It does not replace the inbound archive and it is not a second
semantic memory database.

Durability rules implemented here:

* every write happens in a short ``BEGIN IMMEDIATE`` transaction,
* no network call is ever made inside a transaction,
* the journal is append-only within its retention window; projections and outbox states
  may be updated transactionally,
* a successful commit precedes any external effect.

SQLite runs in WAL mode with ``synchronous=FULL`` for this journal. The existing
archives and the memory database keep their own settings.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from yeoman_gateway.processing.models import (
    CLAIMABLE_EFFECT_STATES,
    EFFECT_STATES,
    CanonicalEvent,
    DecisionRecord,
    EffectConflictError,
    EffectEnvelope,
    EffectTarget,
    InvalidTransitionError,
    JournalConflictError,
    LineageView,
    ProcessingError,
    PurgeReport,
    RelationMeta,
    RetainedAttemptMeta,
    RetainedEffectMeta,
    RetainedEventMeta,
    RetainedEvidenceMeta,
    RetentionSettings,
    StoredEffect,
    canonical_hash,
    canonical_json,
    payload_from_mapping,
    payload_to_mapping,
    validate_transition,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)

SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
      event_id TEXT PRIMARY KEY,
      event_key TEXT NOT NULL UNIQUE,
      trace_id TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'message',
      origin TEXT NOT NULL DEFAULT 'unknown',
      channel TEXT NOT NULL DEFAULT '',
      chat_id TEXT NOT NULL DEFAULT '',
      principal TEXT NOT NULL DEFAULT '',
      source_message_id TEXT,
      target_message_id TEXT,
      thread_id TEXT,
      turn_id TEXT,
      occurred_ms INTEGER,
      payload_hash TEXT NOT NULL,
      payload_json TEXT,
      payload_purged_ms INTEGER,
      created_ms INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_ms)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id)",
    """
    CREATE TABLE IF NOT EXISTS event_relations (
      event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
      relation TEXT NOT NULL,
      ref_id TEXT NOT NULL,
      resolved INTEGER NOT NULL DEFAULT 0,
      created_ms INTEGER NOT NULL,
      PRIMARY KEY (event_id, relation, ref_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relations_ref ON event_relations(ref_id, resolved)",
    "CREATE INDEX IF NOT EXISTS idx_relations_unresolved ON event_relations(resolved)",
    """
    CREATE TABLE IF NOT EXISTS decisions (
      decision_id TEXT PRIMARY KEY,
      trace_id TEXT NOT NULL,
      stage TEXT NOT NULL,
      policy_version TEXT NOT NULL,
      policy_hash TEXT NOT NULL,
      principal TEXT NOT NULL,
      target TEXT NOT NULL,
      capability TEXT NOT NULL,
      turn_revision INTEGER NOT NULL,
      outcome TEXT NOT NULL,
      reason TEXT NOT NULL,
      effect_id TEXT,
      created_ms INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_trace ON decisions(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_ms)",
    """
    CREATE TABLE IF NOT EXISTS effects (
      effect_id TEXT PRIMARY KEY,
      operation_key TEXT NOT NULL UNIQUE,
      trace_id TEXT NOT NULL DEFAULT '',
      turn_id TEXT NOT NULL DEFAULT '',
      turn_revision INTEGER NOT NULL DEFAULT 1,
      principal TEXT NOT NULL DEFAULT '',
      capability TEXT NOT NULL DEFAULT '',
      target_json TEXT NOT NULL DEFAULT '{}',
      target_hash TEXT NOT NULL DEFAULT '',
      payload_kind TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      payload_json TEXT,
      payload_purged_ms INTEGER,
      state TEXT NOT NULL,
      expires_at_ms INTEGER,
      policy_version TEXT,
      policy_hash TEXT,
      lease_owner TEXT,
      lease_until_ms INTEGER,
      created_ms INTEGER NOT NULL,
      updated_ms INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_effects_state ON effects(state, updated_ms)",
    "CREATE INDEX IF NOT EXISTS idx_effects_trace ON effects(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_effects_lease ON effects(state, lease_until_ms)",
    """
    CREATE TABLE IF NOT EXISTS effect_attempts (
      attempt_id TEXT PRIMARY KEY,
      effect_id TEXT NOT NULL REFERENCES effects(effect_id) ON DELETE CASCADE,
      worker_id TEXT,
      started_ms INTEGER NOT NULL,
      finished_ms INTEGER,
      outcome TEXT,
      policy_version TEXT,
      detail TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_effect ON effect_attempts(effect_id, started_ms)",
    """
    CREATE TABLE IF NOT EXISTS effect_evidence (
      evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
      effect_id TEXT NOT NULL REFERENCES effects(effect_id) ON DELETE CASCADE,
      kind TEXT NOT NULL,
      state TEXT,
      detail TEXT,
      observed_ms INTEGER NOT NULL,
      worker_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evidence_effect ON effect_evidence(effect_id, observed_ms)",
)


class ProcessingStore:
    """Purpose-built durable store for the processing pipeline."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention: RetentionSettings | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._path = str(path)
        self._retention = retention or RetentionSettings()
        self._lock = threading.RLock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=max(0.1, busy_timeout_ms / 1000),
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._create_schema()

    # -- lifecycle ---------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ProcessingStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    @property
    def retention(self) -> RetentionSettings:
        return self._retention

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row is not None else 0

    def quick_check(self) -> str:
        with self._lock:
            row = self._conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row is not None else "unknown"

    # -- schema ------------------------------------------------------------------------

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    self._conn.execute(statement)
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(row["value"]) > SCHEMA_VERSION:
                    raise ProcessingError(
                        f"processing store schema {row['value']} is newer than {SCHEMA_VERSION}"
                    )
                # Future versions append ordered migrations here; v1 only creates tables.
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Short exclusive write transaction. Never wrap a network call in this."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- journal -----------------------------------------------------------------------

    def append_event(
        self,
        *,
        event_key: str,
        event_id: str,
        trace_id: str,
        payload: CanonicalEvent | Mapping[str, Any],
        now_ms: int | None = None,
    ) -> str:
        """Append one canonical event; idempotent per provider identity.

        Returns the stable event id: a duplicate ``event_key`` never inserts a second
        event and never re-triggers downstream work. A conflicting payload hash raises
        :class:`JournalConflictError`. Once the raw payload was removed by retention, the
        identity alone decides (replay boundary, spec R06).
        """
        if not event_key:
            raise ValueError("event_key is required")
        created = self._now(now_ms)
        event = self._coerce_event(
            event_key=event_key, event_id=event_id, trace_id=trace_id, payload=payload,
            created_ms=created,
        )
        with self._write() as conn:
            row = conn.execute(
                "SELECT event_id, payload_hash, payload_json FROM events WHERE event_key = ?",
                (event.event_key,),
            ).fetchone()
            if row is not None:
                if row["payload_json"] is None or row["payload_hash"] == event.payload_hash:
                    return str(row["event_id"])
                raise JournalConflictError(
                    f"event_key {event.event_key!r} already exists with a different payload"
                )
            clash = conn.execute(
                "SELECT event_key FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if clash is not None:
                raise JournalConflictError(
                    f"event_id {event.event_id!r} already belongs to {clash['event_key']!r}"
                )
            conn.execute(
                """
                INSERT INTO events (
                  event_id, event_key, trace_id, kind, origin, channel, chat_id, principal,
                  source_message_id, target_message_id, thread_id, turn_id, occurred_ms,
                  payload_hash, payload_json, payload_purged_ms, created_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                """,
                (
                    event.event_id,
                    event.event_key,
                    event.trace_id,
                    event.kind,
                    event.origin,
                    event.channel,
                    event.chat_id,
                    event.principal,
                    event.source_message_id,
                    event.target_message_id,
                    event.thread_id,
                    event.turn_id,
                    event.occurred_ms,
                    event.payload_hash,
                    canonical_json(dict(event.payload or {})),
                    event.created_ms,
                ),
            )
            self._record_relations(conn, event)
        return event.event_id

    def _coerce_event(
        self,
        *,
        event_key: str,
        event_id: str,
        trace_id: str,
        payload: CanonicalEvent | Mapping[str, Any],
        created_ms: int,
    ) -> CanonicalEvent:
        if isinstance(payload, CanonicalEvent):
            body: dict[str, Any] = dict(payload.payload or {})
            return CanonicalEvent(
                event_id=event_id,
                event_key=event_key,
                trace_id=trace_id,
                kind=payload.kind,
                origin=payload.origin,
                principal=payload.principal,
                channel=payload.channel,
                chat_id=payload.chat_id,
                occurred_ms=payload.occurred_ms,
                created_ms=created_ms,
                source_message_id=payload.source_message_id,
                target_message_id=payload.target_message_id,
                thread_id=payload.thread_id,
                turn_id=payload.turn_id,
                payload=body,
                payload_hash=canonical_hash(body),
            )
        if not isinstance(payload, Mapping):
            raise TypeError("event payload must be a CanonicalEvent or a mapping")
        body = dict(payload)
        return CanonicalEvent(
            event_id=event_id,
            event_key=event_key,
            trace_id=trace_id,
            kind=str(body.get("kind") or "message"),
            origin=str(body.get("origin") or "unknown"),
            principal=str(body.get("principal") or ""),
            channel=str(body.get("channel") or ""),
            chat_id=str(body.get("chat_id") or ""),
            occurred_ms=_opt_int(body.get("occurred_ms") or body.get("timestamp_ms")),
            created_ms=created_ms,
            source_message_id=_opt_str(
                body.get("source_message_id") or body.get("message_id")
            ),
            target_message_id=_opt_str(body.get("target_message_id")),
            thread_id=_opt_str(body.get("thread_id")),
            turn_id=_opt_str(body.get("turn_id")),
            payload=body,
            payload_hash=canonical_hash(body),
        )

    def _record_relations(self, conn: sqlite3.Connection, event: CanonicalEvent) -> None:
        for relation, ref_id in event.relations():
            resolved = 1 if self._ref_known(conn, ref_id) else 0
            conn.execute(
                """
                INSERT OR IGNORE INTO event_relations
                  (event_id, relation, ref_id, resolved, created_ms)
                VALUES (?,?,?,?,?)
                """,
                (event.event_id, relation, ref_id, resolved, event.created_ms or 0),
            )
        resolved_ids = [
            value
            for value in (event.event_id, event.source_message_id, event.target_message_id)
            if value
        ]
        if resolved_ids:
            placeholders = ",".join("?" for _ in resolved_ids)
            conn.execute(
                f"UPDATE event_relations SET resolved = 1 "
                f"WHERE resolved = 0 AND ref_id IN ({placeholders})",
                resolved_ids,
            )

    def _ref_known(self, conn: sqlite3.Connection, ref_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM events WHERE event_id = ? OR source_message_id = ? LIMIT 1",
            (ref_id, ref_id),
        ).fetchone()
        return row is not None

    def get_event(self, event_id: str) -> CanonicalEvent | None:
        """Raw event access for processing stages; lineage views never expose this."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def event_by_key(self, event_key: str) -> CanonicalEvent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_key = ?", (event_key,)
            ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def unresolved_relations(self) -> tuple[RelationMeta, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_relations WHERE resolved = 0 ORDER BY created_ms, event_id"
            ).fetchall()
        return tuple(_relation_from_row(row) for row in rows)

    def count_events(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def list_effects(
        self, *, states: Iterable[str] | None = None, limit: int = 100
    ) -> tuple[RetainedEffectMeta, ...]:
        """Lineage projections of effects, optionally filtered by state.

        The reconciler (Plan 04) and diagnostics use this; it never exposes payload text.
        """
        query = "SELECT * FROM effects"
        params: list[Any] = []
        if states is not None:
            wanted = tuple(states)
            if not wanted:
                return ()
            placeholders = ",".join("?" for _ in wanted)
            query += f" WHERE state IN ({placeholders})"
            params.extend(wanted)
        query += " ORDER BY created_ms, effect_id LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            effects = tuple(
                _effect_meta_from_row(
                    row,
                    attempts=self._attempts_for(row["effect_id"]),
                    evidence=self._evidence_for(row["effect_id"]),
                )
                for row in rows
            )
        return effects

    def count_effects(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM effects").fetchone()
        return int(row["n"])

    # -- decisions ---------------------------------------------------------------------

    def record_decision(self, record: DecisionRecord) -> str:
        """Persist one immutable decision; identical re-records are idempotent."""
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (record.decision_id,)
            ).fetchone()
            if row is not None:
                if _decision_signature_from_row(row) != _decision_signature(record):
                    raise ValueError(
                        f"decision {record.decision_id!r} already exists with different content"
                    )
                return record.decision_id
            conn.execute(
                """
                INSERT INTO decisions (
                  decision_id, trace_id, stage, policy_version, policy_hash, principal,
                  target, capability, turn_revision, outcome, reason, effect_id, created_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.decision_id,
                    record.trace_id,
                    record.stage,
                    record.policy_version,
                    record.policy_hash,
                    record.principal,
                    record.target,
                    record.capability,
                    record.turn_revision,
                    record.outcome,
                    record.reason,
                    record.effect_id,
                    record.created_ms,
                ),
            )
        return record.decision_id

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return _decision_from_row(row) if row is not None else None

    # -- effect outbox -----------------------------------------------------------------

    def enqueue_effect(
        self,
        *,
        effect_id: str,
        operation_key: str,
        payload: Any,
        now_ms: int,
        trace_id: str = "",
        turn_id: str = "",
        turn_revision: int = 1,
        principal: str = "",
        capability: str = "",
        target: EffectTarget | Mapping[str, Any] | None = None,
        expires_at_ms: int | None = None,
        policy_version: str | None = None,
        policy_hash: str | None = None,
        state: str = "queued",
    ) -> str:
        """Durably accept one effect. Returns the original id for an identical retry.

        Same ``operation_key`` with a different payload, target or turn identity is a
        conflict (:class:`EffectConflictError`) - it is never treated as a retry. After the
        payload was removed by retention only the operation key decides, so an old
        action cannot silently re-fire.
        """
        if not effect_id:
            raise ValueError("effect_id is required")
        if not operation_key:
            raise ValueError("operation_key is required")
        if state not in EFFECT_STATES:
            raise ValueError(f"unknown effect state: {state}")
        if turn_revision < 1:
            raise ValueError("turn revision must be a positive integer")
        created = self._now(now_ms)
        body = payload_to_mapping(payload)
        payload_hash = canonical_hash(body)
        if target is None:
            target_json = "{}"
            target_hash = canonical_hash({})
        else:
            resolved_target = (
                target if isinstance(target, EffectTarget) else EffectTarget.from_mapping(target)
            )
            target_json = canonical_json(resolved_target.to_dict())
            target_hash = resolved_target.target_hash
        effective_state = state
        if state == "queued" and expires_at_ms is not None and expires_at_ms <= created:
            effective_state = "expired"

        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM effects WHERE operation_key = ?", (operation_key,)
            ).fetchone()
            if row is not None:
                if row["payload_json"] is None:
                    return str(row["effect_id"])
                if (
                    row["payload_hash"] == payload_hash
                    and row["target_hash"] == target_hash
                    and str(row["turn_id"]) == turn_id
                    and int(row["turn_revision"]) == turn_revision
                    and str(row["principal"]) == principal
                    and str(row["capability"]) == capability
                ):
                    return str(row["effect_id"])
                raise EffectConflictError(
                    f"operation_key {operation_key!r} already exists with a different effect"
                )
            clash = conn.execute(
                "SELECT operation_key FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if clash is not None:
                raise EffectConflictError(
                    f"effect_id {effect_id!r} already belongs to {clash['operation_key']!r}"
                )
            conn.execute(
                """
                INSERT INTO effects (
                  effect_id, operation_key, trace_id, turn_id, turn_revision, principal,
                  capability, target_json, target_hash, payload_kind, payload_hash,
                  payload_json, payload_purged_ms, state, expires_at_ms, policy_version,
                  policy_hash, lease_owner, lease_until_ms, created_ms, updated_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,NULL,NULL,?,?)
                """,
                (
                    effect_id,
                    operation_key,
                    trace_id,
                    turn_id,
                    turn_revision,
                    principal,
                    capability,
                    target_json,
                    target_hash,
                    str(body.get("kind") or "text"),
                    payload_hash,
                    canonical_json(body),
                    effective_state,
                    expires_at_ms,
                    policy_version,
                    policy_hash,
                    created,
                    created,
                ),
            )
            self._append_evidence(
                conn,
                effect_id=effect_id,
                kind="submitted",
                state=effective_state,
                detail=None,
                observed_ms=created,
                worker_id=None,
            )
        return effect_id

    def get_effect(self, effect_id: str) -> StoredEffect | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
        return _effect_from_row(row) if row is not None else None

    def effect_by_operation_key(self, operation_key: str) -> StoredEffect | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        return _effect_from_row(row) if row is not None else None

    def effect_state(self, effect_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
        return str(row["state"]) if row is not None else None

    def open_attempt_id(self, effect_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_id FROM effect_attempts "
                "WHERE effect_id = ? AND finished_ms IS NULL ORDER BY started_ms DESC LIMIT 1",
                (effect_id,),
            ).fetchone()
        return str(row["attempt_id"]) if row is not None else None

    def claim_effect(
        self,
        effect_id: str,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        *,
        policy_version: str | None = None,
    ) -> bool:
        """Atomically claim an executable effect. Only one worker can win.

        ``policy_version`` records which policy version the attempt was authorized
        under; a policy change after dispatch starts cannot undo an effect in flight.
        """
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_ms <= 0:
            raise ValueError("lease_ms must be positive")
        with self._write() as conn:
            row = conn.execute(
                "SELECT state, lease_until_ms, lease_owner, policy_version "
                "FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None or str(row["state"]) not in CLAIMABLE_EFFECT_STATES:
                return False
            cursor = conn.execute(
                """
                UPDATE effects
                   SET state = 'executing', lease_owner = ?, lease_until_ms = ?, updated_ms = ?
                 WHERE effect_id = ? AND state = 'queued'
                """,
                (worker_id, now_ms + lease_ms, now_ms, effect_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                INSERT INTO effect_attempts
                  (attempt_id, effect_id, worker_id, started_ms, finished_ms, outcome,
                   policy_version, detail)
                VALUES (?,?,?,?,NULL,NULL,?,NULL)
                """,
                (
                    uuid.uuid4().hex,
                    effect_id,
                    worker_id,
                    now_ms,
                    policy_version or row["policy_version"],
                ),
            )
            self._append_evidence(
                conn,
                effect_id=effect_id,
                kind="claim",
                state="executing",
                detail=None,
                observed_ms=now_ms,
                worker_id=worker_id,
            )
        return True

    def release_claim(self, effect_id: str, worker_id: str, now_ms: int) -> bool:
        """Drop a lease without inventing an outcome; the effect stays ``executing``."""
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE effects SET lease_owner = NULL, lease_until_ms = NULL, updated_ms = ?
                 WHERE effect_id = ? AND state = 'executing' AND lease_owner = ?
                """,
                (now_ms, effect_id, worker_id),
            )
        return cursor.rowcount == 1

    def transition(
        self,
        effect_id: str,
        *,
        expected: str | Iterable[str],
        target: str,
        now_ms: int,
        evidence: Any = None,
        worker_id: str | None = None,
    ) -> bool:
        """Compare-and-set state change. Returns False when the expectation missed.

        Structurally illegal changes raise :class:`InvalidTransitionError` instead of
        silently doing nothing, so a wrong state machine shows up in tests.
        """
        states = (expected,) if isinstance(expected, str) else tuple(expected)
        if not states:
            raise ValueError("expected must name at least one state")
        evidence_record = _coerce_evidence(evidence, now_ms=now_ms, worker_id=worker_id)
        with self._write() as conn:
            row = conn.execute(
                "SELECT state, lease_owner FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                return False
            current = str(row["state"])
            if current not in states:
                return False
            validate_transition(
                current,
                target,
                evidence_kinds=(evidence_record.kind,) if evidence_record else (),
                worker_id=worker_id,
                lease_owner=(
                    str(row["lease_owner"]) if row["lease_owner"] is not None else None
                ),
            )
            clears_lease = current == "executing"
            cursor = conn.execute(
                """
                UPDATE effects
                   SET state = ?, updated_ms = ?,
                       lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                       lease_until_ms = CASE WHEN ? THEN NULL ELSE lease_until_ms END
                 WHERE effect_id = ? AND state = ?
                """,
                (target, now_ms, clears_lease, clears_lease, effect_id, current),
            )
            if cursor.rowcount != 1:
                return False
            if clears_lease:
                conn.execute(
                    """
                    UPDATE effect_attempts SET finished_ms = ?, outcome = ?
                     WHERE effect_id = ? AND finished_ms IS NULL
                    """,
                    (now_ms, target, effect_id),
                )
            self._append_evidence(
                conn,
                effect_id=effect_id,
                kind=evidence_record.kind if evidence_record else "state",
                state=target,
                detail=evidence_record.detail if evidence_record else None,
                observed_ms=now_ms,
                worker_id=worker_id,
            )
        return True

    def record_evidence(
        self, effect_id: str, *, kind: str, now_ms: int, detail: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Attach transport evidence (delivered/read/...) without changing state.

        Late evidence may strengthen what is known; it never downgrades a monotonic
        state (spec R10).
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT state FROM effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise ProcessingError(f"unknown effect: {effect_id}")
            self._append_evidence(
                conn,
                effect_id=effect_id,
                kind=kind,
                state=str(row["state"]),
                detail=detail,
                observed_ms=now_ms,
                worker_id=worker_id,
            )

    def recover_executing(self, now_ms: int) -> tuple[str, ...]:
        """Turn expired ``executing`` claims into ``unknown`` - never into ``queued``.

        A process that died after a possible dispatch cannot prove the outcome, so the
        effect becomes ``unknown`` and needs reconciliation (spec R07).
        """
        with self._write() as conn:
            rows = conn.execute(
                """
                SELECT effect_id FROM effects
                 WHERE state = 'executing'
                   AND (lease_until_ms IS NULL OR lease_until_ms <= ?)
                """,
                (now_ms,),
            ).fetchall()
            effect_ids = tuple(str(row["effect_id"]) for row in rows)
            for effect_id in effect_ids:
                conn.execute(
                    """
                    UPDATE effects
                       SET state = 'unknown', lease_owner = NULL, lease_until_ms = NULL,
                           updated_ms = ?
                     WHERE effect_id = ? AND state = 'executing'
                    """,
                    (now_ms, effect_id),
                )
                conn.execute(
                    """
                    UPDATE effect_attempts SET finished_ms = ?, outcome = 'unknown'
                     WHERE effect_id = ? AND finished_ms IS NULL
                    """,
                    (now_ms, effect_id),
                )
                self._append_evidence(
                    conn,
                    effect_id=effect_id,
                    kind="recovery",
                    state="unknown",
                    detail="executing claim expired; outcome unproven",
                    observed_ms=now_ms,
                    worker_id=None,
                )
        return effect_ids

    def _append_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        effect_id: str,
        kind: str,
        state: str | None,
        detail: str | None,
        observed_ms: int,
        worker_id: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO effect_evidence
              (effect_id, kind, state, detail, observed_ms, worker_id)
            VALUES (?,?,?,?,?,?)
            """,
            (effect_id, kind, state, detail, observed_ms, worker_id),
        )

    # -- lineage -----------------------------------------------------------------------

    def get_lineage(self, trace_id: str) -> LineageView:
        """Administrative metadata graph for one trace: no payload text, no secrets."""
        with self._lock:
            event_rows = self._conn.execute(
                "SELECT * FROM events WHERE trace_id = ? ORDER BY created_ms, event_id",
                (trace_id,),
            ).fetchall()
            relation_rows = self._conn.execute(
                """
                SELECT r.* FROM event_relations r
                  JOIN events e ON e.event_id = r.event_id
                 WHERE e.trace_id = ?
                 ORDER BY r.created_ms, r.event_id
                """,
                (trace_id,),
            ).fetchall()
            decision_rows = self._conn.execute(
                "SELECT * FROM decisions WHERE trace_id = ? ORDER BY created_ms, decision_id",
                (trace_id,),
            ).fetchall()
            effect_rows = self._conn.execute(
                "SELECT * FROM effects WHERE trace_id = ? ORDER BY created_ms, effect_id",
                (trace_id,),
            ).fetchall()
            relations_by_event: dict[str, list[RelationMeta]] = {}
            for row in relation_rows:
                relations_by_event.setdefault(str(row["event_id"]), []).append(
                    _relation_from_row(row)
                )
            events = tuple(
                _event_meta_from_row(row, tuple(relations_by_event.get(str(row["event_id"]), ())))
                for row in event_rows
            )
            effects = tuple(
                _effect_meta_from_row(
                    row,
                    attempts=self._attempts_for(row["effect_id"]),
                    evidence=self._evidence_for(row["effect_id"]),
                )
                for row in effect_rows
            )
        return LineageView(
            trace_id=trace_id,
            events=events,
            decisions=tuple(_decision_from_row(row) for row in decision_rows),
            effects=effects,
            unresolved=tuple(
                _relation_from_row(row) for row in relation_rows if int(row["resolved"]) == 0
            ),
        )

    def _attempts_for(self, effect_id: str) -> tuple[RetainedAttemptMeta, ...]:
        rows = self._conn.execute(
            "SELECT * FROM effect_attempts WHERE effect_id = ? ORDER BY started_ms",
            (effect_id,),
        ).fetchall()
        return tuple(
            RetainedAttemptMeta(
                attempt_id=str(row["attempt_id"]),
                effect_id=str(row["effect_id"]),
                started_ms=int(row["started_ms"]),
                finished_ms=int(row["finished_ms"]) if row["finished_ms"] is not None else None,
                outcome=str(row["outcome"]) if row["outcome"] is not None else None,
                policy_version=(
                    str(row["policy_version"]) if row["policy_version"] is not None else None
                ),
                detail=str(row["detail"]) if row["detail"] is not None else None,
            )
            for row in rows
        )

    def _evidence_for(self, effect_id: str) -> tuple[RetainedEvidenceMeta, ...]:
        rows = self._conn.execute(
            "SELECT * FROM effect_evidence WHERE effect_id = ? ORDER BY observed_ms, evidence_id",
            (effect_id,),
        ).fetchall()
        return tuple(
            RetainedEvidenceMeta(
                effect_id=str(row["effect_id"]),
                kind=str(row["kind"]),
                state=str(row["state"]) if row["state"] is not None else None,
                detail=str(row["detail"]) if row["detail"] is not None else None,
                observed_ms=int(row["observed_ms"]),
                worker_id=str(row["worker_id"]) if row["worker_id"] is not None else None,
            )
            for row in rows
        )

    # -- retention ---------------------------------------------------------------------

    def purge(self, *, now_ms: int) -> PurgeReport:
        """Apply retention. Payloads are stripped first, metadata later.

        Events with unresolved relations survive until the unresolved grace period.
        Effects keep a minimal tombstone (ids, hashes, state) and are never deleted, so
        an operation key cannot silently fire a second time after retention.
        """
        retention = self._retention
        payload_cutoff = now_ms - retention.journal_payload_ms
        metadata_cutoff = now_ms - retention.metadata_ms
        unresolved_cutoff = now_ms - retention.unresolved_ms
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE events SET payload_json = NULL, payload_purged_ms = ?
                 WHERE payload_json IS NOT NULL AND created_ms <= ?
                """,
                (now_ms, payload_cutoff),
            )
            event_payloads_purged = int(cursor.rowcount or 0)

            cursor = conn.execute(
                """
                UPDATE effects SET payload_json = NULL, payload_purged_ms = ?
                 WHERE payload_json IS NOT NULL AND updated_ms <= ?
                """,
                (now_ms, payload_cutoff),
            )
            effect_payloads_purged = int(cursor.rowcount or 0)

            relations_before = int(
                conn.execute("SELECT COUNT(*) AS n FROM event_relations").fetchone()["n"]
            )
            cursor = conn.execute(
                """
                DELETE FROM events
                 WHERE created_ms <= :metadata
                   AND NOT (
                     created_ms > :unresolved
                     AND event_id IN (
                       SELECT event_id FROM event_relations WHERE resolved = 0
                     )
                   )
                """,
                {"metadata": metadata_cutoff, "unresolved": unresolved_cutoff},
            )
            events_deleted = int(cursor.rowcount or 0)
            relations_after = int(
                conn.execute("SELECT COUNT(*) AS n FROM event_relations").fetchone()["n"]
            )

            cursor = conn.execute(
                "DELETE FROM decisions WHERE created_ms <= ?", (metadata_cutoff,)
            )
            decisions_deleted = int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM effect_attempts WHERE started_ms <= ?", (metadata_cutoff,)
            )
            attempts_deleted = int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM effect_evidence WHERE observed_ms <= ?", (metadata_cutoff,)
            )
            evidence_deleted = int(cursor.rowcount or 0)
        return PurgeReport(
            event_payloads_purged=event_payloads_purged,
            effect_payloads_purged=effect_payloads_purged,
            events_deleted=events_deleted,
            relations_deleted=max(0, relations_before - relations_after),
            decisions_deleted=decisions_deleted,
            attempts_deleted=attempts_deleted,
            evidence_deleted=evidence_deleted,
        )

    # -- helpers -----------------------------------------------------------------------

    def _now(self, value: int | None) -> int:
        return int(value) if value is not None else _now_ms()

    def _event_from_row(self, row: sqlite3.Row) -> CanonicalEvent:
        payload = None
        if row["payload_json"] is not None:
            import json

            decoded = json.loads(row["payload_json"])
            payload = decoded if isinstance(decoded, Mapping) else {"value": decoded}
        return CanonicalEvent(
            event_id=str(row["event_id"]),
            event_key=str(row["event_key"]),
            trace_id=str(row["trace_id"]),
            kind=str(row["kind"]),
            origin=str(row["origin"]),
            principal=str(row["principal"]),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            occurred_ms=int(row["occurred_ms"]) if row["occurred_ms"] is not None else None,
            created_ms=int(row["created_ms"]),
            source_message_id=(
                str(row["source_message_id"]) if row["source_message_id"] is not None else None
            ),
            target_message_id=(
                str(row["target_message_id"]) if row["target_message_id"] is not None else None
            ),
            thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            payload=payload,
            payload_hash=str(row["payload_hash"]),
            payload_purged_ms=(
                int(row["payload_purged_ms"]) if row["payload_purged_ms"] is not None else None
            ),
        )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relation_from_row(row: sqlite3.Row) -> RelationMeta:
    return RelationMeta(
        event_id=str(row["event_id"]),
        relation=str(row["relation"]),
        ref_id=str(row["ref_id"]),
        resolved=int(row["resolved"]),
        created_ms=int(row["created_ms"]),
    )


def _event_meta_from_row(
    row: sqlite3.Row, relations: tuple[RelationMeta, ...]
) -> RetainedEventMeta:
    return RetainedEventMeta(
        event_id=str(row["event_id"]),
        event_key=str(row["event_key"]),
        trace_id=str(row["trace_id"]),
        kind=str(row["kind"]),
        origin=str(row["origin"]),
        principal=str(row["principal"]),
        channel=str(row["channel"]),
        chat_id=str(row["chat_id"]),
        occurred_ms=int(row["occurred_ms"]) if row["occurred_ms"] is not None else None,
        created_ms=int(row["created_ms"]) if row["created_ms"] is not None else None,
        payload_hash=str(row["payload_hash"]),
        payload_available=row["payload_json"] is not None,
        payload_purged_ms=(
            int(row["payload_purged_ms"]) if row["payload_purged_ms"] is not None else None
        ),
        source_message_id=(
            str(row["source_message_id"]) if row["source_message_id"] is not None else None
        ),
        target_message_id=(
            str(row["target_message_id"]) if row["target_message_id"] is not None else None
        ),
        thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
        turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
        relations=relations,
    )


def _effect_from_row(row: sqlite3.Row) -> StoredEffect:
    import json

    payload = None
    if row["payload_json"] is not None:
        payload = payload_from_mapping(json.loads(row["payload_json"]))
    target = None
    if row["target_json"]:
        decoded = json.loads(row["target_json"])
        if decoded:
            target = EffectTarget.from_mapping(decoded)
    return StoredEffect(
        effect_id=str(row["effect_id"]),
        operation_key=str(row["operation_key"]),
        trace_id=str(row["trace_id"]),
        turn_id=str(row["turn_id"]),
        turn_revision=int(row["turn_revision"]),
        principal=str(row["principal"]),
        capability=str(row["capability"]),
        payload=payload,
        target=target,
        payload_kind=str(row["payload_kind"]),
        payload_hash=str(row["payload_hash"]),
        target_hash=str(row["target_hash"]),
        payload_purged_ms=(
            int(row["payload_purged_ms"]) if row["payload_purged_ms"] is not None else None
        ),
        state=str(row["state"]),
        expires_at_ms=(
            int(row["expires_at_ms"]) if row["expires_at_ms"] is not None else None
        ),
        policy_version=(
            str(row["policy_version"]) if row["policy_version"] is not None else None
        ),
        policy_hash=str(row["policy_hash"]) if row["policy_hash"] is not None else None,
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_until_ms=(
            int(row["lease_until_ms"]) if row["lease_until_ms"] is not None else None
        ),
        created_ms=int(row["created_ms"]),
        updated_ms=int(row["updated_ms"]),
    )


def _effect_meta_from_row(
    row: sqlite3.Row,
    *,
    attempts: tuple[RetainedAttemptMeta, ...],
    evidence: tuple[RetainedEvidenceMeta, ...],
) -> RetainedEffectMeta:
    return RetainedEffectMeta(
        effect_id=str(row["effect_id"]),
        operation_key=str(row["operation_key"]),
        trace_id=str(row["trace_id"]),
        turn_id=str(row["turn_id"]),
        turn_revision=int(row["turn_revision"]),
        principal=str(row["principal"]),
        capability=str(row["capability"]),
        target_hash=str(row["target_hash"]),
        payload_kind=str(row["payload_kind"]),
        payload_hash=str(row["payload_hash"]),
        payload_available=row["payload_json"] is not None,
        payload_purged_ms=(
            int(row["payload_purged_ms"]) if row["payload_purged_ms"] is not None else None
        ),
        state=str(row["state"]),
        expires_at_ms=(
            int(row["expires_at_ms"]) if row["expires_at_ms"] is not None else None
        ),
        policy_version=(
            str(row["policy_version"]) if row["policy_version"] is not None else None
        ),
        policy_hash=str(row["policy_hash"]) if row["policy_hash"] is not None else None,
        created_ms=int(row["created_ms"]),
        updated_ms=int(row["updated_ms"]),
        attempts=attempts,
        evidence=evidence,
    )


def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=str(row["decision_id"]),
        trace_id=str(row["trace_id"]),
        policy_version=str(row["policy_version"]),
        policy_hash=str(row["policy_hash"]),
        principal=str(row["principal"]),
        target=str(row["target"]),
        capability=str(row["capability"]),
        turn_revision=int(row["turn_revision"]),
        outcome=str(row["outcome"]),
        reason=str(row["reason"]),
        created_ms=int(row["created_ms"]),
        stage=str(row["stage"]),
        effect_id=str(row["effect_id"]) if row["effect_id"] is not None else None,
    )


def _decision_signature(record: DecisionRecord) -> str:
    return canonical_hash(
        {
            "trace_id": record.trace_id,
            "stage": record.stage,
            "policy_version": record.policy_version,
            "policy_hash": record.policy_hash,
            "principal": record.principal,
            "target": record.target,
            "capability": record.capability,
            "turn_revision": record.turn_revision,
            "outcome": record.outcome,
            "reason": record.reason,
            "effect_id": record.effect_id,
            "created_ms": record.created_ms,
        }
    )


def _decision_signature_from_row(row: sqlite3.Row) -> str:
    return _decision_signature(_decision_from_row(row))


def _coerce_evidence(evidence: Any, *, now_ms: int, worker_id: str | None):
    from yeoman_gateway.processing.models import EffectEvidence

    if evidence is None:
        return None
    if isinstance(evidence, EffectEvidence):
        return evidence
    if isinstance(evidence, str):
        return EffectEvidence(kind=evidence, observed_ms=now_ms, worker_id=worker_id)
    if isinstance(evidence, Mapping):
        return EffectEvidence(
            kind=str(evidence.get("kind") or "state"),
            detail=_opt_str(evidence.get("detail")),
            observed_ms=_opt_int(evidence.get("observed_ms")) or now_ms,
            worker_id=_opt_str(evidence.get("worker_id")) or worker_id,
        )
    raise TypeError("evidence must be an EffectEvidence, a mapping or a kind string")


__all__ = [
    "SCHEMA_VERSION",
    "ProcessingStore",
    "EffectConflictError",
    "InvalidTransitionError",
    "JournalConflictError",
    "ProcessingError",
    "EffectEnvelope",
    "RetentionSettings",
]
