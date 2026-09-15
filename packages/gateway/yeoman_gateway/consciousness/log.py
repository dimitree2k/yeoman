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

#: Reservation categories with their identity in the ledger.
RESERVATION_CATEGORIES: tuple[str, ...] = ("initiation", "comment", "reaction")

#: Delivery states in which the reserved capacity is definitely gone.
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

#: Evidence kinds that prove a recipient received the exact target message.
RECIPIENT_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"recipient_delivery", "recipient_read", "quote_proof", "reaction_proof"}
)

#: Evidence kinds scoped to at least one recipient instead of every member.
GROUP_SCOPED_EVIDENCE_KINDS: frozenset[str] = frozenset({"group_delivery_at_least_one"})


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
    accepted_since_ms: int | None = None,
    accepted_before_ms: int | None = None,
) -> int:
    """Accepted sends inside the accounting window **plus** every unresolved hold.

    The two sets are a union, not an intersection: a hold that was never accepted
    still occupies its slot, and an unresolved hold is counted regardless of age -
    an unknown outcome never regains capacity by crossing a day or rolling-window
    boundary (spec section 9).
    """
    sql = [
        "SELECT COUNT(*) AS c FROM delivery_reservations",
        "WHERE channel = ? AND chat_id = ? AND category = ? AND proposal_id <> ?",
    ]
    params: list[Any] = [channel, chat_id, category, exclude_proposal_id]
    if accepted_since_ms is not None:
        sql.append(
            "AND ((accepted_at_ms IS NOT NULL AND accepted_at_ms >= ?"
            " AND (? IS NULL OR accepted_at_ms < ?))"
            " OR delivery_state IN"
            " ('reserved', 'submitted', 'transport_accepted', 'delivery_unknown'))"
        )
        params.append(int(accepted_since_ms))
        params.append(None if accepted_before_ms is None else int(accepted_before_ms))
        params.append(None if accepted_before_ms is None else int(accepted_before_ms))
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
    """Capacity for the current UTC calendar day. Indefinite holds still count."""
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
                    outcome TEXT
                )
                """
            )
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
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dispositions_revision
                ON opportunity_dispositions(channel, chat_id, observed_revision)
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
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_claims_owner
                ON source_claims(channel, chat_id, owner, claimed_at_ms)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activation_state (
                    scope TEXT PRIMARY KEY,
                    activation_epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at_ms INTEGER NOT NULL
                )
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
    ) -> bool:
        """Synchronous reservation core: one transaction, no check-then-send race.

        ``limits`` entries are ``(category, limit, window_ms)`` for a rolling
        window, or ``(category, limit, window_ms, "calendar_day")`` for the
        current UTC calendar day (initiation). Zero limits deny the action. All
        dimensions are acquired together or none are. A duplicate reservation for
        the same ``(proposal_id, effect_id)`` is idempotent and returns whether
        that reservation is still held.
        """
        proposal = str(proposal_id or "").strip()
        effect = str(effect_id or "").strip()
        if not proposal or not effect:
            raise ValueError("proposal_id and effect_id are required")
        if not limits:
            return False
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
                SELECT delivery_state FROM delivery_reservations
                WHERE effect_id = ? LIMIT 1
                """,
                (effect,),
            ).fetchone()
            if existing is not None:
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
                        activation_epoch, lane, proposal_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', 'unsubmitted', ?, ?, ?, ?)
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
                        str(lane),
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

    async def record_judge_outcome(self, attempt_id: str, *, outcome: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE judge_attempts SET outcome = ? WHERE attempt_id = ?",
                (str(outcome), str(attempt_id)),
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

    def activation_epoch_sync(self, scope: str = "participation") -> int:
        """Synchronous read of the persisted activation epoch (schema-safe)."""
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
                return 1
            return int(row["activation_epoch"])

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
                    accepted_at_ms = COALESCE(accepted_at_ms, ?),
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
                    accepted_at_ms = COALESCE(accepted_at_ms, ?),
                    provider_message_id = COALESCE(?, provider_message_id),
                    evidence_kind = ?, evidence_ref = ?, attempt_state = 'delivered'
                WHERE effect_id = ?
                """,
                (
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
            conn.execute(
                """
                UPDATE delivery_reservations
                SET delivery_state = 'delivery_unknown',
                    accepted_at_ms = COALESCE(accepted_at_ms, ?),
                    evidence_kind = ?, evidence_ref = ?, attempt_state = 'unknown'
                WHERE effect_id = ?
                  AND delivery_state IN ('reserved', 'submitted', 'transport_accepted')
                """,
                (
                    int(now_ms),
                    str(evidence_kind),
                    str(evidence_ref),
                    str(effect_id),
                ),
            )
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

        An accepted send is never refunded, and an unknown in-flight delivery keeps
        its hold even after expiry or pause (spec section 9).
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
                    accepted_since_ms=start_ms,
                    accepted_before_ms=end_ms,
                )
            return _held_or_accepted_count(
                conn=self._conn,
                channel=channel,
                chat_id=chat_id,
                category=category,
                exclude_proposal_id="",
                accepted_since_ms=int(now_ms) - max(1, int(window_ms)) + 1,
            )

    async def pending_delivery_reservations(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Holds that are not terminally released, oldest first.

        ``attempt_state='unsubmitted'`` rows are cancellable speculation; rows that
        were already handed to transport need reconciliation, never a blind resend.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM delivery_reservations
                WHERE delivery_state IN ('reserved', 'submitted', 'transport_accepted', 'delivery_unknown')
                ORDER BY created_at_ms ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
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
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MAX(observed_revision) AS r FROM opportunity_dispositions
                WHERE channel = ? AND chat_id = ? AND lane = ?
                """,
                (str(channel), str(chat_id), str(lane)),
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
        self, scope: str = "participation", *, now_ms: int | None = None
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
                WHERE delivery_state = 'delivered'
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
                WHERE channel = ? AND chat_id = ? AND delivery_state = 'delivered'
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
                WHERE delivery_state = 'delivered' AND outcome IS NOT NULL
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
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at >= ?
                  AND committed_at < ?
                """,
                (channel, chat_id, start.timestamp(), end.timestamp()),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def count_sent_since(
        self,
        *,
        channel: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        current_since = since
        if current_since.tzinfo is None:
            current_since = current_since.replace(tzinfo=UTC)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at >= ?
                """,
                (channel, chat_id, current_since.astimezone(UTC).timestamp()),
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
                """
                SELECT committed_at
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at IS NOT NULL
                ORDER BY committed_at DESC
                LIMIT 1
                """,
                (channel, chat_id),
            ).fetchone()
        if row is None or row["committed_at"] is None:
            return None
        return float(row["committed_at"])

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
