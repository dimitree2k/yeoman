"""Person notes: storage, source join and the ``person_note`` job rows.

A note is derived context about one person in one group.  It follows the digest
contract:

* rebuilding never rewrites history: a new note of the same person supersedes the
  previous active one, which stays readable for audit;
* the source join lists every Knowledge statement and history message the note rests
  on;
* a deleted/edited message or a revoked/corrected statement marks the note stale
  *before* the next read (the synchronous invalidation paths call
  :meth:`PersonNoteStore.mark_stale`), and every read re-checks the sources it has
  (the ledger, statement lifecycle, or the caller's ``source_present`` callback);
* the staleness is persisted, never silently recomputed away.

The job reuses the existing ``knowledge_jobs`` state machine with ``kind =
'person_note'``.  The job id includes the input hash, so a rebuild is a new row while
an unchanged input is never queued twice.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from yeoman_gateway.knowledge.models import (
    PERSON_NOTE_FAMILIES,
    PERSON_NOTE_JOB_KIND,
    PERSON_NOTE_LINE_KINDS,
    PERSON_NOTE_SOURCE_KINDS,
    PersonNoteJob,
    PersonNoteLine,
    PersonNoteSeed,
    PersonNoteView,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.knowledge._store import KnowledgeStore

#: Version recorded in ``knowledge_jobs.extractor_version`` for person-note jobs.
JOB_VERSION = "person-note-job-v1"


def scope_key_for(channel: str, chat_id: str) -> str:
    return f"channel:{channel}:chat:{chat_id}"


def job_id_for(channel: str, chat_id: str, person_id: str, input_hash: str) -> str:
    """The dedupe key: one job per chat, person and input."""
    return f"{PERSON_NOTE_JOB_KIND}:{channel}:{chat_id}:{person_id}:{input_hash}"


def _parse_job_id(job_id: str) -> tuple[str, str, str, str] | None:
    prefix, _sep, rest = job_id.partition(":")
    if prefix != PERSON_NOTE_JOB_KIND:
        return None
    _channel, _sep, rest = rest.partition(":")
    chat_id, _sep, rest = rest.partition(":")
    person_id, _sep, input_hash = rest.rpartition(":")
    if not chat_id or not person_id or not input_hash:
        return None
    channel = job_id.split(":", 2)[1]
    return channel, chat_id, person_id, input_hash


def _job(row: Mapping[str, Any], *, created: bool = False) -> PersonNoteJob:
    parsed = _parse_job_id(str(row["job_id"]))
    if parsed is None:  # pragma: no cover - only our own rows use this kind
        raise ValidationError("malformed person note job id")
    channel, chat_id, person_id, input_hash = parsed
    return PersonNoteJob(
        job_id=str(row["job_id"]),
        channel=channel,
        chat_id=chat_id,
        person_id=person_id,
        input_hash=input_hash,
        state=str(row["state"]),
        reason=str(row["reason"] or ""),
        attempts=int(row["attempts"] or 0),
        due_ms=int(row["due_ms"] or 0),
        created_ms=int(row["created_ms"] or 0),
        updated_ms=int(row["updated_ms"] or 0),
        created=created,
    )


class PersonNoteStore:
    """Note rows, source joins, budget counters and jobs in the Knowledge owner."""

    def __init__(self, store: "KnowledgeStore", *, workspace_id: str) -> None:
        self._store = store
        self.workspace_id = str(workspace_id)
        store.ensure_person_note_schema()

    # ── jobs ─────────────────────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        channel: str,
        chat_id: str,
        person_id: str,
        input_hash: str,
        now_ms: int,
        due_ms: int | None = None,
    ) -> PersonNoteJob:
        """Queue one input unless its job already exists."""
        job_id = job_id_for(channel, chat_id, person_id, input_hash)
        with self._store.transaction():
            cursor = self._store.execute(
                "INSERT OR IGNORE INTO knowledge_jobs (job_id, workspace_id, scope_key, kind,"
                " sources_json, extractor_version, state, reason, attempts, due_ms, created_ms,"
                " updated_ms) VALUES (?, ?, ?, ?, '[]', ?, 'queued', NULL, 0, ?, ?, ?)",
                (
                    job_id,
                    self.workspace_id,
                    scope_key_for(channel, chat_id),
                    PERSON_NOTE_JOB_KIND,
                    JOB_VERSION,
                    int(due_ms if due_ms is not None else now_ms),
                    int(now_ms),
                    int(now_ms),
                ),
            )
            created = cursor.rowcount == 1
        row = self._job_row(job_id)
        assert row is not None
        return _job(row, created=created)

    def _job_row(self, job_id: str) -> Any:
        return self._store.query_one(
            "SELECT * FROM knowledge_jobs WHERE job_id = ? AND kind = ?",
            (str(job_id), PERSON_NOTE_JOB_KIND),
        )

    def job(self, job_id: str) -> PersonNoteJob | None:
        row = self._job_row(job_id)
        return None if row is None else _job(row)

    def jobs(
        self, *, channel: str = "whatsapp", chat_id: str | None = None, limit: int = 50
    ) -> tuple[PersonNoteJob, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            + (" AND scope_key = ?" if chat_id else "")
            + " ORDER BY created_ms DESC, job_id DESC LIMIT ?",
            (PERSON_NOTE_JOB_KIND, self.workspace_id)
            + ((scope_key_for(channel, chat_id),) if chat_id else ())
            + (int(limit),),
        )
        return tuple(_job(row) for row in rows)

    def due(self, *, now_ms: int, max_attempts: int, limit: int = 8) -> tuple[PersonNoteJob, ...]:
        """Queued, failed-with-attempts-left, and budget-deferred jobs whose time came."""
        rows = self._store.query(
            "SELECT * FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            " AND (state = 'queued' OR (state = 'failed' AND attempts < ?)"
            " OR (state = 'skipped' AND reason = 'skipped_budget'))"
            " AND due_ms <= ? ORDER BY due_ms, job_id LIMIT ?",
            (PERSON_NOTE_JOB_KIND, self.workspace_id, int(max_attempts), int(now_ms), int(limit)),
        )
        return tuple(_job(row) for row in rows)

    def claim(self, job_id: str, *, now_ms: int, max_attempts: int) -> PersonNoteJob | None:
        """Move one due job to ``running`` and count the attempt; ``None`` if lost."""
        with self._store.transaction():
            cursor = self._store.execute(
                "UPDATE knowledge_jobs SET state = 'running', reason = 'running',"
                " attempts = attempts + 1, updated_ms = ?"
                " WHERE job_id = ? AND kind = ? AND due_ms <= ?"
                " AND (state = 'queued' OR (state = 'failed' AND attempts < ?)"
                " OR (state = 'skipped' AND reason = 'skipped_budget'))",
                (
                    int(now_ms),
                    str(job_id),
                    PERSON_NOTE_JOB_KIND,
                    int(now_ms),
                    int(max_attempts),
                ),
            )
            claimed = cursor.rowcount == 1
        return self.job(job_id) if claimed else None

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        reason: str = "",
        now_ms: int,
        due_ms: int | None = None,
        refund_attempt: bool = False,
    ) -> PersonNoteJob | None:
        if state not in ("queued", "done", "skipped", "failed", "cancelled"):
            raise ValidationError(f"unknown person note job state: {state!r}")
        with self._store.transaction():
            self._store.execute(
                "UPDATE knowledge_jobs SET state = ?, reason = ?, updated_ms = ?,"
                " due_ms = COALESCE(?, due_ms),"
                " attempts = MAX(0, attempts - ?) WHERE job_id = ? AND kind = ?",
                (
                    str(state),
                    str(reason or ""),
                    int(now_ms),
                    None if due_ms is None else int(due_ms),
                    1 if refund_attempt else 0,
                    str(job_id),
                    PERSON_NOTE_JOB_KIND,
                ),
            )
        return self.job(job_id)

    def recover_stale(self, *, updated_before_ms: int, now_ms: int) -> tuple[str, ...]:
        """Jobs a crash left ``running`` become a failed attempt, retried while allowed."""
        rows = self._store.query(
            "SELECT job_id FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            " AND state = 'running' AND updated_ms <= ? ORDER BY job_id",
            (PERSON_NOTE_JOB_KIND, self.workspace_id, int(updated_before_ms)),
        )
        recovered = tuple(str(row["job_id"]) for row in rows)
        for job_id in recovered:
            self.finish(job_id, state="failed", reason="interrupted", now_ms=now_ms, due_ms=now_ms)
        return recovered

    # ── notes ────────────────────────────────────────────────────────────────

    def store_note(self, seed: PersonNoteSeed, *, now_ms: int | None = None) -> PersonNoteView:
        """Insert a new version; the previous active note becomes ``superseded``."""
        lines = tuple(seed.lines)
        family = str(seed.family or "profile")
        if family not in PERSON_NOTE_FAMILIES:
            raise ValidationError(f"unknown person note family: {family!r}")
        if any(line.kind not in PERSON_NOTE_LINE_KINDS for line in lines):
            raise ValidationError("unknown person note line kind")
        if any(not line.text.strip() for line in lines):
            raise ValidationError("empty person note line")
        sources = tuple(
            (str(kind), str(ref), int(revision)) for kind, ref, revision in seed.sources if str(ref)
        )
        if any(kind not in PERSON_NOTE_SOURCE_KINDS for kind, _ref, _revision in sources):
            raise ValidationError("unknown person note source kind")
        if not str(seed.input_hash or ""):
            raise ValidationError("person note input_hash is required")
        note_id = self._store.new_id()
        built = int(self._store.now_ms() if now_ms is None else now_ms)
        with self._store.transaction():
            latest = self._store.query_one(
                "SELECT note_id FROM knowledge_person_notes"
                " WHERE workspace_id = ? AND channel = ? AND chat_id = ? AND person_id = ?"
                " AND family = ? AND status = 'active'"
                " ORDER BY built_ms DESC, note_id DESC LIMIT 1",
                (
                    self.workspace_id, str(seed.channel), str(seed.chat_id),
                    str(seed.person_id), family,
                ),
            )
            self._store.execute(
                "INSERT INTO knowledge_person_notes (note_id, workspace_id, channel, chat_id,"
                " person_id, family, status, input_hash, built_ms, input_cut_ms, lines_json,"
                " superseded_by, stale_reason) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?,"
                " NULL, NULL)",
                (
                    note_id,
                    self.workspace_id,
                    str(seed.channel),
                    str(seed.chat_id),
                    str(seed.person_id),
                    family,
                    str(seed.input_hash),
                    built,
                    int(seed.input_cut_ms),
                    json.dumps(
                        [
                            {
                                "kind": line.kind,
                                "text": line.text,
                                "author": line.author,
                                "refs": list(line.refs),
                            }
                            for line in lines
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            for kind, ref, revision in dict.fromkeys(sources):
                self._store.execute(
                    "INSERT OR IGNORE INTO knowledge_person_note_sources"
                    " (note_id, kind, ref, revision) VALUES (?, ?, ?, ?)",
                    (note_id, kind, ref, revision),
                )
            if latest is not None:
                # The self-reference is valid only after the new row exists.
                self._store.execute(
                    "UPDATE knowledge_person_notes SET status = 'superseded', superseded_by = ?"
                    " WHERE note_id = ? AND status = 'active'",
                    (note_id, str(latest["note_id"])),
                )
        view = self._view(self._note_row(note_id))
        assert view is not None
        return view

    def has_hash(
        self, *, channel: str, chat_id: str, person_id: str, input_hash: str,
        family: str = "profile",
    ) -> bool:
        return (
            self._store.query_one(
                "SELECT 1 FROM knowledge_person_notes WHERE workspace_id = ? AND channel = ?"
                " AND chat_id = ? AND person_id = ? AND family = ? AND input_hash = ? LIMIT 1",
                (
                    self.workspace_id, str(channel), str(chat_id), str(person_id),
                    str(family), str(input_hash),
                ),
            )
            is not None
        )

    def latest(
        self, *, channel: str, chat_id: str, person_id: str, family: str = "profile"
    ) -> PersonNoteView | None:
        row = self._store.query_one(
            "SELECT * FROM knowledge_person_notes WHERE workspace_id = ? AND channel = ?"
            " AND chat_id = ? AND person_id = ? AND family = ?"
            " ORDER BY built_ms DESC, note_id DESC LIMIT 1",
            (self.workspace_id, str(channel), str(chat_id), str(person_id), str(family)),
        )
        return self._view(row)

    def notes(
        self,
        *,
        channel: str,
        chat_id: str,
        person_ids: Iterable[str] = (),
        source_present: Callable[[str], bool] | None = None,
        include_superseded: bool = False,
        limit: int = 100,
        family: str = "profile",
    ) -> tuple[PersonNoteView, ...]:
        """Notes for one chat, newest first; an active one is re-checked before it renders."""
        wanted = tuple(dict.fromkeys(str(item) for item in person_ids if str(item)))
        rows = self._store.query(
            "SELECT * FROM knowledge_person_notes WHERE workspace_id = ? AND channel = ?"
            " AND chat_id = ? AND family = ?"
            + (" AND person_id IN (" + ",".join("?" for _ in wanted) + ")" if wanted else "")
            + ("" if include_superseded else " AND status = 'active'")
            + " ORDER BY built_ms DESC, note_id DESC LIMIT ?",
            (self.workspace_id, str(channel), str(chat_id), str(family), *wanted, int(limit)),
        )
        views: list[PersonNoteView] = []
        for row in rows:
            if str(row["status"]) == "active" and self._is_stale(
                str(row["note_id"]), source_present
            ):
                self._mark_stale(str(row["note_id"]), reason="stale_source")
                row = self._note_row(str(row["note_id"]))
            view = self._view(row)
            if view is not None:
                views.append(view)
        return tuple(views)

    def _note_row(self, note_id: str) -> Any:
        return self._store.query_one(
            "SELECT * FROM knowledge_person_notes WHERE note_id = ? AND workspace_id = ?",
            (str(note_id), self.workspace_id),
        )

    def _sources_of(self, note_id: str) -> tuple[tuple[str, str, int], ...]:
        rows = self._store.query(
            "SELECT kind, ref, revision FROM knowledge_person_note_sources WHERE note_id = ?"
            " ORDER BY kind, ref",
            (str(note_id),),
        )
        return tuple((str(row["kind"]), str(row["ref"]), int(row["revision"])) for row in rows)

    def _view(self, row: Any) -> PersonNoteView | None:
        if row is None:
            return None
        status = str(row["status"])
        reason = {"superseded": "superseded", "stale": "stale_source"}.get(status, "ok")
        try:
            payload = json.loads(str(row["lines_json"] or "[]"))
        except ValueError:  # pragma: no cover - a corrupt row is not a reader error
            payload = []
        lines = tuple(
            PersonNoteLine(
                kind=str(item.get("kind") or ""),
                text=str(item.get("text") or ""),
                author=item.get("author"),
                refs=tuple(str(ref) for ref in item.get("refs") or ()),
            )
            for item in payload
            if isinstance(item, dict)
        )
        return PersonNoteView(
            note_id=str(row["note_id"]),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            person_id=str(row["person_id"]),
            status=status,
            input_hash=str(row["input_hash"]),
            built_ms=int(row["built_ms"]),
            input_cut_ms=int(row["input_cut_ms"]),
            lines=() if status == "stale" else lines,
            superseded_by=(None if row["superseded_by"] is None else str(row["superseded_by"])),
            stale_reason=str(row["stale_reason"] or ""),
            sources=self._sources_of(str(row["note_id"])),
            reason=reason,
            family=str(row["family"] or "profile"),
        )

    def _is_stale(self, note_id: str, source_present: Callable[[str], bool] | None) -> bool:
        """A deleted message or a revoked/corrected statement invalidates the note."""
        ledger = self._store.has_table("knowledge_history_sources")
        for kind, ref, revision in self._sources_of(note_id):
            if kind == "message":
                if ledger:
                    row = self._store.query_one(
                        "SELECT revoked FROM knowledge_history_sources WHERE event_id = ?"
                        " ORDER BY revision DESC LIMIT 1",
                        (ref,),
                    )
                    if row is not None and int(row["revoked"] or 0):
                        return True
                if source_present is not None and not source_present(ref):
                    return True
            elif kind == "statement":
                row = self._store.query_one(
                    "SELECT status, revoked_at_ms FROM knowledge_statements WHERE statement_id = ?",
                    (ref,),
                )
                if row is None or str(row["status"]) not in ("assertion", "confirmed"):
                    return True
                if row["revoked_at_ms"] is not None:
                    return True
        return False

    def _mark_stale(self, note_id: str, *, reason: str) -> bool:
        with self._store.transaction():
            cursor = self._store.execute(
                "UPDATE knowledge_person_notes SET status = 'stale', stale_reason = ?"
                " WHERE note_id = ? AND status = 'active'",
                (str(reason or "stale_source"), str(note_id)),
            )
        return cursor.rowcount == 1

    def mark_stale(
        self, sources: Iterable[tuple[str, str]], *, reason: str = "stale_source"
    ) -> int:
        """Mark every active note that depends on one of these sources stale."""
        pairs = tuple(
            dict.fromkeys(
                (str(kind), str(ref))
                for kind, ref in sources
                if str(ref) and str(kind) in PERSON_NOTE_SOURCE_KINDS
            )
        )
        if not pairs:
            return 0
        changed = 0
        with self._store.transaction():
            for kind, ref in pairs:
                rows = self._store.query(
                    "SELECT DISTINCT note_id FROM knowledge_person_note_sources"
                    " WHERE kind = ? AND ref = ?",
                    (kind, ref),
                )
                for row in rows:
                    changed += 1 if self._mark_stale(str(row["note_id"]), reason=reason) else 0
        return changed

    def purge(
        self,
        *,
        channel: str,
        chat_id: str | None = None,
        person_id: str | None = None,
        family: str | None = None,
    ) -> int:
        """Delete notes (and their source rows) for one chat and/or person.

        ``family=None`` covers both families, which is what the owner purge wants.
        """
        clauses = ["workspace_id = ?", "channel = ?"]
        params: list[Any] = [self.workspace_id, str(channel)]
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(str(chat_id))
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(str(person_id))
        if family is not None:
            clauses.append("family = ?")
            params.append(str(family))
        rows = self._store.query(
            f"SELECT note_id FROM knowledge_person_notes WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        note_ids = tuple(str(row["note_id"]) for row in rows)
        if not note_ids:
            return 0
        with self._store.transaction():
            placeholders = ",".join("?" for _ in note_ids)
            self._store.execute(
                f"DELETE FROM knowledge_person_note_sources WHERE note_id IN ({placeholders})",
                note_ids,
            )
            self._store.execute(
                f"DELETE FROM knowledge_person_notes WHERE note_id IN ({placeholders})",
                note_ids,
            )
        return len(note_ids)

    def counts(
        self, *, channel: str, chat_id: str | None = None, family: str = "profile"
    ) -> tuple[dict[str, Any], ...]:
        """Aggregate counts and line kinds only; never line text."""
        rows = (
            self.notes(
                channel=channel,
                chat_id=str(chat_id) if chat_id is not None else "",
                limit=10_000,
                family=family,
            )
            if chat_id is not None
            else self._all_active(channel, family=family)
        )
        out: list[dict[str, Any]] = []
        for view in rows:
            kinds: dict[str, int] = {}
            for line in view.lines:
                kinds[line.kind] = kinds.get(line.kind, 0) + 1
            out.append(
                {
                    "chat_id": view.chat_id,
                    "person_id": view.person_id,
                    "note_id": view.note_id,
                    "status": view.status,
                    "built_ms": view.built_ms,
                    "family": view.family,
                    "kinds": kinds,
                    "line_count": len(view.lines),
                }
            )
        return tuple(out)

    def _all_active(
        self, channel: str, family: str = "profile"
    ) -> tuple[PersonNoteView, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_person_notes WHERE workspace_id = ? AND channel = ?"
            " AND family = ? AND status = 'active'"
            " ORDER BY chat_id, person_id, built_ms DESC",
            (self.workspace_id, str(channel), str(family)),
        )
        views: list[PersonNoteView] = []
        for row in rows:
            if self._is_stale(str(row["note_id"]), None):
                self._mark_stale(str(row["note_id"]), reason="stale_source")
                row = self._note_row(str(row["note_id"]))
            view = self._view(row)
            if view is not None:
                views.append(view)
        return tuple(views)

    # ── daily token budget ───────────────────────────────────────────────────

    def tokens_used(self, *, day: str) -> int:
        return int(self._store.int_meta(f"person_notes:tokens:{day}", 0) or 0)

    def add_tokens(self, *, day: str, tokens: int) -> int:
        used = self.tokens_used(day=day) + max(0, int(tokens))
        self._store.set_meta(f"person_notes:tokens:{day}", str(used))
        self._store.commit_if_idle()
        return used


def today(now_ms: int, *, tz: Any = None) -> str:
    from datetime import datetime

    moment = datetime.fromtimestamp(int(now_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    return moment.date().isoformat()


__all__ = [
    "JOB_VERSION",
    "PersonNoteStore",
    "job_id_for",
    "scope_key_for",
    "today",
]
