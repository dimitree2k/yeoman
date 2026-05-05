"""Manual persona evolution proposal helpers."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import ensure_dir, safe_filename

from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.memory.service import MemoryService
from yeoman_gateway.policy.persona import resolve_persona_path
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive

PERSONA_EVOLUTION_PROPOSAL_TTL_SECONDS = 24 * 60 * 60
PERSONA_EVOLUTION_ALLOWED_SECTIONS = {
    "How This File Works",
    "Trait Drift",
    "Domain Confidence",
    "Relationship Map",
    "Relationship And Group Map",
    "Schema Log",
    "Consciousness Outcome Lessons",
    "Consolidation Changelog",
}


@dataclass(frozen=True, slots=True)
class PersonaChatRef:
    channel: str
    chat_id: str
    persona_file: str
    source: str


@dataclass(frozen=True, slots=True)
class ChatEvolutionEvidence:
    chat: PersonaChatRef
    learned_taste: list[str] = field(default_factory=list)
    recent_preferences: list[str] = field(default_factory=list)
    speakup_outcomes: dict[str, int] = field(default_factory=dict)
    speakups: list[dict[str, Any]] = field(default_factory=list)
    recent_message_count: int = 0


@dataclass(frozen=True, slots=True)
class PersonaEvolutionEvidence:
    persona_file: str
    persona_path: Path
    evolution_path: Path
    collected_at: datetime
    evidence_since: datetime
    window_days: int
    chats: list[ChatEvolutionEvidence]
    current_evolution_text: str

    @property
    def total_message_count(self) -> int:
        return sum(chat.recent_message_count for chat in self.chats)


@dataclass(frozen=True, slots=True)
class PersonaEvolutionDecisionResult:
    status: str
    proposal_id: str
    persona_file: str | None = None
    evolution_path: Path | None = None
    message: str = ""


class PersonaEvolutionLedger:
    """SQLite ledger for persona evolution scan/proposal state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()
        ensure_dir(self.db_path.parent)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_evolution_scans (
                id TEXT PRIMARY KEY,
                persona_file TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                evidence_from TEXT NOT NULL,
                evidence_to TEXT NOT NULL,
                total_message_count INTEGER NOT NULL,
                signal_score REAL NOT NULL,
                result TEXT NOT NULL,
                reason TEXT,
                proposal_id TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_persona_evolution_scans_persona_time
            ON persona_evolution_scans(persona_file, scanned_at)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_evolution_proposals (
                proposal_id TEXT PRIMARY KEY,
                persona_file TEXT NOT NULL,
                status TEXT NOT NULL,
                proposal_path TEXT,
                created_at TEXT NOT NULL,
                evidence_from TEXT NOT NULL,
                evidence_to TEXT NOT NULL,
                total_message_count INTEGER NOT NULL,
                signal_score REAL NOT NULL,
                base_hash TEXT NOT NULL,
                closed_at TEXT,
                final_outcome TEXT
            )
            """
        )
        self._ensure_proposal_columns(
            {
                "persona_hash": "TEXT",
                "applied_hash": "TEXT",
                "approval_channel": "TEXT",
                "approval_chat_id": "TEXT",
                "notified_at": "TEXT",
                "notification_channel": "TEXT",
                "notification_chat_id": "TEXT",
            }
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_persona_evolution_proposals_persona_status
            ON persona_evolution_proposals(persona_file, status, evidence_to)
            """
        )
        self._conn.commit()

    def _ensure_proposal_columns(self, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(persona_evolution_proposals)"
            ).fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE persona_evolution_proposals ADD COLUMN {name} {declaration}"
                )

    def pending_proposal(self, persona_file: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT *
            FROM persona_evolution_proposals
            WHERE persona_file = ? AND status = 'proposed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(persona_file),),
        ).fetchone()
        return dict(row) if row else None

    def pending_proposals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT *
            FROM persona_evolution_proposals
            WHERE status = 'proposed'
            ORDER BY created_at ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_scans(
        self,
        *,
        persona_file: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if persona_file:
            rows = self._conn.execute(
                """
                SELECT *
                FROM persona_evolution_scans
                WHERE persona_file = ?
                ORDER BY scanned_at DESC
                LIMIT ?
                """,
                (str(persona_file), max(1, min(int(limit), 50))),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT *
                FROM persona_evolution_scans
                ORDER BY scanned_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def proposal_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM persona_evolution_proposals
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}

    def scan_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT COALESCE(reason, result) AS bucket, COUNT(*) AS c
            FROM persona_evolution_scans
            GROUP BY COALESCE(reason, result)
            ORDER BY bucket
            """
        ).fetchall()
        return {str(row["bucket"]): int(row["c"]) for row in rows}

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT *
            FROM persona_evolution_proposals
            WHERE proposal_id = ?
            LIMIT 1
            """,
            (str(proposal_id),),
        ).fetchone()
        return dict(row) if row else None

    def latest_closed_watermark(self, persona_file: str) -> datetime | None:
        row = self._conn.execute(
            """
            SELECT evidence_to
            FROM persona_evolution_proposals
            WHERE persona_file = ?
              AND status != 'proposed'
              AND closed_at IS NOT NULL
            ORDER BY evidence_to DESC
            LIMIT 1
            """,
            (str(persona_file),),
        ).fetchone()
        if row is None:
            return None
        return _parse_utc_datetime(str(row["evidence_to"]))

    def record_scan(
        self,
        *,
        persona_file: str,
        scanned_at: datetime,
        evidence_from: datetime,
        evidence_to: datetime,
        total_message_count: int,
        signal_score: float,
        result: str,
        reason: str | None = None,
        proposal_id: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO persona_evolution_scans (
                id, persona_file, scanned_at, evidence_from, evidence_to,
                total_message_count, signal_score, result, reason, proposal_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                str(persona_file),
                _iso_utc(scanned_at),
                _iso_utc(evidence_from),
                _iso_utc(evidence_to),
                int(total_message_count),
                float(signal_score),
                str(result),
                str(reason) if reason else None,
                str(proposal_id) if proposal_id else None,
            ),
        )
        self._conn.commit()

    def record_proposal(
        self,
        *,
        proposal_id: str,
        persona_file: str,
        proposal_path: Path,
        created_at: datetime,
        evidence_from: datetime,
        evidence_to: datetime,
        total_message_count: int,
        signal_score: float,
        base_hash: str,
        persona_hash: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO persona_evolution_proposals (
                proposal_id, persona_file, status, proposal_path, created_at,
                evidence_from, evidence_to, total_message_count, signal_score,
                base_hash, persona_hash, closed_at, final_outcome
            ) VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                str(proposal_id),
                str(persona_file),
                str(proposal_path),
                _iso_utc(created_at),
                _iso_utc(evidence_from),
                _iso_utc(evidence_to),
                int(total_message_count),
                float(signal_score),
                str(base_hash),
                str(persona_hash or ""),
            ),
        )
        self._conn.commit()

    def close_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        final_outcome: str | None = None,
        closed_at: datetime | None = None,
        applied_hash: str | None = None,
        approval_channel: str | None = None,
        approval_chat_id: str | None = None,
    ) -> None:
        now = closed_at or datetime.now(UTC)
        self._conn.execute(
            """
            UPDATE persona_evolution_proposals
            SET status = ?,
                closed_at = ?,
                final_outcome = ?,
                applied_hash = COALESCE(?, applied_hash),
                approval_channel = COALESCE(?, approval_channel),
                approval_chat_id = COALESCE(?, approval_chat_id)
            WHERE proposal_id = ?
            """,
            (
                str(status),
                _iso_utc(now),
                final_outcome,
                applied_hash,
                approval_channel,
                approval_chat_id,
                str(proposal_id),
            ),
        )
        self._conn.commit()

    def mark_notified(
        self,
        proposal_id: str,
        *,
        channel: str,
        chat_id: str,
        notified_at: datetime | None = None,
    ) -> None:
        now = notified_at or datetime.now(UTC)
        self._conn.execute(
            """
            UPDATE persona_evolution_proposals
            SET notified_at = ?,
                notification_channel = ?,
                notification_chat_id = ?
            WHERE proposal_id = ?
            """,
            (_iso_utc(now), str(channel), str(chat_id), str(proposal_id)),
        )
        self._conn.commit()

    def expire_pending(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: int = PERSONA_EVOLUTION_PROPOSAL_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        current = _ensure_utc(now or datetime.now(UTC))
        expired: list[dict[str, Any]] = []
        for proposal in self.pending_proposals():
            created_at = _parse_utc_datetime(str(proposal["created_at"]))
            if (current - created_at).total_seconds() <= int(max_age_seconds):
                continue
            self.close_proposal(
                str(proposal["proposal_id"]),
                status="expired",
                final_outcome="expired",
                closed_at=current,
            )
            proposal = dict(proposal)
            proposal["status"] = "expired"
            proposal["closed_at"] = _iso_utc(current)
            proposal["final_outcome"] = "expired"
            expired.append(proposal)
        return expired

    def close(self) -> None:
        self._conn.close()


def build_persona_evolution_approval_message(proposal: dict[str, Any]) -> str:
    """Render the owner review prompt for a pending persona proposal."""
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    persona_file = str(proposal.get("persona_file") or "").strip()
    proposal_path = str(proposal.get("proposal_path") or "").strip()
    total_messages = int(proposal.get("total_message_count") or 0)
    signal_score = float(proposal.get("signal_score") or 0.0)
    evidence_from = str(proposal.get("evidence_from") or "").strip()
    evidence_to = str(proposal.get("evidence_to") or "").strip()
    return "\n".join(
        [
            f"Persona evolution proposal for {persona_file}",
            f"Path: {proposal_path}",
            f"Evidence: {evidence_from} -> {evidence_to}",
            f"Messages: {total_messages}; signal score: {signal_score:.2f}",
            "",
            f"Approve: pe-approve-{proposal_id}",
            f"Deny: pe-deny-{proposal_id}",
        ]
    )


def apply_persona_evolution_proposal(
    *,
    workspace: Path,
    state_db_path: Path,
    proposal_id: str,
    approved_by_channel: str,
    approved_by_chat_id: str,
    now: datetime | None = None,
) -> PersonaEvolutionDecisionResult:
    """Apply one pending proposal to its companion `.evolution.md` file."""
    decided_at = now or datetime.now(UTC)
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    decided_at = decided_at.astimezone(UTC)
    ledger = PersonaEvolutionLedger(state_db_path)
    try:
        proposal = ledger.get_proposal(proposal_id)
        if proposal is None:
            return PersonaEvolutionDecisionResult(
                status="not_found",
                proposal_id=str(proposal_id),
                message="persona evolution proposal not found",
            )
        persona_file = str(proposal["persona_file"])
        if str(proposal["status"]) != "proposed":
            return PersonaEvolutionDecisionResult(
                status="not_proposed",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                message=f"persona evolution proposal is {proposal['status']}",
            )
        if _proposal_expired(proposal, decided_at):
            ledger.close_proposal(
                str(proposal_id),
                status="expired",
                final_outcome="expired",
                closed_at=decided_at,
            )
            return PersonaEvolutionDecisionResult(
                status="expired",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                message="persona evolution proposal expired",
            )

        persona_path = resolve_persona_path(persona_file, workspace)
        expected_persona_hash = str(proposal.get("persona_hash") or "")
        if expected_persona_hash:
            current_persona_hash = _file_sha256(persona_path)
            if expected_persona_hash != current_persona_hash:
                return PersonaEvolutionDecisionResult(
                    status="blocked",
                    proposal_id=str(proposal_id),
                    persona_file=persona_file,
                    evolution_path=persona_path,
                    message="base persona file changed since proposal was created",
                )

        evolution_path = persona_path.parent / f"{persona_path.stem}.evolution{persona_path.suffix}"
        current = evolution_path.read_text(encoding="utf-8")
        section_error = _evolution_section_error(current)
        if section_error:
            return PersonaEvolutionDecisionResult(
                status="blocked",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                evolution_path=evolution_path,
                message=section_error,
            )
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if str(proposal["base_hash"]) != current_hash:
            return PersonaEvolutionDecisionResult(
                status="blocked",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                evolution_path=evolution_path,
                message="evolution file changed since proposal was created",
            )

        proposal_path = Path(str(proposal["proposal_path"]))
        proposal_text = proposal_path.read_text(encoding="utf-8")
        notes = _extract_suggested_evolution_notes(proposal_text)
        if not notes:
            return PersonaEvolutionDecisionResult(
                status="blocked",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                evolution_path=evolution_path,
                message="proposal contains no suggested evolution notes",
            )
        note_error = _proposed_note_error(notes)
        if note_error:
            return PersonaEvolutionDecisionResult(
                status="blocked",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                evolution_path=evolution_path,
                message=note_error,
            )

        count = _consolidation_count(current) + 1
        updated = _set_consolidation_metadata(current, decided_at.date().isoformat(), count)
        updated = _append_consciousness_lessons(updated, notes)
        updated = _append_consolidation_changelog(
            updated,
            date=decided_at.date().isoformat(),
            count=count,
            proposal_id=str(proposal_id),
            total_messages=int(proposal.get("total_message_count") or 0),
            signal_score=float(proposal.get("signal_score") or 0.0),
        )
        evolution_path.write_text(updated, encoding="utf-8")
        applied_hash = hashlib.sha256(updated.encode("utf-8")).hexdigest()
        ledger.close_proposal(
            str(proposal_id),
            status="applied",
            final_outcome="approved",
            closed_at=decided_at,
            applied_hash=applied_hash,
            approval_channel=approved_by_channel,
            approval_chat_id=approved_by_chat_id,
        )
        return PersonaEvolutionDecisionResult(
            status="applied",
            proposal_id=str(proposal_id),
            persona_file=persona_file,
            evolution_path=evolution_path,
            message=f"applied persona evolution proposal {proposal_id}",
        )
    finally:
        ledger.close()


def deny_persona_evolution_proposal(
    *,
    state_db_path: Path,
    proposal_id: str,
    denied_by_channel: str,
    denied_by_chat_id: str,
    now: datetime | None = None,
) -> PersonaEvolutionDecisionResult:
    """Mark one pending proposal as denied without mutating persona files."""
    decided_at = now or datetime.now(UTC)
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    decided_at = decided_at.astimezone(UTC)
    ledger = PersonaEvolutionLedger(state_db_path)
    try:
        proposal = ledger.get_proposal(proposal_id)
        if proposal is None:
            return PersonaEvolutionDecisionResult(
                status="not_found",
                proposal_id=str(proposal_id),
                message="persona evolution proposal not found",
            )
        persona_file = str(proposal["persona_file"])
        if str(proposal["status"]) != "proposed":
            return PersonaEvolutionDecisionResult(
                status="not_proposed",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                message=f"persona evolution proposal is {proposal['status']}",
            )
        if _proposal_expired(proposal, decided_at):
            ledger.close_proposal(
                str(proposal_id),
                status="expired",
                final_outcome="expired",
                closed_at=decided_at,
            )
            return PersonaEvolutionDecisionResult(
                status="expired",
                proposal_id=str(proposal_id),
                persona_file=persona_file,
                message="persona evolution proposal expired",
            )
        ledger.close_proposal(
            str(proposal_id),
            status="denied",
            final_outcome="denied",
            closed_at=decided_at,
            approval_channel=denied_by_channel,
            approval_chat_id=denied_by_chat_id,
        )
        return PersonaEvolutionDecisionResult(
            status="denied",
            proposal_id=str(proposal_id),
            persona_file=persona_file,
            message=f"denied persona evolution proposal {proposal_id}",
        )
    finally:
        ledger.close()


def _evolution_path_for_persona(workspace: Path, persona_file: str) -> Path:
    persona_path = resolve_persona_path(persona_file, workspace)
    return persona_path.parent / f"{persona_path.stem}.evolution{persona_path.suffix}"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _proposal_expired(
    proposal: dict[str, Any],
    now: datetime,
    *,
    max_age_seconds: int = PERSONA_EVOLUTION_PROPOSAL_TTL_SECONDS,
) -> bool:
    created_at = _parse_utc_datetime(str(proposal["created_at"]))
    return (_ensure_utc(now) - created_at).total_seconds() > int(max_age_seconds)


def _evolution_section_error(text: str) -> str | None:
    for raw_line in text.splitlines():
        if not raw_line.startswith("## "):
            continue
        heading = raw_line.removeprefix("## ").strip()
        if heading not in PERSONA_EVOLUTION_ALLOWED_SECTIONS:
            return f"unsupported evolution section: {heading}"
    return None


def _proposed_note_error(notes: list[str]) -> str | None:
    for note in notes:
        normalized = " ".join(note.split())
        if " evidence=" not in normalized:
            return "proposal note missing evidence count"
        if " confidence=" not in normalized:
            return "proposal note missing confidence"
        if not re.match(r"^\d{4}-\d{2}-\d{2}\s+`[^`]+`", normalized):
            return "proposal note missing date and chat scope"
    return None


def _extract_suggested_evolution_notes(proposal_text: str) -> list[str]:
    active_section: str | None = None
    notes: list[str] = []
    for raw_line in proposal_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            heading = line.strip()
            if heading == "## Proposed Change":
                active_section = "proposed"
            elif heading == "## Suggested Evolution Notes":
                active_section = "legacy"
            else:
                active_section = None
            continue
        if active_section is None:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            note = stripped[2:].strip()
            if note:
                if active_section == "legacy":
                    note = _clean_legacy_suggested_note(note)
                notes.append(note)
    return _collapse_notes_by_chat(notes)


def _collapse_notes_by_chat(notes: list[str]) -> list[str]:
    collapsed: list[str] = []
    seen: set[str] = set()
    for note in notes:
        key = _note_chat_key(note)
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(note)
    return collapsed


def _note_chat_key(note: str) -> str:
    match = re.match(r"\d{4}-\d{2}-\d{2}\s+`(?P<chat>[^`]+)`", note)
    if match:
        return match.group("chat").casefold()
    return re.sub(r"\s+", " ", note).strip().casefold()


def _clean_legacy_suggested_note(note: str) -> str:
    legacy = re.match(
        r"(?P<prefix>\d{4}-\d{2}-\d{2}\s+`[^`]+`):\s+"
        r"Consider adding a Consciousness Outcome Lesson from "
        r"(?P<evidence>.+?) and learned taste:\s+(?P<lesson>.+)$",
        note,
    )
    if legacy:
        lesson = _clean_taste_pattern(legacy.group("lesson"))
        return (
            f"{legacy.group('prefix')} confidence=medium "
            f"evidence={legacy.group('evidence')}: {lesson}"
        )
    return note.replace("Consider adding a Consciousness Outcome Lesson: ", "").strip()


def _consolidation_count(text: str) -> int:
    match = re.search(r"Consolidation count:\s*(\d+)", text)
    return int(match.group(1)) if match else 0


def _set_consolidation_metadata(text: str, date: str, count: int) -> str:
    updated = re.sub(
        r"<!--\s*Last consolidated:\s*.*?-->",
        f"<!-- Last consolidated: {date} -->",
        text,
        count=1,
    )
    if updated == text:
        updated = f"<!-- Last consolidated: {date} -->\n{updated}"
    count_line = f"<!-- Consolidation count: {count} -->"
    replaced = re.sub(
        r"<!--\s*Consolidation count:\s*.*?-->",
        count_line,
        updated,
        count=1,
    )
    if replaced == updated:
        lines = replaced.splitlines()
        insert_at = 1 if lines else 0
        lines.insert(insert_at, count_line)
        replaced = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return replaced


def _append_consciousness_lessons(text: str, notes: list[str]) -> str:
    rendered = "\n".join(f"- {note}" for note in notes)
    return _append_to_section(
        text,
        heading="## Consciousness Outcome Lessons",
        content=rendered,
        insert_before="## Consolidation Changelog",
    )


def _append_consolidation_changelog(
    text: str,
    *,
    date: str,
    count: int,
    proposal_id: str,
    total_messages: int,
    signal_score: float,
) -> str:
    entry = (
        f"{date} — consolidation #{count}: applied proposal {proposal_id} "
        f"from {total_messages} messages (score {signal_score:.2f})."
    )
    return _append_to_section(
        text,
        heading="## Consolidation Changelog",
        content=entry,
    )


def _append_to_section(
    text: str,
    *,
    heading: str,
    content: str,
    insert_before: str | None = None,
) -> str:
    content = content.strip()
    if not content:
        return text
    lines = text.splitlines()
    try:
        heading_index = lines.index(heading)
    except ValueError:
        heading_index = -1

    if heading_index < 0:
        insert_at = len(lines)
        if insert_before and insert_before in lines:
            insert_at = lines.index(insert_before)
        section = ["", heading, content, ""]
        lines[insert_at:insert_at] = section
        return "\n".join(lines).rstrip() + "\n"

    insert_at = len(lines)
    for index in range(heading_index + 1, len(lines)):
        if lines[index].startswith("## "):
            insert_at = index
            break
    if content in "\n".join(lines[heading_index:insert_at]):
        return text
    if insert_at > 0 and lines[insert_at - 1].strip():
        lines.insert(insert_at, "")
        insert_at += 1
    lines.insert(insert_at, content)
    return "\n".join(lines).rstrip() + "\n"


def chats_for_persona(policy: PolicyConfig, persona_file: str) -> list[PersonaChatRef]:
    """Return explicit policy chats whose effective persona matches persona_file."""
    target = persona_file.strip()
    refs: list[PersonaChatRef] = []
    for channel, channel_policy in sorted(policy.channels.items()):
        channel_default = channel_policy.default.persona_file
        for chat_id, override in sorted(channel_policy.chats.items()):
            effective = override.persona_file or channel_default or policy.defaults.persona_file
            if effective == target:
                source = "chat" if override.persona_file else "channel_or_global_default"
                refs.append(
                    PersonaChatRef(
                        channel=channel,
                        chat_id=chat_id,
                        persona_file=effective,
                        source=source,
                    )
                )
    return refs


async def collect_persona_evolution_evidence(
    *,
    policy: PolicyConfig,
    workspace: Path,
    persona_file: str,
    memory: MemoryService,
    speakup_log: SpeakupLog,
    inbound_archive: InboundArchive,
    window_days: int = 14,
    per_chat_limit: int = 20,
    since: datetime | None = None,
    now: datetime | None = None,
) -> PersonaEvolutionEvidence:
    collected_at = now or datetime.now(UTC)
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    collected_at = collected_at.astimezone(UTC)
    persona_path = resolve_persona_path(persona_file, workspace)
    evolution_path = persona_path.parent / f"{persona_path.stem}.evolution{persona_path.suffix}"
    current_evolution_text = (
        evolution_path.read_text(encoding="utf-8") if evolution_path.is_file() else ""
    )
    if since is None:
        effective_since = collected_at - timedelta(days=max(1, int(window_days)))
    else:
        effective_since = since if since.tzinfo else since.replace(tzinfo=UTC)
        effective_since = effective_since.astimezone(UTC)

    chats: list[ChatEvolutionEvidence] = []
    for chat in chats_for_persona(policy, persona_file):
        taste_hits = memory.learned_chat_taste(
            channel=chat.channel,
            chat_id=chat.chat_id,
            limit=per_chat_limit,
        )
        preference_hits = memory.recent_chat_preferences(
            channel=chat.channel,
            chat_id=chat.chat_id,
            limit=per_chat_limit,
        )
        speakups = await speakup_log.history(
            chat.channel,
            chat.chat_id,
            limit=per_chat_limit,
        )
        outcome_counts: dict[str, int] = {}
        for row in speakups:
            key = str(row.get("outcome") or row.get("status") or "unknown")
            outcome_counts[key] = outcome_counts.get(key, 0) + 1
        messages = inbound_archive.lookup_messages_in_range(
            chat.channel,
            chat.chat_id,
            effective_since,
            collected_at,
            limit=per_chat_limit,
            latest=True,
        )
        chats.append(
            ChatEvolutionEvidence(
                chat=chat,
                learned_taste=[hit.entry.content for hit in taste_hits],
                recent_preferences=[hit.entry.content for hit in preference_hits],
                speakup_outcomes=outcome_counts,
                speakups=[_safe_speakup(row) for row in speakups],
                recent_message_count=len(messages),
            )
        )

    return PersonaEvolutionEvidence(
        persona_file=persona_file,
        persona_path=persona_path,
        evolution_path=evolution_path,
        collected_at=collected_at,
        evidence_since=effective_since,
        window_days=max(1, int(window_days)),
        chats=chats,
        current_evolution_text=current_evolution_text,
    )


def render_persona_evolution_proposal(
    evidence: PersonaEvolutionEvidence,
    *,
    proposal_id: str | None = None,
) -> str:
    """Render a private, owner-reviewable evolution proposal report."""
    active_chats = _active_evidence_chats(evidence)
    proposed_changes = _proposed_change_notes(evidence)
    lines = [
        "# Persona Evolution Proposal",
        "",
        f"proposal_id: `{proposal_id or '-'}`",
        f"persona_file: `{evidence.persona_file}`",
        f"persona_path: `{evidence.persona_path}`",
        f"evolution_path: `{evidence.evolution_path}`",
        f"evidence_from: `{evidence.evidence_since.isoformat()}`",
        f"evidence_to: `{evidence.collected_at.isoformat()}`",
        f"collected_at: `{evidence.collected_at.isoformat()}`",
        f"window_days: `{evidence.window_days}`",
        f"total_message_count: `{evidence.total_message_count}`",
        "",
        "## Safety",
        "",
        "- This is a proposal only; no persona files were modified.",
        "- Base persona invariants must take precedence over any suggested evolution.",
        "- Raw chat messages are not included.",
        "",
        "## Proposed Change",
        "",
    ]
    if proposed_changes:
        lines.extend(f"- {note}" for note in proposed_changes)
    else:
        lines.append("No durable persona evolution suggested from the available evidence.")
    lines.extend(
        [
            "",
            "## Evidence Digest",
            "",
            f"- active_chats: `{len(active_chats)}`",
            f"- quiet_chats_omitted: `{len(evidence.chats) - len(active_chats)}`",
        ]
    )
    if not evidence.chats:
        lines.append("- policy_chats: `0`")
    for chat_evidence in active_chats:
        chat = chat_evidence.chat
        sent = sum(chat_evidence.speakup_outcomes.values())
        unique_patterns = _unique_taste_patterns(chat_evidence)
        lines.extend(
            [
                (
                    f"- `{chat.channel}:{chat.chat_id}`: policy_source=`{chat.source}`, "
                    f"messages=`{chat_evidence.recent_message_count}`, "
                    f"speakups=`{sent}`, "
                    f"taste_patterns=`{len(unique_patterns)}`, "
                    f"outcomes=`{json.dumps(chat_evidence.speakup_outcomes, sort_keys=True)}`"
                ),
            ]
        )

    lines.extend(
        [
            "",
            "## Current Evolution Digest",
            "",
            *_current_evolution_digest(evidence),
        ]
    )
    return "\n".join(lines)


def _active_evidence_chats(evidence: PersonaEvolutionEvidence) -> list[ChatEvolutionEvidence]:
    return [
        chat
        for chat in evidence.chats
        if chat.recent_message_count > 0
        or bool(chat.speakup_outcomes)
        or bool(chat.learned_taste)
        or bool(chat.recent_preferences)
    ]


def _proposed_change_notes(evidence: PersonaEvolutionEvidence) -> list[str]:
    notes: list[str] = []
    for chat_evidence in _active_evidence_chats(evidence):
        sent = sum(chat_evidence.speakup_outcomes.values())
        patterns = _unique_taste_patterns(chat_evidence)
        if not patterns:
            continue
        chat = chat_evidence.chat
        evidence_parts: list[str] = []
        if sent:
            evidence_parts.append(f"{sent} speakups")
        if chat_evidence.recent_message_count:
            evidence_parts.append(f"{chat_evidence.recent_message_count} messages")
        evidence_text = ", ".join(evidence_parts) if evidence_parts else "learned taste"
        notes.append(
            f"{evidence.collected_at.date()} `{chat.channel}:{chat.chat_id}` "
            f"confidence=medium evidence={evidence_text}: {patterns[0]}"
        )
    return notes


def _unique_taste_patterns(chat_evidence: ChatEvolutionEvidence) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in [*chat_evidence.learned_taste, *chat_evidence.recent_preferences]:
        pattern = _clean_taste_pattern(raw)
        key = re.sub(r"\s+", " ", pattern).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(pattern)
    return unique


def _clean_taste_pattern(value: str) -> str:
    cleaned = str(value).strip()
    prefix = "Proactive speakup taste pattern:"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :].strip()
    return cleaned


