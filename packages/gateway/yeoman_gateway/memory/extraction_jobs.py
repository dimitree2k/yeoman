"""Bounded, source-versioned extraction of shared facts (Plan 05, Aufgabe 3).

Extraction is a *candidate* generator, never a truth source:

* A job is keyed by the sorted source revisions plus the extractor version, so the same
  revision can never produce a second fact, while a new revision always produces a new job.
* Screening is deterministic code, not just a prompt: opinions, speculation, delivery
  claims, assistant text and unproven authorship are refused before anything is stored.
* The trigger is turn quiescence (idle gap or hard cap), not thread end.
* Work happens on a bounded worker thread, because the extractor calls ``asyncio.run``
  internally and must not add latency to a turn.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from loguru import logger

from yeoman_gateway.memory.shared_facts import (
    SharedFact,
    effective_audience,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.memory.store import MemoryStore

EXTRACTOR_VERSION = "v1"

#: Bases that may become a shared fact. Everything else is refused.
ACCEPTED_BASES: tuple[str, ...] = ("explicit_statement",)

#: Reasons a candidate can be refused (also used as job reasons).
REJECT_BASES: tuple[str, ...] = (
    "opinion",
    "speculation",
    "inference",
    "person_speculation",
    "delivery_claim",
)

JOB_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "done",
    "skipped",
    "cancelled",
    "failed",
)


def extraction_job_key(
    source_refs: Iterable[tuple[str, int]], extractor_version: str = EXTRACTOR_VERSION
) -> str:
    """Stable key over the sorted source references and the extractor version."""
    ordered = sorted((str(event_id), int(revision)) for event_id, revision in source_refs)
    raw = json.dumps([ordered, str(extractor_version)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def turn_settled_job(
    *,
    now_ms: int,
    first_activity_ms: int,
    last_activity_ms: int,
    idle_ms: int,
    max_delay_ms: int,
) -> bool:
    """True when a turn is quiet enough (idle) or has been active too long (cap)."""
    return (now_ms - last_activity_ms) >= idle_ms or (now_ms - first_activity_ms) >= max_delay_ms


_DAY_WORDS: tuple[tuple[str, int], ...] = (
    ("übermorgen", 2),
    ("uebermorgen", 2),
    ("übermorgen", 2),
    ("morgen", 1),
    ("heute", 0),
)


def resolve_relative_time(
    text: str, *, source_ms: int, tz_offset_minutes: int | None
) -> int | None:
    """Resolve a relative day word against the source time in the chat's timezone.

    Without a timezone the resolution is impossible and ``None`` is returned: the
    candidate stays a candidate and is never published.
    """
    if tz_offset_minutes is None:
        return None
    lowered = text.lower()
    days: int | None = None
    for word, offset in _DAY_WORDS:
        if word in lowered:
            days = offset
            break
    if days is None:
        return None
    tz = timezone(timedelta(minutes=int(tz_offset_minutes)))
    local_source = datetime.fromtimestamp(int(source_ms) / 1000, tz=tz)
    target = (local_source + timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(target.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class SharedFactCandidate:
    """A proposal. It becomes a fact only after the deterministic check passes."""

    content: str
    author_principal: str
    source_role: str = "user"
    basis: str = "explicit_statement"
    visibility_scope: str | None = None
    temporal_basis: str = "absolute"
    valid_until_ms: int | None = None
    source_refs: tuple[tuple[str, int], ...] = ()
    source_scopes: tuple[str, ...] = ()
    audience: frozenset[str] = frozenset()
    private_handoff: bool = False
    confidence: float = 0.0

    @property
    def is_resolved(self) -> bool:
        return self.temporal_basis != "unresolved"


@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    accepted: bool
    reason: str

    @property
    def rejected(self) -> bool:
        return not self.accepted


#: Phrasings that restate the message instead of stating a fact about the world.
META_STATEMENT_MARKERS: tuple[str, ...] = (
    "der autor sagt",
    "die autorin sagt",
    "der nutzer sagt",
    "the author says",
    "the user says",
    "die nachricht besagt",
    "the message says",
    "laut nachricht",
    "sagt:",
    "says:",
)


def is_meta_statement(content: str) -> bool:
    """True when a candidate only reports what was said instead of stating a fact."""
    lowered = content.strip().lower()
    return any(marker in lowered for marker in META_STATEMENT_MARKERS)


def check_candidate(candidate: SharedFactCandidate) -> CandidateVerdict:
    """Deterministic screening. Uncertainty refuses the candidate instead of guessing."""
    if candidate.private_handoff:
        return CandidateVerdict(False, "private_handoff")
    if candidate.source_role != "user":
        return CandidateVerdict(False, "not_user_source")
    if not candidate.author_principal.strip():
        return CandidateVerdict(False, "missing_author")
    if not candidate.content.strip():
        return CandidateVerdict(False, "empty")
    if candidate.basis in REJECT_BASES:
        return CandidateVerdict(False, candidate.basis)
    if candidate.basis not in ACCEPTED_BASES:
        return CandidateVerdict(False, "uncertain")
    if is_meta_statement(candidate.content):
        # "The author says X" is a transcript, not a fact; store X itself or nothing.
        return CandidateVerdict(False, "meta_statement")
    if candidate.visibility_scope is None:
        return CandidateVerdict(False, "unknown_visibility")
    if candidate.visibility_scope not in ("chat_shared", "principals", "author_only"):
        return CandidateVerdict(False, "unknown_visibility")
    if not candidate.is_resolved:
        return CandidateVerdict(False, "unresolved_time")
    private_scopes = {
        scope for scope in candidate.source_scopes if scope and scope.startswith("private:")
    }
    if len(private_scopes) > 1:
        return CandidateVerdict(False, "mixed_private_sources")
    return CandidateVerdict(True, "ok")


def initial_assertion_status(candidate: SharedFactCandidate) -> str:
    """Model confidence never yields ``confirmed``; only an explicit later confirmation does."""
    return "assertion"


def confirmation_upgrades(
    *,
    previous_author: str | None,
    previous_source_revision: int | None,
    candidate: SharedFactCandidate,
) -> bool:
    """A confirmation needs the same author and a genuinely newer source revision."""
    if previous_author is None or previous_source_revision is None:
        return False
    if previous_author != candidate.author_principal:
        return False
    if candidate.basis != "explicit_statement":
        return False
    return any(int(revision) > int(previous_source_revision) for _, revision in candidate.source_refs)


def publish_visibility(
    *,
    source_audiences: tuple[frozenset[str] | None, ...],
    membership_proven: bool,
    is_group: bool,
    audience_snapshots: Mapping[str, frozenset[str]] = {},
    snapshot_ids: tuple[str | None, ...] = (),
) -> str:
    """Decide the widest scope a fact may have. Fail closed to ``author_only``."""
    if not membership_proven or not source_audiences:
        return "author_only"
    pairs: list[tuple[frozenset[str] | None, str | None]] = []
    for index, audience in enumerate(source_audiences):
        snapshot_id = snapshot_ids[index] if index < len(snapshot_ids) else None
        pairs.append((audience, snapshot_id))
    # In a direct chat the proven participants *are* the explicit principal list; in a
    # group the source-time member list decides, and new members inherit nothing.
    explicit = (
        frozenset(source_audiences[0] or ())
        if not is_group and source_audiences[0] is not None
        else frozenset()
    )
    shared = effective_audience(
        source_audiences=tuple(pairs),
        group_rule="chat_members_at_source" if is_group else "explicit_principals",
        allowed_principals=explicit,
        audience_snapshots=audience_snapshots,
    )
    if shared:
        return "chat_shared" if is_group else "principals"
    return "author_only"


@dataclass(slots=True)
class ExtractionReport:
    """What one ``run_due`` pass did."""

    processed: int = 0
    published: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str, *, published: int = 0) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        self.processed += 1
        self.published += published


class SharedFactExtractionQueue:
    """Persistent job queue with a bounded worker thread."""

    def __init__(
        self,
        *,
        store: "MemoryStore",
        extractor: Callable[[Any], Iterable[SharedFactCandidate]] | None = None,
        journal: Any | None = None,
        idle_ms: int = 60_000,
        max_delay_ms: int = 300_000,
        max_waiting: int = 32,
        fact_ttl_ms: int | None = None,
        extractor_version: str = EXTRACTOR_VERSION,
        clock: Callable[[], int] | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._journal = journal
        self._idle_ms = int(idle_ms)
        self._max_delay_ms = int(max_delay_ms)
        self._max_waiting = max(1, int(max_waiting))
        self._fact_ttl_ms = fact_ttl_ms
        self._extractor_version = str(extractor_version)
        self._clock = clock if clock is not None else (lambda: int(time.time() * 1000))
        self._poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.overflows = 0

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="shared-fact-extraction", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:  # pragma: no cover - thread body
        while not self._stop.is_set():
            try:
                self.run_due(now_ms=self._clock())
            except Exception as exc:
                logger.warning("shared fact extraction pass failed: {}", exc)
            self._stop.wait(self._poll_seconds)

    # -- queue ------------------------------------------------------------------

    @property
    def waiting(self) -> int:
        return self._store.count_fact_jobs(state="queued") + self._store.count_fact_jobs(
            state="running"
        )

    def enqueue(
        self,
        *,
        turn_ref: str,
        source_refs: Iterable[tuple[str, int]],
        now_ms: int,
        workspace_id: str,
        chat_scope_key: str,
    ) -> str:
        """Queue one extraction. Returns the job key, or "" when the queue is full."""
        refs = tuple(sorted((str(event_id), int(revision)) for event_id, revision in source_refs))
        job_key = extraction_job_key(refs, self._extractor_version)
        existing = self._store.get_fact_job(job_key)
        if existing is not None and str(existing.get("state")) in ("queued", "running", "done"):
            return job_key
        with self._lock:
            if self.waiting >= self._max_waiting:
                self.overflows += 1
                self._store.upsert_fact_job(
                    job_key=job_key,
                    workspace_id=workspace_id,
                    chat_scope_key=chat_scope_key,
                    source_refs_json=json.dumps([list(item) for item in refs]),
                    extractor_version=self._extractor_version,
                    state="skipped",
                    reason="queue_full",
                    due_ms=int(now_ms),
                    now_ms=int(now_ms),
                )
                return job_key
        self._store.upsert_fact_job(
            job_key=job_key,
            workspace_id=workspace_id,
            chat_scope_key=chat_scope_key,
            source_refs_json=json.dumps([list(item) for item in refs]),
            extractor_version=self._extractor_version,
            state="queued",
            due_ms=int(now_ms),
            now_ms=int(now_ms),
            first_activity_ms=int(now_ms),
            last_activity_ms=int(now_ms),
        )
        return job_key

    def cancel_sources(self, source_event_ids: Iterable[str], *, now_ms: int) -> int:
        """Cancel queued or running jobs that rest on the given sources."""
        wanted = {str(item) for item in source_event_ids}
        if not wanted:
            return 0
        cancelled = 0
        for job in self._store.list_fact_jobs(limit=500):
            if str(job.get("state")) not in ("queued", "running"):
                continue
            refs = {str(event_id) for event_id, _ in _job_refs(job)}
            if refs & wanted:
                self._store.upsert_fact_job(
                    job_key=str(job["job_key"]),
                    workspace_id=str(job["workspace_id"]),
                    chat_scope_key=str(job["chat_scope_key"]),
                    source_refs_json=str(job["source_refs_json"]),
                    extractor_version=str(job["extractor_version"]),
                    state="cancelled",
                    reason="source_invalidated",
                    due_ms=int(job["due_ms"]),
                    now_ms=int(now_ms),
                )
                cancelled += 1
        return cancelled

    def run_due(self, *, now_ms: int, limit: int | None = None) -> ExtractionReport:
        """Process due jobs synchronously. The worker thread simply calls this."""
        report = ExtractionReport()
        jobs = self._store.list_fact_jobs(state="queued", due_before_ms=int(now_ms), limit=limit or 20)
        for job in jobs:
            self._run_job(job, now_ms=int(now_ms), report=report)
        return report

    # -- one job ----------------------------------------------------------------

    def _run_job(self, job: Mapping[str, Any], *, now_ms: int, report: ExtractionReport) -> None:
        refs = _job_refs(job)
        state = str(job.get("state"))
        if state != "queued":
            return
        self._mark(job, state="running", reason=None, now_ms=now_ms)

        events = self._load_events(refs)
        if events is None:
            self._mark(job, state="skipped", reason="payload_unavailable", now_ms=now_ms)
            report.note("payload_unavailable")
            return
        if any(bool(getattr(event, "payload", None)) and _is_private_handoff(event) for event in events):
            self._mark(job, state="skipped", reason="private_handoff", now_ms=now_ms)
            report.note("private_handoff")
            return
        if self._extractor is None:
            self._mark(job, state="skipped", reason="no_extractor", now_ms=now_ms)
            report.note("no_extractor")
            return

        try:
            candidates = list(self._extractor(events))
        except Exception as exc:
            self._mark(job, state="failed", reason=f"extractor_error:{type(exc).__name__}", now_ms=now_ms)
            report.failed += 1
            report.note(f"failed:{type(exc).__name__}")
            return

        published = 0
        skipped_reasons: list[str] = []
        for candidate in candidates:
            verdict = check_candidate(candidate)
            if verdict.rejected:
                skipped_reasons.append(verdict.reason)
                continue
            if self._publish(candidate, job=job, now_ms=now_ms):
                published += 1
        if published:
            self._mark(job, state="done", reason=None, now_ms=now_ms)
            report.note("published", published=published)
        else:
            reason = skipped_reasons[0] if skipped_reasons else "no_candidates"
            self._mark(job, state="skipped", reason=reason, now_ms=now_ms)
            report.note(reason)

    def _publish(
        self, candidate: SharedFactCandidate, *, job: Mapping[str, Any], now_ms: int
    ) -> bool:
        refs = candidate.source_refs or _job_refs(job)
        if not refs:
            return False
        fact_id = extraction_job_key(refs, self._extractor_version)
        if self._store.get_fact(fact_id) is not None and not candidate.source_refs:
            return False
        valid_until = candidate.valid_until_ms
        if valid_until is None and self._fact_ttl_ms:
            valid_until = int(now_ms) + int(self._fact_ttl_ms)
        fact = SharedFact(
            fact_id=fact_id,
            workspace_id=str(job["workspace_id"]),
            chat_scope_key=str(job["chat_scope_key"]),
            content=candidate.content,
            author_principal=candidate.author_principal,
            assertion_status=initial_assertion_status(candidate),  # type: ignore[arg-type]
            visibility_scope=(candidate.visibility_scope or "author_only"),  # type: ignore[arg-type]
            group_rule=(
                "chat_members_at_source"
                if candidate.visibility_scope == "chat_shared"
                else "explicit_principals"
            ),  # type: ignore[arg-type]
            valid_from_ms=int(now_ms),
            valid_until_ms=valid_until,
            extractor_version=self._extractor_version,
            sources=tuple(_fact_sources(refs, candidate)),
            allowed_principals=frozenset(),
            audience=frozenset(candidate.audience),
            created_ms=int(now_ms),
            updated_ms=int(now_ms),
        )
        self._store.upsert_fact(fact)
        return True

    def _load_events(self, refs: tuple[tuple[str, int], ...]) -> list[Any] | None:
        """All source events, or ``None`` when any payload is no longer available."""
        if self._journal is None:
            return None
        events: list[Any] = []
        for event_id, _revision in refs:
            event = _get_event(self._journal, event_id)
            if event is None or not getattr(event, "payload_available", True):
                return None
            events.append(event)
        return events

    def _mark(
        self,
        job: Mapping[str, Any],
        *,
        state: str,
        reason: str | None,
        now_ms: int,
    ) -> None:
        self._store.upsert_fact_job(
            job_key=str(job["job_key"]),
            workspace_id=str(job["workspace_id"]),
            chat_scope_key=str(job["chat_scope_key"]),
            source_refs_json=str(job["source_refs_json"]),
            extractor_version=str(job["extractor_version"]),
            state=state,
            reason=reason,
            due_ms=int(job["due_ms"]),
            now_ms=int(now_ms),
            first_activity_ms=int(job.get("first_activity_ms") or now_ms),
            last_activity_ms=int(now_ms),
            attempts=int(job.get("attempts") or 0) + (1 if state in ("done", "failed", "skipped") else 0),
        )


def _job_refs(job: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    try:
        raw = json.loads(str(job.get("source_refs_json") or "[]"))
    except (TypeError, ValueError):
        return ()
    refs: list[tuple[str, int]] = []
    for item in raw:
        try:
            event_id, revision = item
        except (TypeError, ValueError):
            continue
        refs.append((str(event_id), int(revision)))
    return tuple(refs)


def _fact_sources(refs: tuple[tuple[str, int], ...], candidate: SharedFactCandidate) -> list[Any]:
    from yeoman_gateway.memory.shared_facts import FactSource

    return [
        FactSource(
            source_event_id=event_id,
            source_revision=int(revision),
            author_principal=candidate.author_principal,
        )
        for event_id, revision in refs
    ]


def _get_event(journal: Any, event_id: str) -> Any | None:
    for name in ("get_event", "event", "load_event"):
        method = getattr(journal, name, None)
        if method is None:
            continue
        try:
            return method(event_id)
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _is_private_handoff(event: Any) -> bool:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = payload
    return bool(
        metadata.get("private_handoff_active") or metadata.get("private_handoff_origin_chat_id")
    )
