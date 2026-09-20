"""SQLite log for proactive speakup proposals and commits.

Besides the historical ``speakups`` rows, this store owns the *participation
ledger*: durable delivery reservations, recipient-evidence delivery state and
judge attempts (spec section 9). The ledger is deliberately one SQLite database
with one writer lock: capacity reservations are checked and inserted in a single
``BEGIN IMMEDIATE`` transaction, so two concurrent callers can never both win the
last slot. No cross-database atomicity is assumed - the processing effect store
stays the owner of transport truth, and the two are joined by the stable effect id.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import ensure_dir

#: Proposal lifecycle states owned by this ledger. They do not replace the
#: processing effect-store enums; they describe the participation proposal.
PROPOSAL_STATES: tuple[str, ...] = (
    "proposed",
    "decided_silence",
    "decision_failed",
    "approved_to_generate",
    "generated",
    "awaiting_approval",
    "queued_for_approval",
    "approved",
    "denied",
    "expired",
    "cancelled",
    "rejected",
    "submitted",
    "transport_accepted",
    "delivered",
    "delivery_unknown",
    "failed",
)

#: A budget hold is a lease, separate from permanent delivery evidence.
DELIVERY_RESERVATION_TTL_MS = 10 * 60_000

#: Reservation categories with their identity in the ledger.
RESERVATION_CATEGORIES: tuple[str, ...] = ("initiation", "comment", "reaction")

#: Delivery evidence that a release/failure claim must not overwrite.
CONSUMED_DELIVERY_STATES: frozenset[str] = frozenset(
    {"transport_accepted", "delivered", "delivery_unknown"}
)

#: Delivery states that released the reservation.
RELEASED_DELIVERY_STATES: frozenset[str] = frozenset({"failed", "cancelled", "expired"})

#: Evidence kinds that prove a *transport* accepted the effect. They are not
#: proof that a recipient received it.
TRANSPORT_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"transport_receipt", "probe_confirmed", "effect_sent"}
)

#: Sanitized parser/runtime details permitted in durable judge-attempt rows.
JUDGE_DETAIL_CODES: frozenset[str] = frozenset(
    {
        "not_json_object",
        "context_budget_exceeded",
        "unknown_action",
        "action_not_allowed",
        "intent_not_allowed",
        "direct_not_admitted",
        "continuation_not_allowed",
        "evidence_not_list",
        "too_many_evidence",
        "evidence_not_str",
        "unknown_evidence_id",
        "anchor_not_supplied",
        "target_not_supplied",
        "target_not_current",
        "continuation_without_anchor",
        "continuation_without_delivered_anchor",
        "continuation_not_candidate",
        "continuation_anchor_closed",
        "contribution_type_not_allowed",
        "purpose_required",
        "unknown_emoji",
    }
)

#: Evidence kinds that prove a recipient received the exact target message.
RECIPIENT_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"recipient_delivery", "recipient_read", "quote_proof", "reaction_proof"}
)

#: Evidence kinds scoped to at least one recipient instead of every member.
GROUP_SCOPED_EVIDENCE_KINDS: frozenset[str] = frozenset({"group_delivery_at_least_one"})


# One initiating proposal is one send, even with two capacity dimensions or
# overlapping legacy history. Transport acceptance is sufficient for budgeting.
_CONFIRMED_SPEAKUPS_SQL = """
    SELECT proposal_id, MAX(sent_at) AS sent_at FROM (
        SELECT id AS proposal_id, committed_at AS sent_at FROM speakups
        WHERE channel = :channel AND chat_id = :chat_id AND status = 'sent'
          AND committed_at IS NOT NULL
        UNION ALL
        SELECT proposal_id, accepted_at_ms / 1000.0 AS sent_at
        FROM delivery_reservations
        WHERE channel = :channel AND chat_id = :chat_id AND category = 'initiation'
          AND delivery_state IN ('transport_accepted', 'delivered')
          AND accepted_at_ms IS NOT NULL
    ) GROUP BY proposal_id
