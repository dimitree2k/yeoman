"""Weekly group reaction digests: storage and the ``group_digest`` job rows.

A digest is a derived style note about one group for one ISO week.  It is modelled on
episodes:

* rebuilding never rewrites history: a new digest of the same chat supersedes the
  previous active one, which stays readable for audit;
* the source join lists every message that was shown to the model, by role
  (``approval`` / ``engagement`` / ``arvid``);
* when one of those messages is deleted in history or its Knowledge source revision is
  revoked, the digest is marked stale *before* the next read and is not rendered, and
  the staleness is persisted rather than recomputed.

The weekly job reuses the existing ``knowledge_jobs`` state machine (``queued`` ->
``running`` -> ``done`` / ``skipped`` / ``failed``) with ``kind = 'group_digest'``.  The
job id is the dedupe key: one job per chat and ISO week, ever.  ``sources_json`` stays
empty, so no statement-capture path treats these rows as capture work.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from yeoman_gateway.knowledge.models import (
    GROUP_DIGEST_CONFIDENCES,
    GROUP_DIGEST_JOB_KIND,
    GROUP_DIGEST_SOURCE_ROLES,
    GroupDigestJob,
    GroupDigestView,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.knowledge._store import KnowledgeStore

#: Version recorded in ``knowledge_jobs.extractor_version`` for digest jobs.
JOB_VERSION = "group-digest-job-v1"


def scope_key_for(channel: str, chat_id: str) -> str:
    return f"channel:{channel}:chat:{chat_id}"


def job_id_for(channel: str, chat_id: str, week: str) -> str:
    """The dedupe key: one job per chat and ISO week."""
    return f"{GROUP_DIGEST_JOB_KIND}:{channel}:{chat_id}:{week}"


def _job(row: Mapping[str, Any], *, created: bool = False) -> GroupDigestJob:
    _kind, channel, rest = str(row["job_id"]).split(":", 2)
    chat_id, week = rest.rsplit(":", 1)
    return GroupDigestJob(
        job_id=str(row["job_id"]),
        channel=channel,
        chat_id=chat_id,
        week=week,
        state=str(row["state"]),
        reason=str(row["reason"] or ""),
        attempts=int(row["attempts"] or 0),
        due_ms=int(row["due_ms"] or 0),
        created_ms=int(row["created_ms"] or 0),
        updated_ms=int(row["updated_ms"] or 0),
        created=created,
    )


class GroupDigestStore:
    """Digest rows, source joins and digest jobs through the one Knowledge owner."""

    def __init__(self, store: "KnowledgeStore", *, workspace_id: str) -> None:
        self._store = store
        self.workspace_id = str(workspace_id)
        store.ensure_group_digest_schema()

    # ── jobs ─────────────────────────────────────────────────────────────────

    def enqueue(
        self, *, channel: str, chat_id: str, week: str, now_ms: int, due_ms: int | None = None
    ) -> GroupDigestJob:
        """Queue the job of one chat and week unless any job for it already exists."""
        job_id = job_id_for(channel, chat_id, week)
        with self._store.transaction():
            cursor = self._store.execute(
                "INSERT OR IGNORE INTO knowledge_jobs (job_id, workspace_id, scope_key, kind,"
                " sources_json, extractor_version, state, reason, attempts, due_ms, created_ms,"
                " updated_ms) VALUES (?, ?, ?, ?, '[]', ?, 'queued', NULL, 0, ?, ?, ?)",
                (
                    job_id, self.workspace_id, scope_key_for(channel, chat_id),
                    GROUP_DIGEST_JOB_KIND, JOB_VERSION,
                    int(due_ms if due_ms is not None else now_ms), int(now_ms), int(now_ms),
                ),
            )
            created = cursor.rowcount == 1
        row = self._job_row(job_id)
        assert row is not None
        return _job(row, created=created)

    def _job_row(self, job_id: str) -> Any:
        return self._store.query_one(
            "SELECT * FROM knowledge_jobs WHERE job_id = ? AND kind = ?",
            (str(job_id), GROUP_DIGEST_JOB_KIND),
        )

    def job(self, job_id: str) -> GroupDigestJob | None:
        row = self._job_row(job_id)
        return None if row is None else _job(row)

    def jobs(
        self, *, channel: str = "whatsapp", chat_id: str | None = None, limit: int = 50
    ) -> tuple[GroupDigestJob, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            + (" AND scope_key = ?" if chat_id else "")
            + " ORDER BY created_ms DESC, job_id DESC LIMIT ?",
            (GROUP_DIGEST_JOB_KIND, self.workspace_id)
            + ((scope_key_for(channel, chat_id),) if chat_id else ())
            + (int(limit),),
        )
        return tuple(_job(row) for row in rows)

    def due(
        self, *, now_ms: int, max_attempts: int, limit: int = 8
    ) -> tuple[GroupDigestJob, ...]:
        """Queued jobs, and failed ones with attempts left, whose time has come."""
        rows = self._store.query(
            "SELECT * FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            " AND (state = 'queued' OR (state = 'failed' AND attempts < ?))"
            " AND due_ms <= ? ORDER BY due_ms, job_id LIMIT ?",
            (GROUP_DIGEST_JOB_KIND, self.workspace_id, int(max_attempts), int(now_ms), int(limit)),
        )
        return tuple(_job(row) for row in rows)

    def claim(self, job_id: str, *, now_ms: int, max_attempts: int) -> GroupDigestJob | None:
        """Move one due job to ``running`` and count the attempt; ``None`` if lost."""
        with self._store.transaction():
            cursor = self._store.execute(
                "UPDATE knowledge_jobs SET state = 'running', reason = 'running',"
                " attempts = attempts + 1, updated_ms = ?"
                " WHERE job_id = ? AND kind = ?"
                " AND (state = 'queued' OR (state = 'failed' AND attempts < ?))",
                (int(now_ms), str(job_id), GROUP_DIGEST_JOB_KIND, int(max_attempts)),
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
    ) -> GroupDigestJob | None:
        """Record the outcome of a running job (or put it back with ``queued``)."""
        if state not in ("queued", "done", "skipped", "failed", "cancelled"):
            raise ValidationError(f"unknown digest job state: {state!r}")
        with self._store.transaction():
            self._store.execute(
                "UPDATE knowledge_jobs SET state = ?, reason = ?, updated_ms = ?,"
                " due_ms = COALESCE(?, due_ms),"
                " attempts = MAX(0, attempts - ?) WHERE job_id = ? AND kind = ?",
                (
                    str(state), str(reason or ""), int(now_ms),
                    None if due_ms is None else int(due_ms), 1 if refund_attempt else 0,
                    str(job_id), GROUP_DIGEST_JOB_KIND,
                ),
            )
        return self.job(job_id)

    def recover_stale(self, *, updated_before_ms: int, now_ms: int) -> tuple[str, ...]:
        """Jobs a crash left ``running`` become a failed attempt, retried while allowed."""
        rows = self._store.query(
            "SELECT job_id FROM knowledge_jobs WHERE kind = ? AND workspace_id = ?"
            " AND state = 'running' AND updated_ms <= ? ORDER BY job_id",
            (GROUP_DIGEST_JOB_KIND, self.workspace_id, int(updated_before_ms)),
        )
        recovered = tuple(str(row["job_id"]) for row in rows)
        for job_id in recovered:
            self.finish(job_id, state="failed", reason="interrupted", now_ms=now_ms, due_ms=now_ms)
        return recovered

    # ── digests ──────────────────────────────────────────────────────────────

    def store(
        self,
        *,
        channel: str,
        chat_id: str,
        week: str,
        content: Mapping[str, object],
        rendered: str,
        model_version: str,
        prompt_version: str,
        confidence: str,
        signals: Mapping[str, object],
        window_start_ms: int,
        window_end_ms: int,
        sources: Iterable[tuple[str, str]],
        now_ms: int,
    ) -> GroupDigestView:
        """Insert a new digest version; the previous active one becomes ``superseded``."""
        if confidence not in GROUP_DIGEST_CONFIDENCES:
            raise ValidationError(f"unknown digest confidence: {confidence!r}")
        pairs = tuple(dict.fromkeys((str(mid), str(role)) for mid, role in sources if str(mid)))
        if any(role not in GROUP_DIGEST_SOURCE_ROLES for _mid, role in pairs):
            raise ValidationError("unknown digest source role")
        scope_key = scope_key_for(channel, chat_id)
        digest_id = self._store.new_id()
        with self._store.transaction():
            latest = self._store.query_one(
                "SELECT digest_id, version FROM knowledge_group_digests"
                " WHERE workspace_id = ? AND scope_key = ? ORDER BY version DESC LIMIT 1",
                (self.workspace_id, scope_key),
            )
            self._store.execute(
                "UPDATE knowledge_group_digests SET status = 'superseded'"
                " WHERE workspace_id = ? AND scope_key = ? AND status = 'active'",
                (self.workspace_id, scope_key),
            )
            self._store.execute(
                "INSERT INTO knowledge_group_digests (digest_id, workspace_id, scope_key,"
                " chat_id, week, version, status, content_json, rendered, model_version,"
                " prompt_version, confidence, signal_json, window_start_ms, window_end_ms,"
                " supersedes, created_ms, stale_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    digest_id, self.workspace_id, scope_key, str(chat_id), str(week),
                    1 if latest is None else int(latest["version"]) + 1,
                    json.dumps(dict(content), ensure_ascii=False, sort_keys=True),
                    str(rendered), str(model_version), str(prompt_version), str(confidence),
                    json.dumps(dict(signals), ensure_ascii=False, sort_keys=True),
                    int(window_start_ms), int(window_end_ms),
                    None if latest is None else str(latest["digest_id"]), int(now_ms),
                ),
            )
            for message_id, role in pairs:
                self._store.execute(
                    "INSERT OR IGNORE INTO knowledge_group_digest_sources"
                    " (digest_id, message_id, role) VALUES (?, ?, ?)",
                    (digest_id, message_id, role),
                )
        view = self._view(self._digest_row(digest_id))
        assert view is not None
        return view

    def has_digest(self, *, channel: str, chat_id: str, week: str) -> bool:
        return self._store.query_one(
            "SELECT 1 FROM knowledge_group_digests WHERE workspace_id = ? AND scope_key = ?"
            " AND week = ? LIMIT 1",
            (self.workspace_id, scope_key_for(channel, chat_id), str(week)),
        ) is not None

    def digests(
        self,
        *,
        chat_id: str | None = None,
        source_present: Callable[[str], bool] | None = None,
        limit: int = 50,
    ) -> tuple[GroupDigestView, ...]:
        """Digests newest first; an active one is staleness-checked before it renders."""
        rows = self._store.query(
            "SELECT * FROM knowledge_group_digests WHERE workspace_id = ?"
            + (" AND chat_id = ?" if chat_id else "")
            + " ORDER BY created_ms DESC, version DESC LIMIT ?",
            (self.workspace_id,) + ((str(chat_id),) if chat_id else ()) + (int(limit),),
        )
        views: list[GroupDigestView] = []
        for row in rows:
            if str(row["status"]) == "active" and self._is_stale(
                str(row["digest_id"]), source_present
            ):
                self._mark_stale(str(row["digest_id"]))
                row = self._digest_row(str(row["digest_id"]))
            view = self._view(row)
            if view is not None:
                views.append(view)
        return tuple(views)

    def _digest_row(self, digest_id: str) -> Any:
        return self._store.query_one(
            "SELECT * FROM knowledge_group_digests WHERE digest_id = ? AND workspace_id = ?",
            (str(digest_id), self.workspace_id),
        )

    def _sources_of(self, digest_id: str) -> tuple[tuple[str, str], ...]:
        rows = self._store.query(
            "SELECT message_id, role FROM knowledge_group_digest_sources WHERE digest_id = ?"
            " ORDER BY role, message_id",
            (str(digest_id),),
        )
        return tuple((str(row["message_id"]), str(row["role"])) for row in rows)

    def _view(self, row: Any) -> GroupDigestView | None:
        if row is None:
            return None
        status = str(row["status"])
        reason = {"superseded": "superseded", "stale": "stale_source"}.get(status, "ok")
        return GroupDigestView(
            digest_id=str(row["digest_id"]),
            chat_id=str(row["chat_id"]),
            scope_key=str(row["scope_key"]),
            week=str(row["week"]),
            version=int(row["version"]),
            status=status,
            rendered="" if status == "stale" else str(row["rendered"]),
            content=json.loads(str(row["content_json"] or "{}")),
            signals=json.loads(str(row["signal_json"] or "{}")),
            confidence=str(row["confidence"]),
            model_version=str(row["model_version"]),
            prompt_version=str(row["prompt_version"]),
            window_start_ms=int(row["window_start_ms"]),
            window_end_ms=int(row["window_end_ms"]),
            supersedes=None if row["supersedes"] is None else str(row["supersedes"]),
            created_ms=int(row["created_ms"]),
            stale_ms=None if row["stale_ms"] is None else int(row["stale_ms"]),
            sources=self._sources_of(str(row["digest_id"])),
            reason=reason,
        )

    def _is_stale(self, digest_id: str, source_present: Callable[[str], bool] | None) -> bool:
        """A deleted message or a revoked Knowledge source revision invalidates the note."""
        ledger = self._store.has_table("knowledge_history_sources")
        for message_id, _role in self._sources_of(digest_id):
            if ledger:
                row = self._store.query_one(
                    "SELECT revoked FROM knowledge_history_sources WHERE event_id = ?"
                    " ORDER BY revision DESC LIMIT 1",
                    (message_id,),
                )
                if row is not None and int(row["revoked"] or 0):
                    return True
            if source_present is not None and not source_present(message_id):
                return True
        return False

    def _mark_stale(self, digest_id: str) -> None:
        with self._store.transaction():
            self._store.execute(
                "UPDATE knowledge_group_digests SET status = 'stale', stale_ms = ?"
                " WHERE digest_id = ? AND status = 'active'",
                (self._store.now_ms(), str(digest_id)),
            )


__all__ = ["JOB_VERSION", "GroupDigestStore", "job_id_for", "scope_key_for"]