def _current_evolution_digest(evidence: PersonaEvolutionEvidence) -> list[str]:
    text = evidence.current_evolution_text
    if not text.strip():
        return ["- current_file: `(missing)`"]
    digest = [
        f"- sha256: `{hashlib.sha256(text.encode('utf-8')).hexdigest()}`",
        f"- bytes: `{len(text.encode('utf-8'))}`",
    ]
    last = re.search(r"<!--\s*Last consolidated:\s*(.*?)\s*-->", text)
    if last:
        digest.append(f"- last_consolidated: `{last.group(1).strip()}`")
    count = re.search(r"<!--\s*Consolidation count:\s*(.*?)\s*-->", text)
    if count:
        digest.append(f"- consolidation_count: `{count.group(1).strip()}`")
    lesson_count = _section_bullet_count(text, "## Consciousness Outcome Lessons")
    if lesson_count:
        digest.append(f"- consciousness_outcome_lessons: `{lesson_count}`")
    return digest


def _section_bullet_count(text: str, heading: str) -> int:
    lines = text.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return 0
    count = 0
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.strip().startswith("- "):
            count += 1
    return count


async def run_persona_evolution_cron(
    *,
    policy: PolicyConfig,
    workspace: Path,
    persona_file: str,
    memory: MemoryService,
    speakup_log: SpeakupLog,
    inbound_archive: InboundArchive,
    window_days: int = 1,
    limit: int = 20,
    output_path: Path | None = None,
    state_db_path: Path | None = None,
    min_meaningful_messages: int = 10,
    min_signal_score: float = 3.0,
    max_accumulation_days: int = 14,
    proposal_ttl_seconds: int = PERSONA_EVOLUTION_PROPOSAL_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """Run typed persona-evolution cron and write an owner-reviewable proposal."""
    collected_at = now or datetime.now(UTC)
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    collected_at = collected_at.astimezone(UTC)

    ledger_path = state_db_path or workspace / "persona-evolution" / "persona-evolution.db"
    ledger = PersonaEvolutionLedger(ledger_path)
    try:
        ledger.expire_pending(now=collected_at, max_age_seconds=proposal_ttl_seconds)
        pending = ledger.pending_proposal(persona_file)
        if pending is not None:
            return f"persona_evolution no proposal: pending_proposal proposal_id={pending['proposal_id']}"

        since = collected_at - timedelta(days=max(1, int(max_accumulation_days)))
        watermark = ledger.latest_closed_watermark(persona_file)
        if watermark is not None and watermark > since:
            since = watermark

        evidence = await collect_persona_evolution_evidence(
            policy=policy,
            workspace=workspace,
            persona_file=persona_file,
            memory=memory,
            speakup_log=speakup_log,
            inbound_archive=inbound_archive,
            window_days=window_days,
            per_chat_limit=limit,
            since=since,
            now=collected_at,
        )
        total_messages = evidence.total_message_count
        signal_score = persona_evolution_signal_score(evidence)
        if total_messages < int(min_meaningful_messages) or signal_score < float(min_signal_score):
            ledger.record_scan(
                persona_file=persona_file,
                scanned_at=collected_at,
                evidence_from=evidence.evidence_since,
                evidence_to=evidence.collected_at,
                total_message_count=total_messages,
                signal_score=signal_score,
                result="no_proposal",
                reason="below_threshold",
            )
            return (
                "persona_evolution no proposal: below_threshold "
                f"messages={total_messages} score={signal_score:.2f}"
            )

        proposed_changes = _proposed_change_notes(evidence)
        if not proposed_changes:
            ledger.record_scan(
                persona_file=persona_file,
                scanned_at=collected_at,
                evidence_from=evidence.evidence_since,
                evidence_to=evidence.collected_at,
                total_message_count=total_messages,
                signal_score=signal_score,
                result="no_proposal",
                reason="no_durable_changes",
            )
            return (
                "persona_evolution no proposal: no_durable_changes "
                f"messages={total_messages} score={signal_score:.2f}"
            )

        proposal_id = uuid.uuid4().hex
        rendered = render_persona_evolution_proposal(evidence, proposal_id=proposal_id)
        target, output_error = _resolve_private_output_path(
            workspace=workspace,
            persona_file=persona_file,
            collected_at=evidence.collected_at,
            output_path=output_path,
        )
        if output_error:
            ledger.record_scan(
                persona_file=persona_file,
                scanned_at=collected_at,
                evidence_from=evidence.evidence_since,
                evidence_to=evidence.collected_at,
                total_message_count=total_messages,
                signal_score=signal_score,
                result="no_proposal",
                reason="invalid_output_path",
            )
            return f"persona_evolution no proposal: invalid_output_path {output_error}"
        ensure_dir(target.parent)
        target.write_text(rendered, encoding="utf-8")
        ledger.record_proposal(
            proposal_id=proposal_id,
            persona_file=persona_file,
            proposal_path=target,
            created_at=collected_at,
            evidence_from=evidence.evidence_since,
            evidence_to=evidence.collected_at,
            total_message_count=total_messages,
            signal_score=signal_score,
            base_hash=hashlib.sha256(
                evidence.current_evolution_text.encode("utf-8")
            ).hexdigest(),
            persona_hash=_file_sha256(evidence.persona_path),
        )
        ledger.record_scan(
            persona_file=persona_file,
            scanned_at=collected_at,
            evidence_from=evidence.evidence_since,
            evidence_to=evidence.collected_at,
            total_message_count=total_messages,
            signal_score=signal_score,
            result="proposal",
            proposal_id=proposal_id,
        )
        return f"persona_evolution proposal written: {target}"
    finally:
        ledger.close()


def persona_evolution_signal_score(evidence: PersonaEvolutionEvidence) -> float:
    """Score whether evidence is strong enough to deserve a review proposal."""
    score = 0.0
    for chat_evidence in evidence.chats:
        score += chat_evidence.recent_message_count * 0.25
        score += len(chat_evidence.learned_taste) * 3.0
        score += len(chat_evidence.recent_preferences) * 2.0
        for outcome, count in chat_evidence.speakup_outcomes.items():
            weight = 2.0 if str(outcome) in {"replied", "approved", "denied"} else 1.0
            score += max(0, int(count)) * weight
    return score


async def build_persona_evolution_status(
    *,
    policy: PolicyConfig,
    workspace: Path,
    memory: MemoryService,
    speakup_log: SpeakupLog,
    state_db_path: Path,
    persona_file: str,
    channel: str | None = None,
    chat_id: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Build a compact operator summary for persona evolution learning state."""
    ledger = PersonaEvolutionLedger(state_db_path)
    try:
        pending = ledger.pending_proposals()
        scans = ledger.latest_scans(persona_file=persona_file, limit=limit)
        proposal_counts = ledger.proposal_counts()
        scan_counts = ledger.scan_counts()
    finally:
        ledger.close()

    status: dict[str, Any] = {
        "persona_file": persona_file,
        "persona_path": str(resolve_persona_path(persona_file, workspace)),
        "pending_proposals": [_compact_proposal(row) for row in pending],
        "latest_scans": [_compact_scan(row) for row in scans],
        "metrics": {
            "proposals": proposal_counts,
            "scans": scan_counts,
        },
        "policy_chats": [
            {
                "channel": ref.channel,
                "chat_id": ref.chat_id,
                "source": ref.source,
            }
            for ref in chats_for_persona(policy, persona_file)
        ],
    }
    if channel and chat_id:
        summary = await speakup_log.learning_summary(channel=channel, chat_id=chat_id)
        taste = memory.learned_chat_taste(channel=channel, chat_id=chat_id, limit=1)
        status["chat"] = {
            **summary,
            "channel": channel,
            "chat_id": chat_id,
            "last_learned_taste": taste[0].entry.content if taste else None,
        }
    return status


def _compact_proposal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": row.get("proposal_id"),
        "persona_file": row.get("persona_file"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "closed_at": row.get("closed_at"),
        "final_outcome": row.get("final_outcome"),
        "proposal_path": row.get("proposal_path"),
        "total_message_count": row.get("total_message_count"),
        "signal_score": row.get("signal_score"),
    }


def _compact_scan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scanned_at": row.get("scanned_at"),
        "evidence_from": row.get("evidence_from"),
        "evidence_to": row.get("evidence_to"),
        "total_message_count": row.get("total_message_count"),
        "signal_score": row.get("signal_score"),
        "result": row.get("result"),
        "reason": row.get("reason"),
        "proposal_id": row.get("proposal_id"),
    }


def _resolve_private_output_path(
    *,
    workspace: Path,
    persona_file: str,
    collected_at: datetime,
    output_path: Path | None,
) -> tuple[Path, str | None]:
    root = (workspace / "persona-evolution").resolve()
    target = output_path or _default_cron_output_path(workspace, persona_file, collected_at)
    if not target.is_absolute():
        target = workspace / target
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved, f"path must be under {root}"
    return resolved, None


def _default_cron_output_path(workspace: Path, persona_file: str, collected_at: datetime) -> Path:
    stamp = collected_at.strftime("%Y%m%dT%H%M%SZ")
    name = safe_filename(persona_file.replace("/", "-").replace(".", "-"))
    return workspace / "persona-evolution" / f"{stamp}-{name}-proposal.md"


def _ensure_utc(value: datetime) -> datetime:
    current = value if value.tzinfo else value.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat()


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _suggested_notes(evidence: PersonaEvolutionEvidence) -> str:
    notes = _proposed_change_notes(evidence)
    if not notes:
        return "No durable persona evolution suggested from the available evidence."
    return "\n".join(f"- {note}" for note in notes)


def _safe_speakup(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": row.get("created_at"),
        "committed_at": row.get("committed_at"),
        "channel": row.get("channel"),
        "chat_id": row.get("chat_id"),
        "action_type": row.get("action_type"),
        "profile": row.get("profile"),
        "status": row.get("status"),
        "outcome": row.get("outcome"),
    }