"""


def deterministic_effect_id(
    *,
    channel: str,
    chat_id: str,
    operation: str,
    proposal_id: str,
    revision: int = 1,
) -> str:
    """Stable effect identity derived from proposal, revision and target operation.

    A retry after a crash recomputes the same id, so the processing store's
    ``operation_key`` uniqueness turns a second submit into the same effect
    instead of a second send (spec section 9).
    """
    material = "\x1f".join(
        [
            "speakup",
            str(channel),
            str(chat_id),
            str(operation),
            str(proposal_id),
            str(int(revision)),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"spk-{digest[:32]}"


def _held_or_accepted_count(
    *,
    conn: sqlite3.Connection,
    channel: str,
    chat_id: str,
    category: str,
    exclude_proposal_id: str,
    now_ms: int,
    accepted_since_ms: int | None = None,
    accepted_before_ms: int | None = None,
) -> int:
    """Confirmed sends in the accounting window plus unexpired budget leases.

    Unknown delivery evidence survives lease expiry, but cannot hold capacity
    indefinitely. Only real acceptance/delivery counts as a confirmed send.
    """
    sql = [
        "SELECT COUNT(*) AS c FROM delivery_reservations",
        "WHERE channel = ? AND chat_id = ? AND category = ? AND proposal_id <> ?",
    ]
    params: list[Any] = [channel, chat_id, category, exclude_proposal_id]
    if accepted_since_ms is not None:
        sql.append(
            "AND ((delivery_state IN ('transport_accepted', 'delivered')"
            " AND accepted_at_ms IS NOT NULL AND accepted_at_ms >= ?"
            " AND (? IS NULL OR accepted_at_ms < ?))"
            " OR (delivery_state IN ('reserved', 'submitted', 'delivery_unknown')"
            " AND created_at_ms > ?))"
        )
        params.append(int(accepted_since_ms))
        params.append(None if accepted_before_ms is None else int(accepted_before_ms))
        params.append(None if accepted_before_ms is None else int(accepted_before_ms))
        params.append(int(now_ms) - DELIVERY_RESERVATION_TTL_MS)
    row = conn.execute(" ".join(sql), tuple(params)).fetchone()
    return int(row["c"] if row else 0)


def _rolling_capacity(
    *,
    conn: sqlite3.Connection,
    channel: str,
    chat_id: str,
    category: str,
    limit: int,
    now_ms: int,
    window_ms: int,
    exclude_proposal_id: str,
) -> tuple[bool, str]:
    """Capacity for one rolling-window dimension. Caller holds the write transaction."""
    window_start = int(now_ms) - max(1, int(window_ms))
    used = _held_or_accepted_count(
        conn=conn,
        channel=channel,
        chat_id=chat_id,
        category=category,
        exclude_proposal_id=exclude_proposal_id,
        now_ms=now_ms,
        accepted_since_ms=window_start + 1,
    )
    if used >= int(limit):
        return False, f"{category}_limit_reached"
    return True, ""


def _calendar_day_capacity(
    *,
    conn: sqlite3.Connection,
    channel: str,
    chat_id: str,
    category: str,
    limit: int,
    now_ms: int,
    exclude_proposal_id: str,
) -> tuple[bool, str]:
    """Capacity for the current UTC calendar day, including unexpired leases."""
    current = datetime.fromtimestamp(int(now_ms) / 1000, UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int((day_start + timedelta(days=1)).timestamp() * 1000)
    used = _held_or_accepted_count(
        conn=conn,
        channel=channel,
        chat_id=chat_id,
        category=category,
        exclude_proposal_id=exclude_proposal_id,
        now_ms=now_ms,
        accepted_since_ms=start_ms,
        accepted_before_ms=end_ms,
    )
    if used >= int(limit):
        return False, f"{category}_limit_reached"
    return True, ""


class SpeakupLog:
    """Append-oriented speakup log with daily sent counters."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()
        ensure_dir(self.db_path.parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._explicit_feedback_reader: Any | None = None
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS speakups (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    committed_at REAL,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    context_snapshot_json TEXT NOT NULL,
                    outcome TEXT,
                    outcome_classified_at REAL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_speakups_chat_day
                ON speakups(channel, chat_id, committed_at)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS taste_distillations (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sample_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (channel, chat_id, sample_fingerprint)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_reservations (
                    proposal_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    limit_value INTEGER NOT NULL,
                    window_ms INTEGER NOT NULL,
                    window_kind TEXT NOT NULL DEFAULT 'rolling',
                    created_at_ms INTEGER NOT NULL,
                    submitted_at_ms INTEGER,
                    accepted_at_ms INTEGER,
                    delivered_at_ms INTEGER,
                    released_at_ms INTEGER,
                    delivery_state TEXT NOT NULL,
                    provider_message_id TEXT,
                    evidence_kind TEXT,
                    evidence_ref TEXT,
                    attempt_state TEXT NOT NULL DEFAULT 'unsubmitted',
                    observed_revision INTEGER,
                    activation_epoch INTEGER,
                    lane TEXT NOT NULL DEFAULT 'production',
                    origin TEXT NOT NULL DEFAULT 'legacy',
                    proposal_revision INTEGER NOT NULL DEFAULT 1,
                    outcome TEXT,
                    outcome_kind TEXT,
                    outcome_evidence_json TEXT,
                    outcome_at_ms INTEGER,
                    PRIMARY KEY (effect_id, category)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_proposal
                ON delivery_reservations(proposal_id, effect_id)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_state
                ON delivery_reservations(channel, chat_id, delivery_state, created_at_ms)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_category
                ON delivery_reservations(channel, chat_id, category, accepted_at_ms)
                """
            )
            self._apply_delivery_columns()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judge_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL,
                    evaluation_index INTEGER NOT NULL DEFAULT 0,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    hourly_limit INTEGER NOT NULL,
                    continuation_candidate INTEGER NOT NULL DEFAULT 0,
                    continuation_reserve INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    detail_code TEXT
                )
                """
            )
            self._apply_judge_columns()
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_judge_attempts_chat
                ON judge_attempts(channel, chat_id, created_at_ms)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_judge_attempts_opportunity
                ON judge_attempts(opportunity_id, evaluation_index)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS opportunity_dispositions (
                    opportunity_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    observed_revision INTEGER NOT NULL DEFAULT 0,
                    activation_epoch INTEGER NOT NULL DEFAULT 0,
                    lane TEXT NOT NULL DEFAULT 'production',
                    trigger TEXT NOT NULL DEFAULT 'inbound',
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    disposition TEXT NOT NULL,
                    reason TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            self._apply_disposition_columns()
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dispositions_revision
                ON opportunity_dispositions(channel, chat_id, observed_revision)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS social_anchor_closures (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    anchor_message_id TEXT NOT NULL,
                    closed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (channel, chat_id, anchor_message_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_claims (
                    proposal_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    owner_channel TEXT NOT NULL DEFAULT '',
                    owner_chat_id TEXT NOT NULL DEFAULT '',
                    owner_id TEXT NOT NULL DEFAULT '',
                    payload_hash TEXT NOT NULL DEFAULT '',
                    proposal_revision INTEGER NOT NULL DEFAULT 1,
                    target_effect_id TEXT NOT NULL DEFAULT '',
                    claimed_at_ms INTEGER,
                    resolved_at_ms INTEGER,
                    resolution TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_approval_state
                ON approval_claims(state, updated_at_ms)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_revisions (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    considered_revision INTEGER NOT NULL DEFAULT 0,
                    shadow_considered_revision INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (channel, chat_id)
                )
                """
            )
            self._apply_chat_revision_columns()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_claims (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    claim_key TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lane TEXT NOT NULL DEFAULT 'production',
                    activation_epoch INTEGER NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (channel, chat_id, claim_key)
                )
                """
            )
            self._apply_source_claim_columns()
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_claims_owner
                ON source_claims(channel, chat_id, owner, claimed_at_ms)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_revisions (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (channel, chat_id, source_event_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_revisions_chat
                ON source_revisions(channel, chat_id, revision)
                """
            )
            self._migrate_source_revisions()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activation_state (
                    scope TEXT PRIMARY KEY,
                    activation_epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at_ms INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Databases created before ``fingerprint`` existed get the column here, and
            # the marker row below tells a later run that no transition has happened yet.
            self._apply_activation_columns()
            self._conn.execute(
                """
                INSERT OR IGNORE INTO activation_state (
                    scope, activation_epoch, updated_at_ms, fingerprint
                ) VALUES ('participation', 1, CAST(strftime('%s','now') AS INTEGER) * 1000, '')
                """
            )
            self._conn.commit()

    def _apply_delivery_columns(self) -> None:
        """Additive, idempotent column migration for existing ledger databases."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(delivery_reservations)")
        }
        for name, ddl in (
            ("origin", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("outcome", "TEXT"),
            ("outcome_kind", "TEXT"),
            ("outcome_evidence_json", "TEXT"),
            ("outcome_at_ms", "INTEGER"),
        ):
            if name in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE delivery_reservations ADD COLUMN {name} {ddl}"
            )

    def _apply_chat_revision_columns(self) -> None:
        """Additive migration for durable material watermarks on old ledgers."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(chat_revisions)")
        }
        for name in ("considered_revision", "shadow_considered_revision"):
            if name in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE chat_revisions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )

    def _apply_source_claim_columns(self) -> None:
        """Keep old claim rows readable while adding lane metadata additively."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(source_claims)")
        }
        if "lane" not in existing:
            self._conn.execute(
                "ALTER TABLE source_claims ADD COLUMN lane TEXT NOT NULL DEFAULT 'production'"
            )

    def _apply_disposition_columns(self) -> None:
        """Add fields needed to migrate legacy considered dispositions safely."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(opportunity_dispositions)")
        }
        for name, ddl in (
            ("observed_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("activation_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("lane", "TEXT NOT NULL DEFAULT 'production'"),
            ("trigger", "TEXT NOT NULL DEFAULT 'inbound'"),
            ("source_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("reason", "TEXT"),
            ("created_at_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("updated_at_ms", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE opportunity_dispositions ADD COLUMN {name} {ddl}"
            )

    @staticmethod
    def _source_id(value: object) -> str | None:
        token = str(value or "").strip()
        if not token or token.startswith("observed:"):
            return None
        return token

    @staticmethod
    def _considered_column(lane: str) -> str:
        if str(lane) == "shadow":
            return "shadow_considered_revision"
        if str(lane) == "production":
            return "considered_revision"
        raise ValueError(f"unknown participation lane: {lane}")

    def _ensure_chat_revision_row(
        self, conn: sqlite3.Connection, *, channel: str, chat_id: str, now_ms: int
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_revisions (
                channel, chat_id, revision, considered_revision,
                shadow_considered_revision, updated_at_ms
            ) VALUES (?, ?, 0, 0, 0, ?)
            """,
            (str(channel), str(chat_id), int(now_ms)),
        )

    def _set_revision_at_least(
        self,
        conn: sqlite3.Connection,
        *,
        channel: str,
        chat_id: str,
        revision: int,
        now_ms: int,
    ) -> None:
        self._ensure_chat_revision_row(
            conn, channel=channel, chat_id=chat_id, now_ms=now_ms
        )
        conn.execute(
            """
            UPDATE chat_revisions
            SET revision = MAX(revision, ?), updated_at_ms = ?
            WHERE channel = ? AND chat_id = ?
            """,
            (max(0, int(revision)), int(now_ms), str(channel), str(chat_id)),
        )

    def _allocate_source_revision(
        self,
        conn: sqlite3.Connection,
        *,
        channel: str,
        chat_id: str,
        minimum: int = 0,
        now_ms: int,
    ) -> int:
        """Allocate one revision without consulting retained archive rows."""
        self._ensure_chat_revision_row(
            conn, channel=channel, chat_id=chat_id, now_ms=now_ms
        )
        row = conn.execute(
            "SELECT revision FROM chat_revisions WHERE channel = ? AND chat_id = ?",
            (str(channel), str(chat_id)),
        ).fetchone()
        current = int(row["revision"] if row is not None else 0)
        revision = max(current + 1, int(minimum))
        conn.execute(
            """
            UPDATE chat_revisions SET revision = ?, updated_at_ms = ?
            WHERE channel = ? AND chat_id = ?
            """,
            (revision, int(now_ms), str(channel), str(chat_id)),
        )
        return revision

    def _mark_considered_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        channel: str,
        chat_id: str,
        observed_revision: int,
        lane: str,
        now_ms: int,
    ) -> None:
        revision = int(observed_revision)
        if revision <= 0:
            return
        column = self._considered_column(lane)
        self._ensure_chat_revision_row(
            conn, channel=channel, chat_id=chat_id, now_ms=now_ms
        )
        conn.execute(
            f"""
            UPDATE chat_revisions
            SET {column} = MAX({column}, ?), updated_at_ms = ?
            WHERE channel = ? AND chat_id = ?
            """,
            (revision, int(now_ms), str(channel), str(chat_id)),
        )

    def _migrate_source_revisions(self) -> None:
        """Backfill source identity mappings and watermarks from legacy ledger rows."""
        now_ms = int(time.time() * 1000)
        dispositions = self._conn.execute(
            """
            SELECT channel, chat_id, observed_revision, lane, source_ids_json
            FROM opportunity_dispositions
            ORDER BY created_at_ms ASC, opportunity_id ASC
            """
        ).fetchall()
        for row in dispositions:
            channel = str(row["channel"])
            chat_id = str(row["chat_id"])
            lane = str(row["lane"] or "production")
            if lane not in {"production", "shadow"}:
                lane = "production"
            observed = max(0, int(row["observed_revision"] or 0))
            try:
                parsed = json.loads(str(row["source_ids_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
            raw_ids = parsed if isinstance(parsed, (list, tuple)) else []
            source_ids = tuple(dict.fromkeys(filter(None, (self._source_id(item) for item in raw_ids))))
            highest = observed
            for material_source_id in source_ids:
                mapped = self._conn.execute(
                    """
                    SELECT revision FROM source_revisions
                    WHERE channel = ? AND chat_id = ? AND source_event_id = ?
                    """,
                    (channel, chat_id, material_source_id),
                ).fetchone()
                if mapped is None:
                    revision = (
                        observed
                        if observed > 0
                        else self._allocate_source_revision(
                            self._conn,
                            channel=channel,
                            chat_id=chat_id,
                            now_ms=now_ms,
                        )
                    )
                    self._set_revision_at_least(
                        self._conn,
                        channel=channel,
                        chat_id=chat_id,
                        revision=revision,
                        now_ms=now_ms,
                    )
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO source_revisions (
                            channel, chat_id, source_event_id, revision, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (channel, chat_id, material_source_id, revision, now_ms),
                    )
                else:
                    revision = int(mapped["revision"])
                highest = max(highest, revision)
            self._mark_considered_in_connection(
                self._conn,
                channel=channel,
                chat_id=chat_id,
                observed_revision=highest,
                lane=lane,
                now_ms=now_ms,
            )

        claims = self._conn.execute(
            """
            SELECT channel, chat_id, claim_key, lane
            FROM source_claims
            ORDER BY claimed_at_ms ASC, claim_key ASC
            """
        ).fetchall()
        for row in claims:
            channel = str(row["channel"])
            chat_id = str(row["chat_id"])
            claim_key = str(row["claim_key"] or "").strip()
            lane = str(row["lane"] or "production")
            source_id: str | None
            if claim_key.startswith("shadow:"):
                lane = "shadow"
                source_id = self._source_id(claim_key.removeprefix("shadow:"))
            else:
                source_id = self._source_id(claim_key)
            if lane not in {"production", "shadow"} or source_id is None:
                continue
            mapped = self._conn.execute(
                """
                SELECT revision FROM source_revisions
                WHERE channel = ? AND chat_id = ? AND source_event_id = ?
                """,
                (channel, chat_id, source_id),
            ).fetchone()
            revision = int(mapped["revision"]) if mapped is not None else 0
            if mapped is None:
                revision = self._allocate_source_revision(
                    self._conn,
                    channel=channel,
                    chat_id=chat_id,
                    now_ms=now_ms,
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO source_revisions (
                        channel, chat_id, source_event_id, revision, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (channel, chat_id, source_id, revision, now_ms),
                )
            self._mark_considered_in_connection(
                self._conn,
                channel=channel,
                chat_id=chat_id,
                observed_revision=revision,
                lane=lane,
                now_ms=now_ms,
            )

    def _apply_activation_columns(self) -> None:
        """Additive, idempotent migration for existing activation state rows."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(activation_state)")
        }
        if existing and "fingerprint" not in existing:
            self._conn.execute(
                "ALTER TABLE activation_state ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"
            )
            # The row predates the column: record that its inputs are unknown, so the
            # first observation adopts the current state instead of faking a transition.
            self._conn.execute(
                "UPDATE activation_state SET fingerprint = '' WHERE fingerprint IS NULL"
            )

    def _apply_judge_columns(self) -> None:
        """Additive, idempotent migration for sanitized judge details."""
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(judge_attempts)")
        }
        if existing and "detail_code" not in existing:
            self._conn.execute("ALTER TABLE judge_attempts ADD COLUMN detail_code TEXT")

    def _set_activation_fingerprint(
        self, conn: sqlite3.Connection, scope: str, fingerprint: str
    ) -> None:
        """Record the inputs behind the current epoch, on databases old and new."""
        columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(activation_state)")
        }
        if not columns or "fingerprint" not in columns:
            # A database created before this column existed: migrate it here, inside the
            # caller's transaction, so the write and the migration cannot diverge.
            self._apply_activation_columns()
        conn.execute(
            "UPDATE activation_state SET fingerprint = ? WHERE scope = ?",
            (str(fingerprint), str(scope)),
        )

    # -- transactions ------------------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Short exclusive write transaction. Never wrap a provider call in this."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- participation ledger ----------------------------------------------------------

    def reserve_delivery_sync(
        self,
        *,
        proposal_id: str,
        effect_id: str,
        channel: str,
        chat_id: str,
        now_ms: int,
        limits: tuple[tuple[str, int, int], ...] | tuple[tuple[str, int, int, str], ...],
        proposal_revision: int = 1,
        observed_revision: int | None = None,
        activation_epoch: int | None = None,
        lane: str = "production",
        origin: str = "legacy",
    ) -> bool:
        """Synchronous reservation core: one transaction, no check-then-send race.

        ``limits`` entries are ``(category, limit, window_ms)`` for a rolling
        window, or ``(category, limit, window_ms, "calendar_day")`` for the
        current UTC calendar day (initiation). Zero limits deny the action. All
        dimensions are acquired together or none are. A duplicate reservation for
        the same ``(proposal_id, effect_id)`` is idempotent and returns whether
        that reservation is still held within its original ten-minute lease.
        """
        proposal = str(proposal_id or "").strip()
        effect = str(effect_id or "").strip()
        if not proposal or not effect:
            raise ValueError("proposal_id and effect_id are required")
        if not limits:
            return False
        reservation_origin = str(origin or "legacy").strip()
        if reservation_origin not in {"legacy", "participation"}:
            raise ValueError(f"unknown delivery origin: {reservation_origin}")
        reservation_lane = str(lane or "production").strip()
        if reservation_lane not in {"production", "shadow"}:
            raise ValueError(f"unknown participation lane: {reservation_lane}")
        checked: list[tuple[str, int, int, str]] = []
        for entry in limits:
            category, limit, window_ms = str(entry[0]), int(entry[1]), int(entry[2])
            window_kind = str(entry[3]) if len(entry) > 3 else "rolling"
            if category not in RESERVATION_CATEGORIES:
                raise ValueError(f"unknown reservation category: {category}")
            if limit <= 0:
                return False
            checked.append((category, limit, window_ms, window_kind))
        with self._write() as conn:
            # The effect id is unique across the ledger. One effect owns one
            # reservation row per dimension; the delivery state lives on every row
            # of that effect and is always written for all of them together.
            existing = conn.execute(
                """
                SELECT delivery_state, channel, chat_id, lane, origin, created_at_ms, effect_id, proposal_id
                FROM delivery_reservations
                WHERE effect_id = ? OR (proposal_id = ? AND channel = ? AND chat_id = ? AND lane = ?)
                LIMIT 1
                """,
                (effect, proposal, str(channel), str(chat_id), reservation_lane),
            ).fetchone()
            if existing is not None:
                if (
                    int(now_ms) >= int(existing["created_at_ms"]) + DELIVERY_RESERVATION_TTL_MS
                    or str(existing["effect_id"]) != effect
                    or str(existing["proposal_id"]) != proposal
                    or str(existing["channel"]) != str(channel)
                    or str(existing["chat_id"]) != str(chat_id)
                    or str(existing["lane"]) != reservation_lane
                    or str(existing["origin"]) != reservation_origin
                ):
                    return False
                state = str(existing["delivery_state"])
                if state in RELEASED_DELIVERY_STATES:
                    return False
                return state != "released"
            for category, limit, window_ms, window_kind in checked:
                if window_kind == "calendar_day":
                    allowed, _reason = _calendar_day_capacity(
                        conn=conn,
                        channel=str(channel),
                        chat_id=str(chat_id),
                        category=category,
                        limit=limit,
                        now_ms=int(now_ms),
                        exclude_proposal_id=proposal,
                    )
                else:
                    allowed, _reason = _rolling_capacity(
                        conn=conn,
                        channel=str(channel),
                        chat_id=str(chat_id),
                        category=category,
                        limit=limit,
                        now_ms=int(now_ms),
                        window_ms=window_ms,
                        exclude_proposal_id=proposal,
                    )
                if not allowed:
                    return False
            for category, limit, window_ms, window_kind in checked:
                conn.execute(
                    """
                    INSERT INTO delivery_reservations (
                        proposal_id, effect_id, channel, chat_id, category,
                        limit_value, window_ms, window_kind, created_at_ms,
                        delivery_state, attempt_state, observed_revision,
                        activation_epoch, lane, origin, proposal_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 'unsubmitted', ?, ?, ?, ?, ?)
                    ON CONFLICT(effect_id, category) DO NOTHING
                    """,
                    (
                        proposal,
                        effect,
                        str(channel),
                        str(chat_id),
                        category,
                        limit,
                        window_ms,
                        window_kind,
                        int(now_ms),
                        observed_revision,
                        activation_epoch,
                        reservation_lane,
                        reservation_origin,
                        int(proposal_revision),
                    ),
                )
        return True

    async def reserve_delivery(
        self,
        *,
        proposal_id: str,
        effect_id: str,
        channel: str,
        chat_id: str,
        now_ms: int,
        limits: tuple[tuple[str, int, int], ...] | tuple[tuple[str, int, int, str], ...],
        proposal_revision: int = 1,
        observed_revision: int | None = None,
        activation_epoch: int | None = None,
        lane: str = "production",
        origin: str = "legacy",
    ) -> bool:
        """Async wrapper around :meth:`reserve_delivery_sync`."""
        return self.reserve_delivery_sync(
            proposal_id=proposal_id,
            effect_id=effect_id,
            channel=channel,
            chat_id=chat_id,
            now_ms=now_ms,
            limits=limits,
            proposal_revision=proposal_revision,
            observed_revision=observed_revision,
            activation_epoch=activation_epoch,
            lane=lane,
            origin=origin,
        )

    async def reserve_judge_attempt(
        self,
        attempt_id: str,
        *,
        opportunity_id: str,
        channel: str,
        chat_id: str,
        now_ms: int,
        hourly_limit: int,
        min_gap_ms: int,
        continuation_candidate: bool,
        continuation_reserve: int,
    ) -> bool:
        """Charge one unaddressed judge attempt before the provider call.

        The attempt id is deterministic (``opportunity_id:evaluation_index``), so a
        duplicate attempt returns ``False`` and never pays for a second provider
        call. Attempts are persisted, so a restart cannot reset cost limits.
        """
        attempt = str(attempt_id or "").strip()
        if not attempt:
            raise ValueError("attempt_id is required")
        limit = int(hourly_limit)
        reserve = max(0, min(int(continuation_reserve), limit))
        with self._write() as conn:
            exists = conn.execute(
                "SELECT 1 FROM judge_attempts WHERE attempt_id = ? LIMIT 1",
                (attempt,),
            ).fetchone()
            if exists is not None:
                return False
            if limit <= 0:
                return False
            window_start = int(now_ms) - 3_600_000
            total_row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM judge_attempts
                WHERE channel = ? AND chat_id = ? AND created_at_ms > ?
                """,
                (str(channel), str(chat_id), window_start),
            ).fetchone()
            total = int(total_row["c"] if total_row else 0)
            if total >= limit:
                return False
            if continuation_candidate:
                if total >= limit:
                    return False
            elif total >= max(0, limit - reserve):
                # Background initiation may not consume the protected continuation slots.
                return False
            if int(min_gap_ms) > 0:
                # A reconsideration inside the already admitted chain needs no new gap;
                # it still pays an attempt from the same total quota.
                same_chain = conn.execute(
                    "SELECT 1 FROM judge_attempts WHERE opportunity_id = ? LIMIT 1",
                    (str(opportunity_id),),
                ).fetchone()
                if same_chain is None:
                    last_row = conn.execute(
                        """
                        SELECT created_at_ms FROM judge_attempts
                        WHERE channel = ? AND chat_id = ?
                        ORDER BY created_at_ms DESC LIMIT 1
                        """,
                        (str(channel), str(chat_id)),
                    ).fetchone()
                    if last_row is not None:
                        gap = int(now_ms) - int(last_row["created_at_ms"])
                        if gap < int(min_gap_ms):
                            return False
            evaluation_index = 0
            head = attempt.rsplit(":", 1)
            if len(head) == 2 and head[1].isdigit():
                evaluation_index = int(head[1])
            conn.execute(
                """
                INSERT INTO judge_attempts (
                    attempt_id, opportunity_id, evaluation_index, channel, chat_id,
                    created_at_ms, hourly_limit, continuation_candidate,
                    continuation_reserve
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt,
                    str(opportunity_id),
                    evaluation_index,
                    str(channel),
                    str(chat_id),
                    int(now_ms),
                    limit,
                    1 if continuation_candidate else 0,
                    reserve,
                ),
            )
        return True

    async def record_judge_outcome(
        self,
        attempt_id: str,
        *,
        outcome: str,
        detail_code: str | None = None,
    ) -> None:
        detail = str(detail_code or "").strip()
        if detail not in JUDGE_DETAIL_CODES:
            detail = ""
        with self._write() as conn:
            conn.execute(
                "UPDATE judge_attempts SET outcome = ?, detail_code = ? WHERE attempt_id = ?",
                (str(outcome), detail or None, str(attempt_id)),
            )

    async def judge_attempts_since(
        self, *, channel: str, chat_id: str, since_ms: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM judge_attempts
                WHERE channel = ? AND chat_id = ? AND created_at_ms >= ?
                ORDER BY created_at_ms ASC
                """,
                (str(channel), str(chat_id), int(since_ms)),
            ).fetchall()
        return [dict(row) for row in rows]

    def next_chat_revision_sync(self, *, channel: str, chat_id: str) -> int:
        """Monotonic per-chat revision for a new trigger (never a wall-clock value).

        The revision is derived from the durable identity of what has already been
        recorded for the chat, so a restart cannot hand out a smaller number and a
        clock change cannot invent a newer revision.
        """
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM source_claims
                WHERE channel = ? AND chat_id = ? AND lane = 'production'
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            claims = int(row["c"] if row else 0)
            disposition_row = conn.execute(
                """
                SELECT COALESCE(MAX(observed_revision), 0) AS r FROM opportunity_dispositions
                WHERE channel = ? AND chat_id = ?
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            considered = int(disposition_row["r"] if disposition_row else 0)
        return max(claims, considered) + 1

    def next_source_revision_sync(self, *, channel: str, chat_id: str) -> int:
        """Atomically advance and return the durable per-chat source revision.

        This is the monotonic watermark the opportunity identity and the restart
        duplicate guard both use. It is not a hash of message text and not wall time,
        so a clock change cannot invent a newer revision and a restart cannot hand out
        a smaller one.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO chat_revisions (channel, chat_id, revision, updated_at_ms)
                VALUES (?, ?, 1, CAST(strftime('%s','now') AS INTEGER) * 1000)
                ON CONFLICT(channel, chat_id) DO UPDATE SET
                    revision = chat_revisions.revision + 1,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (str(channel), str(chat_id)),
            )
            row = conn.execute(
                "SELECT revision FROM chat_revisions WHERE channel = ? AND chat_id = ?",
                (str(channel), str(chat_id)),
            ).fetchone()
        return int(row["revision"]) if row is not None else 1

    def ensure_source_revisions_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        source_ids: tuple[str, ...] | list[str],
        now_ms: int | None = None,
    ) -> tuple[tuple[str, int], ...]:
        """Register each new canonical source once and return its durable revision."""
        normalized = tuple(
            dict.fromkeys(
                token
                for token in (self._source_id(item) for item in source_ids)
                if token is not None
            )
        )
        if not channel or not chat_id or not normalized:
            return ()
        moment = int(now_ms if now_ms is not None else time.time() * 1000)
        result: list[tuple[str, int]] = []
        with self._write() as conn:
            self._ensure_chat_revision_row(
                conn, channel=channel, chat_id=chat_id, now_ms=moment
            )
            for source_id in normalized:
                row = conn.execute(
                    """
                    SELECT revision FROM source_revisions
                    WHERE channel = ? AND chat_id = ? AND source_event_id = ?
                    """,
                    (str(channel), str(chat_id), source_id),
                ).fetchone()
                if row is None:
                    revision = self._allocate_source_revision(
                        conn,
                        channel=channel,
                        chat_id=chat_id,
                        now_ms=moment,
                    )
                    conn.execute(
                        """
                        INSERT INTO source_revisions (
                            channel, chat_id, source_event_id, revision, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (str(channel), str(chat_id), source_id, revision, moment),
                    )
                else:
                    revision = int(row["revision"])
                result.append((source_id, revision))
        return tuple(result)

    def material_for_opportunity(
        self,
        channel: str,
        chat_id: str,
        source_ids: tuple[str, ...] | None,
        *,
        lane: str = "production",
    ) -> tuple[tuple[str, ...], int]:
        """Return registered source ids without advancing their considered watermark.

        Explicit source ids are returned only after an archive/authorization layer has
        registered them with :meth:`ensure_source_revisions_sync`.  ``None`` returns
        only registered sources newer than the lane's durable considered watermark.
        """
        column = self._considered_column(lane)
        if not channel or not chat_id:
            return (), 0
        normalized = tuple(
            dict.fromkeys(
                token
                for token in (self._source_id(item) for item in (source_ids or ()))
                if token is not None
            )
        )
        with self._lock:
            state = self._conn.execute(
                f"""
                SELECT {column} AS considered FROM chat_revisions
                WHERE channel = ? AND chat_id = ?
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            considered = int(state["considered"] if state is not None else 0)
            if source_ids is None:
                rows = self._conn.execute(
                    """
                    SELECT source_event_id, revision FROM source_revisions
                    WHERE channel = ? AND chat_id = ? AND revision > ?
                    ORDER BY revision ASC, source_event_id ASC
                    """,
                    (str(channel), str(chat_id), considered),
                ).fetchall()
            elif not normalized:
                rows = []
            else:
                placeholders = ",".join("?" for _ in normalized)
                rows = self._conn.execute(
                    f"""
                    SELECT source_event_id, revision FROM source_revisions
                    WHERE channel = ? AND chat_id = ?
                      AND revision > ?
                      AND source_event_id IN ({placeholders})
                    ORDER BY revision ASC, source_event_id ASC
                    """,
                    (str(channel), str(chat_id), considered, *normalized),
                ).fetchall()
        material = tuple(str(row["source_event_id"]) for row in rows)
        revision = max((int(row["revision"]) for row in rows), default=considered)
        return material, revision

    def mark_material_considered_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        observed_revision: int,
        lane: str = "production",
        now_ms: int | None = None,
    ) -> None:
        """Advance a lane watermark only after an offer was accepted by the queue."""
        moment = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            self._mark_considered_in_connection(
                conn,
                channel=channel,
                chat_id=chat_id,
                observed_revision=int(observed_revision),
                lane=lane,
                now_ms=moment,
            )

    def initialize_material_baseline_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        source_ids: tuple[str, ...] | list[str] = (),
        lane: str = "production",
        now_ms: int | None = None,
    ) -> int:
        """Atomically baseline retained history for a never-seen chat.

        A baseline is accepted only when this ledger has no chat row, source mapping,
        claim, or disposition.  It is therefore safe for first enablement while a
        restart with registered-but-unconsidered material remains visible to the
        normal ``source_ids=None`` lookup.
        """
        column = self._considered_column(lane)
        normalized = tuple(
            dict.fromkeys(
                token
                for token in (self._source_id(item) for item in source_ids)
                if token is not None
            )
        )
        if not channel or not chat_id:
            return 0
        moment = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            existing_chat = conn.execute(
                "SELECT 1 FROM chat_revisions WHERE channel = ? AND chat_id = ?",
                (str(channel), str(chat_id)),
            ).fetchone()
            existing_material = conn.execute(
                """
                SELECT 1 FROM source_revisions
                WHERE channel = ? AND chat_id = ? LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            existing_claim = conn.execute(
                """
                SELECT 1 FROM source_claims
                WHERE channel = ? AND chat_id = ? LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            existing_disposition = conn.execute(
                """
                SELECT 1 FROM opportunity_dispositions
                WHERE channel = ? AND chat_id = ? LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
            if any(
                item is not None
                for item in (existing_chat, existing_material, existing_claim, existing_disposition)
            ):
                row = conn.execute(
                    f"""
                    SELECT COALESCE({column}, 0) AS considered
                    FROM chat_revisions WHERE channel = ? AND chat_id = ?
                    """,
                    (str(channel), str(chat_id)),
                ).fetchone()
                return int(row["considered"] if row is not None else 0)

            self._ensure_chat_revision_row(
                conn, channel=channel, chat_id=chat_id, now_ms=moment
            )
            highest = 0
            for source_id in normalized:
                revision = self._allocate_source_revision(
                    conn,
                    channel=channel,
                    chat_id=chat_id,
                    now_ms=moment,
                )
                conn.execute(
                    """
                    INSERT INTO source_revisions (
                        channel, chat_id, source_event_id, revision, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(channel), str(chat_id), source_id, revision, moment),
                )
                highest = max(highest, revision)
            # A first-enable baseline is administrative history, not an observation
            # by one lane. Both lanes start after the same retained history so a later
            # shadow/live switch cannot replay it as production work.
            for baseline_lane in ("production", "shadow"):
                self._mark_considered_in_connection(
                    conn,
                    channel=channel,
                    chat_id=chat_id,
                    observed_revision=highest,
                    lane=baseline_lane,
                    now_ms=moment,
                )
            return highest

    def close_social_anchor_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        anchor_message_id: str,
        now_ms: int | None = None,
    ) -> bool:
        """Durably close one exact social anchor, idempotently.

        This projection is intentionally narrower than a task/thread closure: it
        only retires the supplied delivered-message association in this chat.
        """
        target = (str(channel).strip(), str(chat_id).strip(), str(anchor_message_id).strip())
        if not all(target):
            return False
        moment = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO social_anchor_closures (
                    channel, chat_id, anchor_message_id, closed_at_ms
                ) VALUES (?, ?, ?, ?)
                """,
                (*target, moment),
            )
        return bool(cursor.rowcount)

    async def close_social_anchor(
        self,
        *,
        channel: str,
        chat_id: str,
        anchor_message_id: str,
        now_ms: int | None = None,
    ) -> bool:
        """Async facade for :meth:`close_social_anchor_sync`."""
        return self.close_social_anchor_sync(
            channel=channel,
            chat_id=chat_id,
            anchor_message_id=anchor_message_id,
            now_ms=now_ms,
        )

    def social_anchor_closed_sync(
        self, *, channel: str, chat_id: str, anchor_message_id: str
    ) -> bool:
        """Read the durable closure for one exact social anchor."""
        target = (str(channel).strip(), str(chat_id).strip(), str(anchor_message_id).strip())
        if not all(target):
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM social_anchor_closures
                WHERE channel = ? AND chat_id = ? AND anchor_message_id = ?
                LIMIT 1
                """,
                target,
            ).fetchone()
        return row is not None

    async def social_anchor_closed(
        self, *, channel: str, chat_id: str, anchor_message_id: str
    ) -> bool:
        """Async facade for :meth:`social_anchor_closed_sync`."""
        return self.social_anchor_closed_sync(
            channel=channel,
            chat_id=chat_id,
            anchor_message_id=anchor_message_id,
        )

    def activation_epoch_sync(
        self, scope: str = "participation", *, fingerprint: str | None = None
    ) -> int:
        """Synchronous read of the persisted activation epoch (schema-safe).

        Passing ``fingerprint`` also records the activation inputs that produced this
        epoch. That record has to be durable: an in-process memory of "the last inputs
        I saw" is empty after a restart, so a shadow/live change made while the process
        was down would otherwise never advance the epoch.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT activation_epoch FROM activation_state WHERE scope = ?",
                (str(scope),),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO activation_state (scope, activation_epoch, updated_at_ms)
                    VALUES (?, 1, CAST(strftime('%s','now') AS INTEGER) * 1000)
                    """,
                    (str(scope),),
                )
                current = 1
            else:
                current = int(row["activation_epoch"])
            if fingerprint is not None:
                self._set_activation_fingerprint(conn, str(scope), str(fingerprint))
            return current

    def activation_fingerprint_sync(self, scope: str = "participation") -> str:
        """The activation inputs recorded with the current epoch, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM activation_state WHERE scope = ?",
                (str(scope),),
            ).fetchone()
        if row is None:
            return ""
        try:
            return str(row["fingerprint"] or "")
        except (IndexError, KeyError):
            return ""

    def refresh_activation_sync(
        self,
        scope: str = "participation",
        *,
        fingerprint: str,
        now_ms: int | None = None,
    ) -> int:
        """Persist one complete activation fingerprint and fence changes atomically.

        The comparison and increment share the existing ledger transaction.  An empty
        fingerprint row is adopted at its existing epoch (for databases created before
        activation fingerprints existed); every later distinct fingerprint advances
        exactly once.  A repeated refresh, including after a restart, is a read.
        """
        value = str(fingerprint)
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            row = conn.execute(
                "SELECT activation_epoch, fingerprint FROM activation_state WHERE scope = ?",
                (str(scope),),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO activation_state (
                        scope, activation_epoch, updated_at_ms, fingerprint
                    ) VALUES (?, 1, ?, ?)
                    """,
                    (str(scope), ts, value),
                )
                return 1

            current = int(row["activation_epoch"])
            previous = str(row["fingerprint"] or "")
            if not previous or previous == value:
                if previous != value:
                    self._set_activation_fingerprint(conn, str(scope), value)
                return current

            conn.execute(
                """
                UPDATE activation_state
                SET activation_epoch = activation_epoch + 1,
                    updated_at_ms = ?, fingerprint = ?
                WHERE scope = ?
                """,
                (ts, value, str(scope)),
            )
            return current + 1

    def claim_source_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_id: str,
        activation_epoch: int,
        lane: str,
        owner: str,
        now_ms: int,
    ) -> tuple[bool, str]:
        """Claim one source for one owner. The first claim wins, across restarts.

        The ledger key is the chat plus the source identity. A source the legacy path
        already produced can never be replayed by the participation lane, even after
        a later activation epoch - that is what makes a cutover safe rather than
        merely ordered.
        """
        key = str(source_event_id)
        if str(lane) == "shadow":
            # Shadow is a separate observational lane: it must never consume a
            # production source id, so it claims under its own namespace.
            key = f"shadow:{key}"
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT owner FROM source_claims
                WHERE channel = ? AND chat_id = ? AND claim_key = ?
                """,
                (str(channel), str(chat_id), key),
            ).fetchone()
            if row is not None:
                # Second value is informational: a repeated claim by the same owner is
                # granted but is not a new claim.
                return (False, str(row["owner"]))
            conn.execute(
                """
                INSERT INTO source_claims (
                    channel, chat_id, claim_key, owner, lane, activation_epoch, claimed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(channel),
                    str(chat_id),
                    key,
                    str(owner),
                    str(lane),
                    int(activation_epoch),
                    int(now_ms),
                ),
            )
        return True, str(owner)

    def release_source_claim_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        source_event_id: str,
        lane: str,
        owner: str,
    ) -> bool:
        """Release only a just-created claim when queue admission is rejected."""
        key = str(source_event_id)
        if str(lane) == "shadow":
            key = f"shadow:{key}"
        with self._write() as conn:
            cursor = conn.execute(
                """
                DELETE FROM source_claims
                WHERE channel = ? AND chat_id = ? AND claim_key = ? AND owner = ?
                """,
                (str(channel), str(chat_id), key, str(owner)),
            )
        return bool(cursor.rowcount)

    async def source_claims(
        self, *, channel: str, chat_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM source_claims
                WHERE channel = ? AND chat_id = ?
                ORDER BY claimed_at_ms ASC, claim_key ASC
                LIMIT ?
                """,
                (str(channel), str(chat_id), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def release_source_claims(
        self, *, channel: str, chat_id: str, owner: str | None = None
    ) -> int:
        """Drop claims for rollback preparation. Never used to replay old sources."""
        with self._write() as conn:
            if owner is None:
                cursor = conn.execute(
                    "DELETE FROM source_claims WHERE channel = ? AND chat_id = ?",
                    (str(channel), str(chat_id)),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM source_claims WHERE channel = ? AND chat_id = ? AND owner = ?",
                    (str(channel), str(chat_id), str(owner)),
                )
        return int(cursor.rowcount or 0)

    async def record_approval_claim(
        self,
        proposal_id: str,
        *,
        owner_channel: str,
        owner_chat_id: str,
        owner_id: str,
        payload_hash: str,
        proposal_revision: int,
        target_effect_id: str,
        now_ms: int,
        ttl_ms: int = 3_600_000,
    ) -> bool:
        """Persist CAS state ``pending -> claimed`` for one owner approval.

        A caller-supplied boolean is never approval authority: ``submit_proposal``
        loads this row and refuses when the claimed hash/revision does not match the
        payload it is about to submit. Repeated codes converge on the same claim.
        """
        del ttl_ms
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM approval_claims WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO approval_claims (
                        proposal_id, state, owner_channel, owner_chat_id, owner_id,
                        payload_hash, proposal_revision, target_effect_id,
                        claimed_at_ms, created_at_ms, updated_at_ms
                    ) VALUES (?, 'claimed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(proposal_id),
                        str(owner_channel),
                        str(owner_chat_id),
                        str(owner_id),
                        str(payload_hash),
                        int(proposal_revision),
                        str(target_effect_id),
                        int(now_ms),
                        int(now_ms),
                        int(now_ms),
                    ),
                )
                return True
            state = str(row["state"])
            if state == "terminal":
                return False
            if str(row["payload_hash"]) not in {"", str(payload_hash)}:
                # A different payload may never be submitted under this claim.
                return False
            if str(row["owner_chat_id"]) not in {"", str(owner_chat_id)}:
                return False
            if str(row["owner_channel"]) not in {"", str(owner_channel)}:
                return False
            conn.execute(
                """
                UPDATE approval_claims
                SET state = 'claimed', claimed_at_ms = COALESCE(claimed_at_ms, ?),
                    updated_at_ms = ?, target_effect_id = ?
                WHERE proposal_id = ?
                """,
                (int(now_ms), int(now_ms), str(target_effect_id), str(proposal_id)),
            )
        return True

    async def approval_claim(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approval_claims WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def resolve_approval_claim(
        self,
        proposal_id: str,
        *,
        resolution: str,
        now_ms: int,
    ) -> bool:
        """Move a claim to its terminal state. Idempotent per proposal."""
        with self._write() as conn:
            row = conn.execute(
                "SELECT state FROM approval_claims WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                return False
            if str(row["state"]) == "terminal":
                return False
            conn.execute(
                """
                UPDATE approval_claims
                SET state = 'terminal', resolution = ?, resolved_at_ms = ?, updated_at_ms = ?
                WHERE proposal_id = ?
                """,
                (str(resolution), int(now_ms), int(now_ms), str(proposal_id)),
            )
        return True

    async def proposal_row(self, proposal_id: str) -> dict[str, Any] | None:
        """Read one durable proposal row (survives restart; no in-memory state)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM speakups WHERE id = ?",
                (str(proposal_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def record_preview_effect(
        self,
        proposal_id: str,
        *,
        preview_effect_id: str,
        preview_operation_ref: str,
        accepted: bool,
        now: float | None = None,
    ) -> None:
        """Record the owner-destination preview effect separately from target truth."""
        await self.mark_status(proposal_id, status="awaiting_approval")
        with self._write() as conn:
            conn.execute(
                """
                UPDATE speakups SET context_snapshot_json = json_set(
                    COALESCE(NULLIF(context_snapshot_json, ''), '{}'),
                    '$.preview_effect_id', ?,
                    '$.preview_operation_ref', ?,
                    '$.preview_accepted', ?
                ) WHERE id = ?
                """,
                (
                    str(preview_effect_id),
                    str(preview_operation_ref),
                    1 if accepted else 0,
                    str(proposal_id),
                ),
            )

    async def record_send_attempt(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        now_ms: int,
    ) -> None:
        """Mark the reservation as submitted to the effect gateway (local queue only)."""
        with self._write() as conn:
            reservation = conn.execute(
                "SELECT created_at_ms FROM delivery_reservations WHERE effect_id = ? LIMIT 1",
                (str(effect_id),),
            ).fetchone()
            if reservation is not None and int(now_ms) >= (
                int(reservation["created_at_ms"]) + DELIVERY_RESERVATION_TTL_MS
            ):
                raise ValueError("reservation_expired")
            conn.execute(
                """
                UPDATE delivery_reservations
                SET attempt_state = 'submitted',
                    submitted_at_ms = COALESCE(submitted_at_ms, ?),
                    delivery_state = CASE
                        WHEN delivery_state = 'reserved' THEN 'submitted'
                        ELSE delivery_state
                    END
                WHERE effect_id = ?
                """,
                (int(now_ms), str(effect_id)),
            )

    async def project_transport_accepted(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        provider_message_id: str | None,
        evidence_kind: str,
        evidence_ref: str,
        now_ms: int,
        group_scope: bool = False,
    ) -> str:
        """Project a validated transport acceptance exactly once.

        Acceptance consumes the send allowance; it is *not* recipient delivery.
        A later duplicate acceptance callback changes nothing and never charges
        capacity a second time.
        """
        if evidence_kind not in TRANSPORT_EVIDENCE_KINDS:
            raise ValueError(f"not transport evidence: {evidence_kind}")
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT delivery_state, accepted_at_ms FROM delivery_reservations
                WHERE effect_id = ?
                """,
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise ValueError("no reservation for this proposal/effect")
            state = str(row["delivery_state"])
            if state in {"delivered", "transport_accepted"}:
                return state
            if state in RELEASED_DELIVERY_STATES:
                return state
            conn.execute(
                """
                UPDATE delivery_reservations
                SET delivery_state = 'transport_accepted',
                    accepted_at_ms = ?,
                    provider_message_id = COALESCE(?, provider_message_id),
                    evidence_kind = ?, evidence_ref = ?, attempt_state = 'accepted'
                WHERE effect_id = ?
                """,
                (
                    int(now_ms),
                    provider_message_id,
                    str(evidence_kind),
                    str(evidence_ref),
                    str(effect_id),
                ),
            )
        await self.mark_status(proposal_id, status="transport_accepted")
        return "transport_accepted"

    async def project_recipient_delivery(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        provider_message_id: str | None,
        evidence_kind: str,
        evidence_ref: str,
        now_ms: int,
        group_scope: bool = False,
    ) -> bool:
        """Project exact recipient evidence once. Returns True for a new delivery.

        Provider ids alone never reach this method: only authenticated target
        delivery/read/quote/reaction evidence does (spec section 9).
        """
        if evidence_kind not in RECIPIENT_EVIDENCE_KINDS and (
            evidence_kind not in GROUP_SCOPED_EVIDENCE_KINDS
        ):
            raise ValueError(f"not recipient evidence: {evidence_kind}")
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT delivery_state FROM delivery_reservations
                WHERE effect_id = ?
                """,
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise ValueError("no reservation for this proposal/effect")
            if str(row["delivery_state"]) == "delivered":
                return False
            conn.execute(
                """
                UPDATE delivery_reservations
                SET delivery_state = 'delivered',
                    delivered_at_ms = COALESCE(delivered_at_ms, ?),
                    accepted_at_ms = CASE WHEN delivery_state = 'transport_accepted'
                        THEN COALESCE(accepted_at_ms, ?) ELSE ? END,
                    provider_message_id = COALESCE(?, provider_message_id),
                    evidence_kind = ?, evidence_ref = ?, attempt_state = 'delivered'
                WHERE effect_id = ?
                """,
                (
                    int(now_ms),
                    int(now_ms),
                    int(now_ms),
                    provider_message_id,
                    str(evidence_kind),
                    str(evidence_ref),
                    str(effect_id),
                ),
            )
        await self.mark_status(proposal_id, status="delivered")
        return True

    async def note_delivery_unknown(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        evidence_kind: str,
        evidence_ref: str,
        now_ms: int,
    ) -> None:
        """Retain the reservation for an inconclusive outcome. Never a refund."""
        with self._write() as conn:
            updated = conn.execute(
                """
                UPDATE delivery_reservations
                SET delivery_state = 'delivery_unknown',
                    evidence_kind = ?, evidence_ref = ?, attempt_state = 'unknown'
                WHERE effect_id = ?
                  AND delivery_state IN ('reserved', 'submitted')
                """,
                (
                    str(evidence_kind),
                    str(evidence_ref),
                    str(effect_id),
                ),
            )
        if updated.rowcount:
            await self.mark_status(proposal_id, status="delivery_unknown")

    async def release_delivery(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        state: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        """Release an unsubmitted/definitely-unaccepted reservation.

        An accepted send is never refunded. Unknown delivery evidence is retained;
        its budget lease expires independently after ten minutes.
        """
        if state not in RELEASED_DELIVERY_STATES:
            raise ValueError(f"not a releasing state: {state}")
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT delivery_state FROM delivery_reservations
                WHERE effect_id = ?
                """,
                (str(effect_id),),
            ).fetchone()
            if row is None:
                return False
            current = str(row["delivery_state"])
            if current in CONSUMED_DELIVERY_STATES or current == "delivered":
                return False
            if current in RELEASED_DELIVERY_STATES:
                return False
            conn.execute(
                """
                UPDATE delivery_reservations
                SET delivery_state = ?, released_at_ms = ?, evidence_kind = 'release',
                    evidence_ref = ?, attempt_state = 'released'
                WHERE effect_id = ?
                """,
                (str(state), int(now_ms), str(reason), str(effect_id)),
            )
        await self.mark_status(proposal_id, status=state, reason=reason)
        return True

    async def delivery_state(self, *, proposal_id: str, effect_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT delivery_state FROM delivery_reservations
                WHERE effect_id = ?
                """,
                (str(effect_id),),
            ).fetchone()
        return str(row["delivery_state"]) if row is not None else None

    async def delivery_record(self, *, proposal_id: str, effect_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM delivery_reservations
                WHERE effect_id = ?
                """,
                (str(effect_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def record_delivery(
        self,
        *,
        effect_id: str,
        state: str,
        provider_message_id: str | None,
        now_ms: int,
        evidence_kind: str,
        evidence_ref: str,
    ) -> bool:
        """Single entry point for one evidenced delivery transition, keyed by effect id.

        This is the interface named by the implementation plan; the richer
        ``project_*`` methods are the projections it dispatches to. It refuses to
        guess: the evidence kind decides which state is admissible, so a provider
        message id can never be promoted to ``delivered``.
        """
        row = self._delivery_row_for_effect(effect_id)
        if row is None:
            raise ValueError(f"no reservation for effect {effect_id}")
        proposal_id = str(row["proposal_id"])
        if state == "transport_accepted":
            await self.project_transport_accepted(
                proposal_id,
                effect_id=str(effect_id),
                provider_message_id=provider_message_id,
                evidence_kind=evidence_kind,
                evidence_ref=evidence_ref,
                now_ms=now_ms,
            )
            return True
        if state == "delivered":
            return await self.project_recipient_delivery(
                proposal_id,
                effect_id=str(effect_id),
                provider_message_id=provider_message_id,
                evidence_kind=evidence_kind,
                evidence_ref=evidence_ref,
                now_ms=now_ms,
            )
        if state == "failed":
            return await self.release_delivery(
                proposal_id,
                effect_id=str(effect_id),
                state="failed",
                reason=evidence_ref or evidence_kind,
                now_ms=now_ms,
            )
        raise ValueError(f"unsupported delivery state: {state}")

    def _delivery_row_for_effect(self, effect_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delivery_reservations WHERE effect_id = ? LIMIT 1",
                (str(effect_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def consumed_slots(
        self,
        *,
        channel: str,
        chat_id: str,
        category: str,
        now_ms: int,
        window_ms: int,
        window_kind: str = "rolling",
    ) -> int:
        """Accepted sends plus unresolved holds for one dimension (inspection/tests)."""
        with self._lock:
            if window_kind == "calendar_day":
                current = datetime.fromtimestamp(int(now_ms) / 1000, UTC)
                day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
                start_ms = int(day_start.timestamp() * 1000)
                end_ms = int((day_start + timedelta(days=1)).timestamp() * 1000)
                return _held_or_accepted_count(
                    conn=self._conn,
                    channel=channel,
                    chat_id=chat_id,
                    category=category,
                    exclude_proposal_id="",
                    now_ms=now_ms,
                    accepted_since_ms=start_ms,
                    accepted_before_ms=end_ms,
                )
            return _held_or_accepted_count(
                conn=self._conn,
                channel=channel,
                chat_id=chat_id,
                category=category,
                exclude_proposal_id="",
                now_ms=now_ms,
                accepted_since_ms=int(now_ms) - max(1, int(window_ms)) + 1,
            )

    async def pending_delivery_reservations(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        origin: str | None = None,
        lane: str | None = None,
        states: tuple[str, ...] = ("reserved", "submitted", "transport_accepted", "delivery_unknown"),
    ) -> list[dict[str, Any]]:
        """Holds that are not terminally released, oldest first.

        ``attempt_state='unsubmitted'`` rows are cancellable speculation; rows that
        were already handed to transport need reconciliation, never a blind resend.
        """
        if not states:
            return []
        clauses = [f"delivery_state IN ({','.join('?' for _ in states)})"]
        params: list[Any] = list(states)
        if origin is not None:
            clauses.append("origin = ?")
            params.append(str(origin))
        if lane is not None:
            clauses.append("lane = ?")
            params.append(str(lane))
        params.extend((max(1, int(limit)), max(0, int(offset))))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM delivery_reservations
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE delivery_state
                    WHEN 'submitted' THEN 0
                    WHEN 'transport_accepted' THEN 0
                    WHEN 'reserved' THEN 1
                    ELSE 2
                END, created_at_ms ASC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    async def record_disposition(
        self,
        *,
        opportunity_id: str,
        channel: str,
        chat_id: str,
        disposition: str,
        reason: str | None = None,
        observed_revision: int = 0,
        activation_epoch: int = 0,
        lane: str = "production",
        trigger: str = "inbound",
        source_ids: tuple[str, ...] = (),
        now_ms: int | None = None,
    ) -> None:
        """Persist the disposition of one opportunity (idempotent per opportunity id)."""
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO opportunity_dispositions (
                    opportunity_id, channel, chat_id, observed_revision,
                    activation_epoch, lane, trigger, source_ids_json, disposition,
                    reason, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id) DO UPDATE SET
                    disposition = excluded.disposition,
                    reason = excluded.reason,
                    observed_revision = MAX(
                        opportunity_dispositions.observed_revision, excluded.observed_revision
                    ),
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    str(opportunity_id),
                    str(channel),
                    str(chat_id),
                    int(observed_revision),
                    int(activation_epoch),
                    str(lane),
                    str(trigger),
                    json.dumps(list(source_ids)),
                    str(disposition),
                    reason,
                    ts,
                    ts,
                ),
            )

    async def disposition_by_chat(
        self, channel: str, chat_id: str
    ) -> dict[str, Any] | None:
        """Most recent recorded disposition for one chat (inspection and tests)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM opportunity_dispositions
                WHERE channel = ? AND chat_id = ?
                ORDER BY updated_at_ms DESC LIMIT 1
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    async def disposition(self, opportunity_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM opportunity_dispositions WHERE opportunity_id = ?",
                (str(opportunity_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def highest_considered_revision(
        self, *, channel: str, chat_id: str, lane: str = "production"
    ) -> int:
        column = self._considered_column(lane)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COALESCE({column}, 0) AS r FROM chat_revisions
                WHERE channel = ? AND chat_id = ?
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
        return int(row["r"]) if row is not None and row["r"] is not None else 0

    def highest_considered_revision_sync(
        self, *, channel: str, chat_id: str, lane: str = "production"
    ) -> int:
        """Synchronous startup read for the producer's durable replay watermark."""
        column = self._considered_column(lane)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COALESCE({column}, 0) AS r FROM chat_revisions
                WHERE channel = ? AND chat_id = ?
                """,
                (str(channel), str(chat_id)),
            ).fetchone()
        return int(row["r"]) if row is not None and row["r"] is not None else 0

    async def activation_epoch(self, scope: str = "participation") -> int:
        with self._write() as conn:
            row = conn.execute(
                "SELECT activation_epoch FROM activation_state WHERE scope = ?",
                (str(scope),),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO activation_state (scope, activation_epoch, updated_at_ms)
                    VALUES (?, 1, ?)
                    """,
                    (str(scope), int(time.time() * 1000)),
                )
                return 1
            return int(row["activation_epoch"])

    async def advance_activation_epoch(
        self, scope: str = "participation", *, now_ms: int | None = None
    ) -> int:
        """Async wrapper around :meth:`advance_activation_epoch_sync`."""
        return self.advance_activation_epoch_sync(scope, now_ms=now_ms)

    def advance_activation_epoch_sync(
        self,
        scope: str = "participation",
        *,
        now_ms: int | None = None,
        fingerprint: str | None = None,
    ) -> int:
        """Atomically advance the persisted activation epoch and return the new value."""
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO activation_state (scope, activation_epoch, updated_at_ms)
                VALUES (?, 2, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    activation_epoch = activation_state.activation_epoch + 1,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (str(scope), ts),
            )
            if fingerprint is not None:
                self._set_activation_fingerprint(conn, str(scope), str(fingerprint))
            row = conn.execute(
                "SELECT activation_epoch FROM activation_state WHERE scope = ?",
                (str(scope),),
            ).fetchone()
        return int(row["activation_epoch"]) if row is not None else 1

    async def pending_outcome_deliveries(
        self, *, before_ms: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Confirmed, recipient-evidenced deliveries whose observation window elapsed.

        Only ``delivered`` rows are eligible: previews, transport-accepted-only,
        unknown, failed and historical-unverified rows are excluded by construction
        (spec section 10).
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_reservations
                WHERE origin = 'participation' AND lane = 'production'
                  AND delivery_state = 'delivered'
                  AND delivered_at_ms IS NOT NULL
                  AND delivered_at_ms <= ?
                ORDER BY delivered_at_ms ASC
                LIMIT ?
                """,
                (int(before_ms), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def explicit_feedback(
        self, *, channel: str, chat_id: str, provider_message_id: str
    ) -> dict[str, Any] | None:
        """Exact quoted reply or reaction for one delivered bot message, if retained.

        Exact feedback is strong evidence and needs no classifier call. When the
        archive holds no such record this returns ``None``; nothing is inferred from
        emoji text.
        """
        token = str(provider_message_id or "").strip()
        if not token:
            return None
        reader = self._explicit_feedback_reader
        if reader is None:
            return None
        try:
            found = reader(channel=str(channel), chat_id=str(chat_id), message_id=token)
            if hasattr(found, "__await__"):
                found = await found
        except Exception:
            return None
        return found if isinstance(found, dict) else None

    def set_explicit_feedback_reader(self, reader: Any | None) -> None:
        """Inject the archive-backed lookup for exact quotes/reactions."""
        self._explicit_feedback_reader = reader

    async def mark_delivery_outcome(
        self,
        *,
        effect_id: str,
        outcome: str,
        evidence_kind: str,
        evidence_ids: tuple[str, ...] = (),
        now_ms: int | None = None,
    ) -> None:
        """Record the classified outcome with its evidence provenance.

        ``evidence_kind`` is one of ``explicit``, ``inferred``, ``none`` or
        ``uncertain``; the sample only becomes learning input once it carries that
        provenance (spec section 10).
        """
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._write() as conn:
            conn.execute(
                "UPDATE delivery_reservations SET outcome = ?, outcome_kind = ?, "
                "outcome_evidence_json = ?, outcome_at_ms = ? WHERE effect_id = ?",
                (
                    str(outcome),
                    str(evidence_kind),
                    json.dumps(list(evidence_ids)),
                    ts,
                    str(effect_id),
                ),
            )

    async def participation_outcome_samples(
        self, *, channel: str, chat_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Delivered, provenance-tagged participation samples for one chat.

        Untagged historical records and unverified deliveries are excluded, so old
        patterns are never promoted to authoritative new guidance.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_reservations
                WHERE origin = 'participation' AND lane = 'production'
                  AND channel = ? AND chat_id = ? AND delivery_state = 'delivered'
                  AND outcome IS NOT NULL AND outcome_kind IS NOT NULL
                ORDER BY outcome_at_ms DESC, delivered_at_ms DESC
                LIMIT ?
                """,
                (str(channel), str(chat_id), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def participation_outcome_chats(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT channel, chat_id, MAX(outcome_at_ms) AS latest_at
                FROM delivery_reservations
                WHERE origin = 'participation' AND lane = 'production'
                  AND delivery_state = 'delivered' AND outcome IS NOT NULL
                  AND outcome_kind IS NOT NULL
                GROUP BY channel, chat_id
                ORDER BY latest_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def delivered_reservation_rows(
        self,
        *,
        channel: str,
        chat_id: str,
        since_ms: int,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Confirmed recipient deliveries for one exact chat, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_reservations
                WHERE channel = ? AND chat_id = ? AND delivery_state = 'delivered'
                  AND origin = 'participation' AND lane = 'production'
                  AND delivered_at_ms IS NOT NULL AND delivered_at_ms >= ?
                ORDER BY delivered_at_ms DESC, effect_id ASC
                LIMIT ?
                """,
                (str(channel), str(chat_id), int(since_ms), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def record_proposed(
        self,
        *,
        proposal_id: str | None,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        trigger: str,
        context_snapshot: dict[str, object],
        now: float | None = None,
    ) -> str:
        entry_id = proposal_id or uuid.uuid4().hex
        created_at = float(now if now is not None else time.time())
        self._insert(
            entry_id=entry_id,
            created_at=created_at,
            committed_at=None,
            channel=channel,
            chat_id=chat_id,
            action_type=action_type,
            profile=profile,
            message=message,
            status="proposed",
            trigger=trigger,
            context_snapshot=context_snapshot,
        )
        return entry_id

    async def record_sent(
        self,
        *,
        proposal_id: str,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        trigger: str,
        context_snapshot: dict[str, object],
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        self._insert(
            entry_id=proposal_id,
            created_at=ts,
            committed_at=ts,
            channel=channel,
            chat_id=chat_id,
            action_type=action_type,
            profile=profile,
            message=message,
            status="sent",
            trigger=trigger,
            context_snapshot=context_snapshot,
            replace=True,
        )

    async def mark_sent(self, proposal_id: str, *, now: float | None = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE speakups SET status = ?, committed_at = ? WHERE id = ?",
                ("sent", ts, proposal_id),
            )
            self._conn.commit()

    async def mark_status(
        self,
        proposal_id: str,
        *,
        status: str,
        reason: str | None = None,
    ) -> None:
        with self._lock:
            if reason is None:
                self._conn.execute(
                    "UPDATE speakups SET status = ? WHERE id = ?",
                    (status, proposal_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE speakups
                    SET status = ?, context_snapshot_json = json_set(
                        COALESCE(NULLIF(context_snapshot_json, ''), '{}'),
                        '$.status_reason',
                        ?
                    )
                    WHERE id = ?
                    """,
                    (status, reason, proposal_id),
                )
            self._conn.commit()

    async def mark_rejected(self, proposal_id: str, *, reason: str) -> None:
        await self.mark_status(proposal_id, status="rejected", reason=reason)

    async def mark_outcome(
        self,
        proposal_id: str,
        *,
        outcome: str,
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE speakups SET outcome = ?, outcome_classified_at = ? WHERE id = ?",
                (outcome, ts, proposal_id),
            )
            self._conn.commit()

    async def record_silent_pass(
        self,
        *,
        channel: str,
        chat_id: str,
        profile: str,
        trigger: str,
        reason: str,
        context_snapshot: dict[str, object] | None = None,
        now: float | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex
        snapshot = dict(context_snapshot or {})
        snapshot["reason"] = reason
        self._insert(
            entry_id=entry_id,
            created_at=float(now if now is not None else time.time()),
            committed_at=None,
            channel=channel,
            chat_id=chat_id,
            action_type="silent_pass",
            profile=profile,
            message="",
            status="silent_pass",
            trigger=trigger,
            context_snapshot=snapshot,
        )
        return entry_id

    async def count_sent_today(
        self,
        *,
        channel: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        start = current.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM ({_CONFIRMED_SPEAKUPS_SQL}) "
                "WHERE sent_at >= :start AND sent_at < :end",
                dict(channel=channel, chat_id=chat_id, start=start.timestamp(), end=end.timestamp()),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def count_sent_since(
        self,
        *,
        channel: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM ({_CONFIRMED_SPEAKUPS_SQL}) WHERE sent_at >= :since",
                dict(channel=channel, chat_id=chat_id, since=since.timestamp()),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def last_sent_at(
        self,
        *,
        channel: str,
        chat_id: str,
    ) -> float | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT MAX(sent_at) AS sent_at FROM ({_CONFIRMED_SPEAKUPS_SQL})",
                dict(channel=channel, chat_id=chat_id),
            ).fetchone()
        return float(row["sent_at"]) if row is not None and row["sent_at"] is not None else None

    async def history(self, channel: str, chat_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE channel = ? AND chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (channel, chat_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def pending_outcome_rows(self, *, before: float, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE status = 'sent'
                  AND committed_at IS NOT NULL
                  AND committed_at <= ?
                  AND outcome IS NULL
                ORDER BY committed_at ASC
                LIMIT ?
                """,
                (float(before), max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def outcome_samples(
        self,
        *,
        channel: str,
        chat_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND outcome IS NOT NULL
                ORDER BY committed_at DESC, created_at DESC
                LIMIT ?
                """,
                (channel, chat_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def outcome_sample_chats(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT channel, chat_id, MAX(COALESCE(committed_at, created_at)) AS latest_at
                FROM speakups
                WHERE status = 'sent'
                  AND outcome IS NOT NULL
                GROUP BY channel, chat_id
                ORDER BY latest_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def learning_summary(self, *, channel: str, chat_id: str) -> dict[str, Any]:
        """Return compact learning counters for one chat without exposing raw messages."""
        with self._lock:
            sent = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ? AND chat_id = ? AND status = 'sent'
                """,
                (channel, chat_id),
            ).fetchone()
            labeled = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ? AND chat_id = ? AND outcome IS NOT NULL
                """,
                (channel, chat_id),
            ).fetchone()
            outcomes = self._conn.execute(
                """
                SELECT outcome, COUNT(*) AS c
                FROM speakups
                WHERE channel = ? AND chat_id = ? AND outcome IS NOT NULL
                GROUP BY outcome
                ORDER BY outcome
                """,
                (channel, chat_id),
            ).fetchall()
            distillations = self._conn.execute(
                """
                SELECT COUNT(*) AS c, MAX(created_at) AS latest_at
                FROM taste_distillations
                WHERE channel = ? AND chat_id = ?
                """,
                (channel, chat_id),
            ).fetchone()
        return {
            "sent_speakups": int(sent["c"] if sent else 0),
            "labeled_outcomes": int(labeled["c"] if labeled else 0),
            "outcomes": {str(row["outcome"]): int(row["c"]) for row in outcomes},
            "taste_distillations": int(distillations["c"] if distillations else 0),
            "last_taste_distillation_at": (
                datetime.fromtimestamp(float(distillations["latest_at"]), UTC).isoformat()
                if distillations and distillations["latest_at"] is not None
                else None
            ),
        }

    def _insert(
        self,
        *,
        entry_id: str,
        created_at: float,
        committed_at: float | None,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        status: str,
        trigger: str,
        context_snapshot: dict[str, object],
        replace: bool = False,
    ) -> None:
        sql = "INSERT OR REPLACE" if replace else "INSERT"
        with self._lock:
            self._conn.execute(
                f"""
                {sql} INTO speakups (
                    id, created_at, committed_at, channel, chat_id, action_type,
                    profile, message, status, trigger, context_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    created_at,
                    committed_at,
                    channel,
                    chat_id,
                    action_type,
                    profile,
                    message,
                    status,
                    trigger,
                    json.dumps(context_snapshot, sort_keys=True),
                ),
            )
            self._conn.commit()

    async def has_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM taste_distillations
                WHERE channel = ? AND chat_id = ? AND sample_fingerprint = ?
                LIMIT 1
                """,
                (channel, chat_id, sample_fingerprint),
            ).fetchone()
        return row is not None

    async def record_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO taste_distillations (
                    channel, chat_id, sample_fingerprint, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (channel, chat_id, sample_fingerprint, ts),
            )
            self._conn.commit()

    async def claim_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
        now: float | None = None,
    ) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO taste_distillations (
                    channel, chat_id, sample_fingerprint, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (channel, chat_id, sample_fingerprint, ts),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    async def delete_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM taste_distillations
                WHERE channel = ? AND chat_id = ? AND sample_fingerprint = ?
                """,
                (channel, chat_id, sample_fingerprint),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
