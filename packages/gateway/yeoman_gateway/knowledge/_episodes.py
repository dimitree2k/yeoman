"""Source-preserving episode consolidation (Phase 2 / Task 3).

An episode is a *derived* statement about context that has closed.  It is not a second
source of truth and not independent human evidence:

* eligibility is deterministic - closed statements whose every source is older than the
  approximately two-month boundary, while open promises, future events and revoked or
  superseded statements stay operational and out of the episode;
* every episode lists the complete set of covered source *and* statement revisions, and a
  source that supports two statements is still counted once;
* revocation or correction marks the episode stale before the next read, and the staleness
  is persisted rather than recomputed on the fly;
* rebuilding is idempotent and never rewrites history: the new version supersedes the old
  one, which stays readable for audit;
* a read is never broader than every rendered source permits, and it goes through the same
  read gate as statements.

The one worker path reuses the existing ``knowledge_jobs`` state machine; no new scheduler
and no new worker framework is introduced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Sequence

from yeoman_gateway.knowledge.models import (
    DAY_MS,
    EPISODE_CLOSURE_MS,
    EpisodeBuildReport,
    EpisodeSeed,
    EpisodeSourceRef,
    EpisodeView,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.knowledge._retrieval import RetrievalEngine
    from yeoman_gateway.knowledge._store import KnowledgeStore

#: Job kind recorded in the existing ``knowledge_jobs`` table.
EPISODE_JOB_KIND = "episode_consolidation"

#: Version of the deterministic default consolidation.  A real model summarizer reports
#: its own values, and both are stored on the episode so a derivation stays attributable.
DEFAULT_MODEL_VERSION = "deterministic-digest-v1"
EPISODE_PROMPT_VERSION = "episode-consolidation-v1"

#: Open promises, plans and future references.  Such context stays operational no matter
#: how old its sources are, so it is never folded into a closed episode.
OPEN_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bi will\b",
        r"\bwe will\b",
        r"\bi'?ll\b",
        r"\bwe'?ll\b",
        r"\bgoing to\b",
        r"\bplan(s|ned|ning)?\b",
        r"\bnext (week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\btomorrow\b",
        r"\bich werde\b",
        r"\bwir werden\b",
        r"\bn(ä|ae)chste[rn]? (woche|monat|jahr)\b",
        r"\b(über|ueber)morgen\b",
        r"\bvor\b.{0,20}\bzu (machen|senden|schicken|k(ü|ue)ndigen)\b",
    )
)

#: Statements that are permanently relevant rather than part of a closed episode.
PERMANENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bevery (day|week|month|year)\b",
        r"\balways\b",
        r"\bnever\b",
        r"\bjeden (tag|woche|monat|jahr)\b",
        r"\bimmer\b",
        r"\bnie\b",
    )
)


def is_open_context(content: str) -> bool:
    """True when a statement is still operational: a promise, a plan or a future event."""
    text = str(content or "")
    return any(pattern.search(text) for pattern in OPEN_CONTEXT_PATTERNS)


def is_permanently_relevant(content: str) -> bool:
    text = str(content or "")
    return any(pattern.search(text) for pattern in PERMANENT_PATTERNS)


@dataclass(frozen=True, slots=True)
class EpisodeCandidate:
    """One eligible closed statement with its aggregate source window."""

    statement_id: str
    content: str
    author_principal: str
    confidence: float
    first_ms: int
    last_ms: int
    source_count: int


class EpisodeConsolidator:
    """Episode rows and source joins through the one Knowledge transaction owner."""

    def __init__(
        self,
        store: "KnowledgeStore",
        *,
        authority: Any,
        retrieval: "RetrievalEngine",
        workspace_id: str,
        closure_ms: int = EPISODE_CLOSURE_MS,
    ) -> None:
        self._store = store
        self._authority = authority
        self._retrieval = retrieval
        self.workspace_id = str(workspace_id)
        self._closure_ms = max(DAY_MS, int(closure_ms))

    # ── eligibility ──────────────────────────────────────────────────────────

    def eligible(self, *, scope_key: str, now_ms: int) -> tuple[EpisodeCandidate, ...]:
        """Closed statements whose every source is older than the boundary.

        The age test runs on the *source* occurrence, not on the row's write time, and a
        statement with a still-valid window, an open promise, a permanent statement, a
        revocation or a supersession is never eligible.
        """
        cutoff = int(now_ms) - self._closure_ms
        rows = self._store.query(
            "SELECT s.statement_id, s.author_principal, n.confidence,"
            " MIN(ss.occurred_at_ms) AS first_ms, MAX(ss.occurred_at_ms) AS last_ms,"
            " COUNT(DISTINCT ss.event_id || ':' || ss.revision) AS source_count"
            " FROM knowledge_statements s"
            " JOIN knowledge_statement_sources ss ON ss.statement_id = s.statement_id"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            " WHERE s.workspace_id = ? AND s.scope_key = ?"
            " AND s.status IN ('assertion','confirmed')"
            " AND s.revoked_at_ms IS NULL AND s.superseded_by IS NULL"
            " AND (s.valid_until_ms IS NULL OR s.valid_until_ms <= ?)"
            " AND ss.status = 'active'"
            " GROUP BY s.statement_id"
            " HAVING MAX(ss.occurred_at_ms) <= ?"
            " ORDER BY s.statement_id",
            (self.workspace_id, str(scope_key), int(now_ms), cutoff),
        )
        eligible: list[EpisodeCandidate] = []
        for row in rows:
            content = str(
                self._store.scalar(
                    "SELECT content FROM memory2_nodes WHERE id = ?", (str(row["statement_id"]),)
                )
                or ""
            )
            if not content.strip():
                continue
            if is_open_context(content) or is_permanently_relevant(content):
                # Open promises, plans, future events and permanent statements stay
                # operational regardless of how old their sources are.
                continue
            eligible.append(
                EpisodeCandidate(
                    statement_id=str(row["statement_id"]),
                    content=content,
                    author_principal=str(row["author_principal"] or ""),
                    confidence=float(row["confidence"] or 0.0),
                    first_ms=int(row["first_ms"] or 0),
                    last_ms=int(row["last_ms"] or 0),
                    source_count=int(row["source_count"] or 0),
                )
            )
        return tuple(eligible)

    # ── building ─────────────────────────────────────────────────────────────

    def consolidate(
        self,
        *,
        scope_key: str,
        summarizer: Callable[[Sequence[EpisodeSeed]], str] | None,
        now_ms: int,
        model_version: str = DEFAULT_MODEL_VERSION,
        prompt_version: str = EPISODE_PROMPT_VERSION,
    ) -> EpisodeBuildReport:
        """Build or reuse the active episode for one chat scope."""
        candidates = self.eligible(scope_key=str(scope_key), now_ms=int(now_ms))
        if not candidates:
            return EpisodeBuildReport(skipped=1)
        seeds = tuple(
            EpisodeSeed(
                statement_id=item.statement_id,
                content=item.content,
                author_principal=item.author_principal,
                confidence=item.confidence,
                occurred_ms=item.last_ms,
            )
            for item in candidates
        )
        digest = _digest(scope_key=str(scope_key), seeds=seeds)
        active = self._store.query_one(
            "SELECT * FROM knowledge_episodes WHERE workspace_id = ? AND scope_key = ?"
            " AND status = 'active' ORDER BY version DESC LIMIT 1",
            (self.workspace_id, str(scope_key)),
        )
        if active is not None and str(active["digest"]) == digest:
            return EpisodeBuildReport(
                reused=1, episode_ids=(str(active["episode_id"]),)
            )

        text = "" if summarizer is None else str(summarizer(seeds))
        uncertainty = _uncertainty(seeds)
        version = 1 if active is None else int(active["version"]) + 1
        supersedes = None if active is None else str(active["episode_id"])
        episode_id = self._store.new_id()
        sources = self._collect_sources(
            [item.statement_id for item in candidates]
        )
        distinct_sources = {(item.event_id, item.revision) for item in sources}

        with self._store.transaction():
            if active is not None:
                self._store.execute(
                    "UPDATE knowledge_episodes SET status = 'superseded'"
                    " WHERE episode_id = ? AND status = 'active'",
                    (str(active["episode_id"]),),
                )
            self._store.execute(
                "INSERT INTO knowledge_episodes (episode_id, workspace_id, scope_key,"
                " version, status, digest, content, content_hash, model_version,"
                " prompt_version, uncertainty, source_count, window_start_ms, window_end_ms,"
                " supersedes, created_ms, stale_ms)"
                " VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    episode_id,
                    self.workspace_id,
                    str(scope_key),
                    int(version),
                    digest,
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
                    str(model_version),
                    str(prompt_version),
                    float(uncertainty),
                    len(distinct_sources),
                    min((item.first_ms for item in candidates), default=0),
                    max((item.last_ms for item in candidates), default=0),
                    supersedes,
                    int(now_ms),
                ),
            )
            for source in sources:
                self._store.execute(
                    "INSERT OR IGNORE INTO knowledge_episode_sources (episode_id, event_id,"
                    " revision, statement_id, channel, chat_id, occurred_ms, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                    (
                        episode_id,
                        source.event_id,
                        int(source.revision),
                        source.statement_id,
                        source.channel,
                        source.chat_id,
                        int(source.occurred_ms),
                    ),
                )
        return EpisodeBuildReport(
            created=1,
            superseded=0 if supersedes is None else 1,
            episode_ids=(episode_id,),
        )

    def _collect_sources(self, statement_ids: Iterable[str]) -> tuple[EpisodeSourceRef, ...]:
        """Every covered source revision, deduplicated by (event, revision, statement)."""
        wanted = [str(item) for item in statement_ids]
        if not wanted:
            return ()
        placeholders = ",".join("?" for _ in wanted)
        rows = self._store.query(
            "SELECT statement_id, event_id, revision, channel, chat_id, occurred_at_ms,"
            " status FROM knowledge_statement_sources"
            f" WHERE statement_id IN ({placeholders})"
            " ORDER BY event_id, revision, statement_id",
            tuple(wanted),
        )
        seen: set[tuple[str, int, str]] = set()
        out: list[EpisodeSourceRef] = []
        for row in rows:
            key = (str(row["event_id"]), int(row["revision"]), str(row["statement_id"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                EpisodeSourceRef(
                    event_id=key[0],
                    revision=key[1],
                    statement_id=key[2],
                    channel=str(row["channel"] or ""),
                    chat_id=str(row["chat_id"] or ""),
                    occurred_ms=int(row["occurred_at_ms"] or 0),
                    status="active" if str(row["status"]) == "active" else "revoked",
                )
            )
        return tuple(out)

    # ── reading ──────────────────────────────────────────────────────────────

    def list_episodes(self, *, scope_key: str, context: Any) -> tuple[EpisodeView, ...]:
        """Every episode of one scope, gated and staleness-checked before rendering."""
        if str(scope_key) != context.scope_key():
            # A scope that is not the reader's own is never disclosed.
            return ()
        decision = self._retrieval.decide(context)
        if not decision.allowed:
            return (self._denied(scope_key=str(scope_key), reason=decision.reason),)
        rows = self._store.query(
            "SELECT * FROM knowledge_episodes WHERE workspace_id = ? AND scope_key = ?"
            " ORDER BY version DESC",
            (self.workspace_id, str(scope_key)),
        )
        views: list[EpisodeView] = []
        for row in rows:
            views.append(self._render(row, context=context, recipients=decision.recipients))
        return tuple(views)

    def get(self, episode_id: str, *, context: Any) -> EpisodeView:
        row = self._store.query_one(
            "SELECT * FROM knowledge_episodes WHERE episode_id = ? AND workspace_id = ?",
            (str(episode_id), self.workspace_id),
        )
        if row is None:
            return EpisodeView(episode_id=str(episode_id), reason="unknown_episode")
        if str(row["scope_key"]) != context.scope_key():
            return EpisodeView(
                episode_id=str(episode_id), scope_key=str(row["scope_key"]),
                reason="scope_mismatch",
            )
        decision = self._retrieval.decide(context)
        if not decision.allowed:
            return EpisodeView(episode_id=str(episode_id), reason=decision.reason)
        return self._render(row, context=context, recipients=decision.recipients)

    def _denied(self, *, scope_key: str, reason: str) -> EpisodeView:
        return EpisodeView(episode_id="", scope_key=str(scope_key), text="", reason=reason)

    def _render(self, row: Any, *, context: Any, recipients: Sequence[str]) -> EpisodeView:
        sources = self._sources_of(str(row["episode_id"]))
        base = EpisodeView(
            episode_id=str(row["episode_id"]),
            workspace_id=str(row["workspace_id"]),
            scope_key=str(row["scope_key"]),
            version=int(row["version"]),
            status=str(row["status"]),
            text=str(row["content"]),
            model_version=str(row["model_version"]),
            prompt_version=str(row["prompt_version"]),
            uncertainty=float(row["uncertainty"]),
            source_count=int(row["source_count"]),
            sources=sources,
            supersedes=None if row["supersedes"] is None else str(row["supersedes"]),
            created_ms=int(row["created_ms"]),
        )
        if base.status == "superseded":
            # The prior version stays readable for audit, and says what it is.
            return _replace(base, reason="superseded")
        if base.status == "stale":
            return _replace(base, text="", reason="stale_source")
        if self._is_stale(sources):
            self._mark_stale(str(row["episode_id"]))
            return _replace(base, status="stale", text="", reason="stale_source")
        if not self._disclosure_allows(sources, recipients):
            return _replace(base, text="", reason="not_permitted")
        return base

    def _sources_of(self, episode_id: str) -> tuple[EpisodeSourceRef, ...]:
        rows = self._store.query(
            "SELECT * FROM knowledge_episode_sources WHERE episode_id = ?"
            " ORDER BY event_id, revision, statement_id",
            (str(episode_id),),
        )
        return tuple(
            EpisodeSourceRef(
                event_id=str(row["event_id"]),
                revision=int(row["revision"]),
                statement_id=str(row["statement_id"]),
                channel=str(row["channel"]),
                chat_id=str(row["chat_id"]),
                occurred_ms=int(row["occurred_ms"]),
                status=str(row["status"]),
            )
            for row in rows
        )

    def _is_stale(self, sources: Sequence[EpisodeSourceRef]) -> bool:
        """A revoked source or a no-longer-current statement invalidates the episode."""
        for source in sources:
            getter = getattr(self._authority, "verify_source_ref", None)
            current = None
            if callable(getter):
                try:
                    current = getter(source.event_id, source.revision)
                except Exception:  # pragma: no cover - defensive
                    current = None
            if current is None:
                return True
            revoked = getattr(self._authority, "source_revoked", None)
            if callable(revoked) and revoked(current):
                return True
            if source.statement_id:
                row = self._store.query_one(
                    "SELECT status, revoked_at_ms, superseded_by FROM knowledge_statements"
                    " WHERE statement_id = ?",
                    (source.statement_id,),
                )
                if row is None:
                    return True
                if (
                    str(row["status"]) not in ("assertion", "confirmed")
                    or row["revoked_at_ms"] is not None
                    or row["superseded_by"] is not None
                ):
                    return True
        return False

    def _mark_stale(self, episode_id: str) -> None:
        with self._store.transaction():
            self._store.execute(
                "UPDATE knowledge_episodes SET status = 'stale', stale_ms = ?"
                " WHERE episode_id = ? AND status = 'active'",
                (self._store.now_ms(), str(episode_id)),
            )

    def _disclosure_allows(
        self, sources: Sequence[EpisodeSourceRef], recipients: Sequence[str]
    ) -> bool:
        """No broader than *every* rendered source: all of them must permit all readers."""
        if not recipients:
            return False
        wanted = set(str(item) for item in recipients)
        for source in sources:
            getter = getattr(self._authority, "verify_source_ref", None)
            current = getter(source.event_id, source.revision) if callable(getter) else None
            if current is None:
                return False
            try:
                audience = self._authority.evidence_audience(current, basis="episode_read")
            except Exception:  # pragma: no cover - defensive
                return False
            if audience is None:
                return False
            status = str(getattr(audience, "status", "unknown"))
            if status == "known":
                if not wanted <= set(getattr(audience, "members", ()) or ()):
                    return False
            elif status == "author_only":
                if not wanted <= {current.author_principal}:
                    return False
            else:
                return False
        return True


def _replace(view: EpisodeView, **changes: Any) -> EpisodeView:
    from dataclasses import replace

    return replace(view, **changes)


def _digest(*, scope_key: str, seeds: Sequence[EpisodeSeed]) -> str:
    """Stable identity of a consolidation input, so a rebuild is idempotent."""
    payload = json.dumps(
        [
            str(scope_key),
            sorted(f"{seed.statement_id}\x00{seed.content}" for seed in seeds),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _uncertainty(seeds: Sequence[EpisodeSeed]) -> float:
    """A derived statement is never certain; more agreeing sources reduce uncertainty."""
    if not seeds:
        return 1.0
    mean = sum(float(seed.confidence) for seed in seeds) / len(seeds)
    return max(0.0, min(1.0, 1.0 - mean))
