"""Reference-only forward capture; model calls own neither a history lease nor a transaction."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from loguru import logger

from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.knowledge._capture import CaptureReport, ObservedEvent
from yeoman_gateway.knowledge._capture_worker import (
    STATEMENT_EXTRACTOR_VERSION,
    StatementCaptureWorker,
    StatementDraft,
    WorkerReport,
    _screen,
    screen_draft,
)
from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
from yeoman_gateway.knowledge.api import KnowledgeService
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    PersonLinkCandidate,
    SourceRef,
    TrustedCaptureContext,
)


def _vector(snapshot: HistorySnapshot) -> dict[str, int]:
    return {source.relative_path: source.line_number for source in snapshot.sources}


# Only physical native captures confer forward eligibility; derived/owner/backfill do not.
_BEYOND = """EXISTS (SELECT 1 FROM json_each(m.source_refs) r
 WHERE substr(r.value,1,9)='whatsapp/'
 AND instr(r.value,'#')>0
 AND CAST(substr(r.value,instr(r.value,'#')+1) AS INTEGER)>
 COALESCE((SELECT CAST(v.value AS INTEGER) FROM json_each(?) v
 WHERE v.key=substr(r.value,1,instr(r.value,'#')-1)),0))"""


class HistoryCaptureProducer:
    def __init__(self, knowledge: KnowledgeService, *, idle_ms: int = 60_000,
                 max_delay_ms: int = 300_000, batch_max: int = 8, max_waiting: int = 64,
                 window_limit: int = 500) -> None:
        self.knowledge = knowledge
        self.store = knowledge._store
        self.idle_ms = max(1, idle_ms)
        self.max_delay_ms = max(self.idle_ms, max_delay_ms)
        self.batch_max = min(8, max(1, batch_max))
        self.max_waiting = max(1, max_waiting)
        self.window_limit = max(1, window_limit)
        self.overflows = 0

    def _state(self, key: str, default: Any = None) -> Any:
        row = self.store.query_one("SELECT value_json FROM knowledge_history_capture_state WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row is not None else default

    def _set_state(self, key: str, value: Any) -> None:
        self.store.execute("INSERT INTO knowledge_history_capture_state VALUES (?,?,1)"
                           " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,version=version+1",
                           (key, json.dumps(value, sort_keys=True)))

    def _outcome(self, mid: str, revision: int, outcome: str, job_id: str | None = None) -> None:
        self.store.execute("INSERT INTO knowledge_history_capture VALUES (?,?,?,?)"
                           " ON CONFLICT(message_id,revision) DO UPDATE SET job_id=excluded.job_id,outcome=excluded.outcome",
                           (mid, revision, job_id, outcome))

    def _refuse(self, mid: str, reason: str) -> None:
        self.store.execute("UPDATE knowledge_history_capture SET outcome=?"
                           " WHERE message_id=? AND outcome='pending'", (reason, mid))
        self._outcome(mid, 0, reason)

    @contextmanager
    def scope(self, snapshot: HistorySnapshot):
        q = HistoryQueries(snapshot)
        sources = HistoryKnowledgeSources(q, {}, self.knowledge.history_source_ledger,
                                          self.knowledge._legacy_authority)
        with self.knowledge.history_scope(q, sources):
            yield q, sources

    def _message_id(self, source: SourceRef) -> str:
        alias = self.knowledge.history_source_ledger.alias(source.key)
        return alias.message_id if alias is not None else source.event_id

    def _permanent_reason(self, row: dict[str, Any]) -> str:
        if not self.knowledge.capture_allowed(channel=row["channel"], chat_id=row["chat_id"],
                                              principal="whatsapp:" + (row["sender_identifier"] or "").split("@")[0]):
            return "not_policy_chat"
        if row["direction"] != "in":
            return "not_inbound"
        if row["provenance"] == "derived_only":
            return "derived_only"
        if row["deleted"]:
            return "source_revoked"
        if not (row["current_text"] or "").strip():
            return "empty_text"
        return ""

    def prepare_handover(self, snapshot: HistorySnapshot, *, pending: tuple[SourceRef, ...],
                         processed: tuple[SourceRef, ...],
                         legacy_boundary: tuple[int, str],
                         classifications: Mapping[str, str] | None = None) -> dict[str, Any]:
        snapshot.assert_current(snapshot.generation)
        receipt = {"version": 1, "generation": snapshot.generation,
                   "sources": [asdict(s) for s in snapshot.sources],
                   "pending": [asdict(s) for s in sorted(pending, key=lambda s: s.key)],
                   "processed": [asdict(s) for s in sorted(processed, key=lambda s: s.key)],
                   "legacy_boundary": list(legacy_boundary)}
        if classifications is not None:
            receipt["classifications"] = dict(sorted(classifications.items()))
        with self.store.transaction(), self.scope(snapshot) as (_, authority):
            existing = self._state("handover")
            if existing is not None:
                if existing != receipt:
                    raise ValueError("handover_receipt_conflict")
                return existing
            assignments: dict[str, tuple[SourceRef, str, str | None]] = {}
            for refs, outcome in ((processed, "processed"), (pending, "pending")):
                for source in refs:
                    if not authority.verify_source(source):
                        raise ValueError("unmapped_handover_source")
                    mid = self._message_id(source)
                    if mid in assignments:
                        raise ValueError("ambiguous_handover_source")
                    assignments[mid] = (source, outcome, None)
            # Preserve exact legacy job identities and all non-success states.
            for job in self.store.query("SELECT job_id,state,reason,sources_json FROM knowledge_jobs"):
                for ref in json.loads(job["sources_json"]):
                    source = authority.verify_source_ref(ref["event_id"], ref["revision"])
                    if source is None:
                        raise ValueError("unmapped_handover_job")
                    if source.channel != "whatsapp":
                        continue
                    mid = self._message_id(source)
                    state = "published" if job["state"] == "done" else (
                        "queued" if job["state"] in ("queued", "running", "failed") else (
                            "pending" if job["state"] == "skipped" and job["reason"] == "queue_full" else "cancelled"))
                    previous = assignments.get(mid)
                    if previous is not None:
                        old_source, old_state, old_job = previous
                        if (old_source != source or (old_job is not None and old_job != job["job_id"])
                                or (old_state == "processed" and state != "published")
                                or (old_state == "pending" and state not in ("queued", "pending"))
                                or (old_job is not None and old_state != state)):
                            raise ValueError("ambiguous_handover_job")
                    assignments[mid] = (source, state, job["job_id"])
            cursor = snapshot.connection.execute(
                "SELECT m.* FROM messages_current m WHERE channel='whatsapp'")
            rows = [dict(zip([c[0] for c in cursor.description], row, strict=True)) for row in cursor]
            explicit = dict(classifications or {})
            permanent = {"not_policy_chat", "not_inbound", "derived_only", "source_revoked", "empty_text"}
            if any(outcome not in permanent | {"pending", "historical_not_selected"}
                   for outcome in explicit.values()):
                raise ValueError("unknown_handover_classification")
            prefix = set()
            vector = _vector(snapshot)
            for row in rows:
                for ref in json.loads(row["source_refs"]):
                    path, _, number = ref.rpartition("#")
                    if (path.startswith("whatsapp/") and number.isdecimal()
                            and 0 < int(number) <= vector.get(path, 0)):
                        prefix.add(row["message_id"])
            if set(explicit) - prefix:
                raise ValueError("out_of_prefix_handover_classification")
            if set(explicit) & assignments.keys():
                raise ValueError("ambiguous_handover_source")
            for row in rows:
                mid = row["message_id"]
                if mid in explicit:
                    outcome = explicit.pop(mid)
                    reason = self._permanent_reason(row)
                    if ((outcome in permanent and outcome != reason)
                            or (outcome not in permanent and reason)):
                        raise ValueError("classification_refusal_mismatch")
                    self._outcome(mid, 0, outcome)
                elif mid not in assignments:
                    if not self._beyond(snapshot, mid, {}):
                        continue
                    reason = self._permanent_reason(row)
                    if not reason:
                        raise ValueError("unclassified_handover_message")
                    self._outcome(mid, 0, reason)
                else:
                    source, outcome, job = assignments.pop(mid)
                    self._outcome(mid, source.revision, outcome, job)
                    self._set_state("source:" + mid, asdict(source))
            if assignments:
                raise ValueError("unmapped_handover_message")
            revision, groups = self.knowledge.capture_policy_state()
            self._set_state("policy", {"revision": revision, "groups": list(groups),
                                     "anchors": dict.fromkeys(groups, {})})
            self._set_state("handover", receipt)
            self._set_state("vector", _vector(snapshot))
            return receipt

    def _policy(self, snapshot: HistorySnapshot) -> dict[str, Any]:
        revision, groups = self.knowledge.capture_policy_state()
        state = self._state("policy")
        if state["revision"] != revision:
            anchors = state["anchors"]
            for group in groups:
                if group not in state["groups"]:
                    # Forward starts at the first capture pass observing the loaded revision.
                    anchors[group] = _vector(snapshot)
            state = {"revision": revision, "groups": list(groups), "anchors": anchors}
            self._set_state("policy", state)
        return state

    def _rows(self, snapshot: HistorySnapshot, *, cursor: str, limit: int,
              discovered: list[str], vector: dict[str, int]) -> list[dict[str, Any]]:
        if limit == 0:
            return []
        sql = ("SELECT m.* FROM messages_current m WHERE m.channel='whatsapp' AND m.message_id>?"
               " AND m.message_id NOT IN (SELECT value FROM json_each(?)) AND " + _BEYOND +
               " ORDER BY m.message_id LIMIT ?")
        result = snapshot.connection.execute(sql, (cursor, json.dumps(discovered), json.dumps(vector), limit))
        columns = [c[0] for c in result.description]
        return [dict(zip(columns, row, strict=True)) for row in result]

    def _beyond(self, snapshot: HistorySnapshot, mid: str, vector: dict[str, int]) -> bool:
        return snapshot.connection.execute(
            "SELECT 1 FROM messages_current m WHERE message_id=? AND " + _BEYOND,
            (mid, json.dumps(vector))).fetchone() is not None

    def observed(self, source: SourceRef, row: dict[str, Any],
                 authority: HistoryKnowledgeSources, *, created_ms: int) -> ObservedEvent | None:
        if not authority.verify_source(source):
            return None
        audience = authority.evidence_audience(source, basis="observed_source_batch")
        if audience is None or audience.status == "unknown":
            return None
        return ObservedEvent(source.event_id, source.revision, source.channel, source.chat_id,
                             source.author_principal, source.occurred_at_ms, created_ms,
                             row["current_text"] or "", direction=row["direction"],
                             provider_message_id=row["native_message_id"] or "",
                             audience_status=audience.status, audience_members=tuple(sorted(audience.members)))

    def run_due(self, snapshot: HistorySnapshot, *, now_ms: int) -> CaptureReport:
        report = CaptureReport()
        snapshot.assert_current(snapshot.generation)
        with self.store.transaction(), self.scope(snapshot) as (q, authority):
            if self._state("handover") is None:
                raise HistoryPaused("capture_handover_required")
            policy = self._policy(snapshot)
            if self._state("generation") != snapshot.generation:
                self._set_state("discovery", "")
                self._set_state("generation", snapshot.generation)
            discovered = [r["message_id"] for r in self.store.query(
                "SELECT DISTINCT message_id FROM knowledge_history_capture")]
            retries = [r["message_id"] for r in self.store.query(
                "SELECT DISTINCT message_id FROM knowledge_history_capture WHERE outcome='pending' ORDER BY message_id")]
            discovery_quota = max(1, self.window_limit // 2)
            if self.window_limit == 1:
                discovery_quota = 1 - self._state("lane", 0)
                self._set_state("lane", discovery_quota)
            retry_quota = self.window_limit - discovery_quota
            cursor = self._state("discovery", "")
            rows = self._rows(snapshot, cursor=cursor, limit=discovery_quota,
                              discovered=discovered, vector=self._state("vector"))
            if not rows and cursor and discovery_quota:
                rows = self._rows(snapshot, cursor="", limit=discovery_quota,
                                  discovered=discovered, vector=self._state("vector"))
                self._set_state("discovery", "")
            if rows:
                self._set_state("discovery", rows[-1]["message_id"])
            retry_cursor = self._state("retry", "")
            ordered = [mid for mid in retries if mid > retry_cursor] + [mid for mid in retries if mid <= retry_cursor]
            selected = ordered[:retry_quota]
            if selected:
                self._set_state("retry", selected[-1])
            for mid in selected:
                row = q.message(mid)
                if row is None:
                    self._refuse(mid, "source_revoked")
                else:
                    rows.append(row)
            batches: dict[tuple[str, str | None], list[ObservedEvent]] = {}
            for row in rows:
                report.examined += 1
                mid = row["message_id"]
                reason = self._permanent_reason(row)
                anchor = policy["anchors"].get(row["chat_id"])
                if not reason and anchor and not self._beyond(snapshot, mid, anchor):
                    reason = "not_policy_chat"
                if reason:
                    self._refuse(mid, reason)
                    report.refuse(reason)
                    continue
                first_seen = self._state("seen:" + mid)
                if first_seen is None:
                    first_seen = now_ms
                    self._set_state("seen:" + mid, first_seen)
                try:
                    stored_source = self._state("source:" + mid)
                    source = SourceRef(**stored_source) if stored_source is not None else None
                    if source is None or not authority.verify_source(source):
                        source = authority.issue(mid)
                    self._set_state("source:" + mid, asdict(source))
                except KnowledgeError:
                    self._outcome(mid, 0, "pending")
                    report.refuse("unknown_evidence")
                    continue
                item = self.observed(source, row, authority, created_ms=first_seen)
                if item is None:
                    self._outcome(mid, source.revision, "pending")
                    continue
                self.store.execute("DELETE FROM knowledge_history_capture WHERE message_id=? AND revision=0", (mid,))
                self.store.execute("UPDATE knowledge_history_capture SET outcome='source_changed'"
                                   " WHERE message_id=? AND revision<>? AND outcome='pending'", (mid, source.revision))
                prior = self.store.query_one(
                    "SELECT j.job_id FROM knowledge_history_capture h JOIN knowledge_jobs j ON j.job_id=h.job_id"
                    " WHERE h.message_id=? AND h.revision=? AND j.state='skipped' AND j.reason='queue_full'",
                    (mid, source.revision))
                job_alias = prior["job_id"] if prior is not None else None
                self._outcome(mid, source.revision, "pending", job_alias)
                batches.setdefault((row["chat_id"], job_alias), []).append(item)
            for (chat, job_alias), items in batches.items():
                oldest = min(i.created_ms for i in items)
                newest = max(i.created_ms for i in items)
                if (len(items) < self.batch_max and now_ms - newest < self.idle_ms
                        and now_ms - oldest < self.max_delay_ms):
                    continue
                for start in range(0, len(items), self.batch_max):
                    chunk = items[start:start+self.batch_max]
                    refs = tuple(i.source for i in chunk)
                    if job_alias is not None:
                        # Retry the durable batch, even if new chat sources arrive beside it.
                        original = self.knowledge.capture_job_record(job_alias)
                        if original.unresolved:
                            self.knowledge.mark_capture_job(job_alias, "cancelled", reason="source_changed")
                            continue
                        refs = original.sources
                    context = TrustedCaptureContext(f"history:{chat}:{now_ms}", self.knowledge.policy_revision,
                                                    "observed_source_batch", refs)
                    receipt = self.knowledge.enqueue_capture(refs, context=context,
                        scope_key=f"whatsapp:{chat}", extractor_version=STATEMENT_EXTRACTOR_VERSION,
                        max_waiting=self.max_waiting, due_ms=now_ms, ts_ms=now_ms)
                    for source in refs:
                        self._outcome(self._message_id(source), source.revision,
                                      "queued" if receipt.state in ("queued", "running") else (
                                          "published" if receipt.state == "done" else "pending"),
                                      receipt.job_id)
                    if receipt.state == "skipped":
                        report.refuse("queue_full")
                        self.overflows += 1
                    else:
                        report.jobs += 1
                        report.promoted_sources += len(refs)
                        if receipt.reason == "already_queued":
                            report.already_queued += 1
            if self.knowledge.capture_policy_state()[0] != policy["revision"]:
                raise HistoryPaused("policy_changed")
        return report


class HistoryCaptureWorker(StatementCaptureWorker):
    def __init__(self, *, projector: HistoryProjector, knowledge: KnowledgeService,
                 producer: HistoryCaptureProducer,
                 extractor: Callable[[Sequence[ObservedEvent]], Iterable[StatementDraft]],
                 max_jobs: int = 20, poll_seconds: float = 5.0, stale_ms: int = 600_000,
                 max_attempts: int = 3, retry_delay_ms: int = 60_000) -> None:
        super().__init__(knowledge=knowledge, processing=None, extractor=extractor,
                         max_jobs=max_jobs, poll_seconds=poll_seconds, stale_ms=stale_ms,
                         max_attempts=max_attempts, retry_delay_ms=retry_delay_ms)
        self.projector, self.producer = projector, producer
        self._task: asyncio.Task[None] | None = None
        self._pass_lock = asyncio.Lock()
        self._cancel = threading.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._cancel.clear()
        self._task = asyncio.create_task(self._run_loop(), name="history-statement-capture")

    async def stop(self) -> None:
        self._cancel.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._pass_lock:
            pass

    async def _run_loop(self) -> None:
        while not self._cancel.is_set():
            try:
                await self.run_due(now_ms=int(time.time() * 1000))
            except HistoryPaused:
                pass
            except Exception as exc:
                logger.warning("history capture failed error_type={}", type(exc).__name__)
            await asyncio.sleep(self._poll_seconds)

    def _eligible_people(self, items: Sequence[ObservedEvent], source: ObservedEvent) -> tuple[str, ...]:
        author = self._knowledge._authority.author_contact(source.source)
        return (author,) if author is not None else ()

    def _links(self, draft: StatementDraft, source: ObservedEvent,
               eligible: tuple[str, ...]) -> tuple[PersonLinkCandidate, ...]:
        # StatementEngine writes the transport speaker from the source-time author proof.
        return tuple(link for link in super()._links(draft, source, eligible) if link.role != "speaker")

    def _check(self, snapshot: HistorySnapshot) -> None:
        if self._cancel.is_set():
            raise HistoryPaused("capture_cancelled")
        health = self.projector.health()
        if health["status"] != "ready":
            raise HistoryPaused(health["reason"] or health["status"])
        snapshot.assert_current(health["generation"])

    async def run_due(self, *, now_ms: int) -> WorkerReport:
        async with self._pass_lock:
            report = WorkerReport()
            owner = asyncio.current_task()
            def check(snapshot):
                if owner is not None and owner.cancelling():
                    raise HistoryPaused("capture_cancelled")
                self._check(snapshot)
            def discover(snapshot):
                check(snapshot)
                with self.producer.scope(snapshot):
                    for job in self.producer.store.query(
                            "SELECT DISTINCT j.job_id FROM knowledge_jobs j"
                            " JOIN knowledge_history_capture h ON h.job_id=j.job_id"
                            " WHERE j.state='running' AND j.updated_ms<=? LIMIT 50", (now_ms-self._stale_ms,)):
                        self._knowledge.requeue_capture_job(job["job_id"], due_ms=now_ms,
                            reason="recovered_after_crash", ts_ms=now_ms)
                promotion = self.producer.run_due(snapshot, now_ms=now_ms)
                for name in ("examined", "promoted_sources", "jobs", "already_queued", "refusals"):
                    setattr(report, name, getattr(promotion, name))
                self.overflows = self.producer.overflows
                return tuple(row["job_id"] for row in self.producer.store.query(
                    "SELECT DISTINCT j.job_id,j.due_ms FROM knowledge_jobs j"
                    " JOIN knowledge_history_capture h ON h.job_id=j.job_id"
                    " WHERE j.state='queued' AND j.due_ms<=? ORDER BY j.due_ms,j.job_id LIMIT ?",
                    (now_ms, self._max_jobs)))
            jobs = await self.projector.worker_snapshot(discover)
            for job_id in jobs:
                if self._cancel.is_set():
                    break
                def acquire(snapshot):
                    check(snapshot)
                    with self.producer.scope(snapshot) as (q, authority):
                        record = self._knowledge.capture_job_record(job_id)
                        if record.state != "queued":
                            return None
                        if record.unresolved:
                            self._cancel_job(record.job_id, "source_changed")
                            return None
                        items = []
                        for source in record.sources:
                            row = q.message(self.producer._message_id(source))
                            if row is None:
                                self._cancel_job(record.job_id, "source_changed")
                                return None
                            reason = self.producer._permanent_reason(row)
                            if reason:
                                self._cancel_job(record.job_id, reason)
                                return None
                            item = self.producer.observed(source, row, authority, created_ms=now_ms)
                            if item is None:
                                return None
                            items.append(item)
                        self._knowledge.mark_capture_job(record.job_id, "running", reason="extracting", ts_ms=now_ms)
                        return record, items
                loaded = await self.projector.worker_snapshot(acquire)
                if loaded is None:
                    continue
                record, items = loaded
                report.jobs_run += 1
                try:
                    # Provider returns detached drafts only. Await cleanup before releasing a pass.
                    model = asyncio.create_task(asyncio.to_thread(lambda: list(self._extractor(items))))
                    try:
                        drafts = await asyncio.shield(model)
                    except asyncio.CancelledError:
                        self._cancel.set()
                        while not model.done():
                            try:
                                await asyncio.shield(model)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if not model.cancelled():
                            model.exception()
                        raise
                    except Exception as exc:
                        def provider_failure(snapshot, error=exc):
                            check(snapshot)
                            with self.producer.scope(snapshot):
                                self._provider_failure(record, error, now_ms=now_ms, report=report)
                        await self.projector.worker_snapshot(provider_failure)
                        continue
                    def publish(snapshot):
                        self._check(snapshot)
                        with self.producer.scope(snapshot) as (q, authority), self._knowledge._store.transaction():
                            current = self._knowledge.capture_job_record(record.job_id)
                            if current.state == "cancelled":
                                return 0
                            if current.state == "done":
                                return 0
                            refreshed = []
                            for source in record.sources:
                                row = q.message(self.producer._message_id(source))
                                if row is None or not authority.verify_source(source):
                                    self._cancel_job(record.job_id, "source_changed")
                                    return 0
                                reason = self.producer._permanent_reason(row)
                                if reason:
                                    self._cancel_job(record.job_id, reason)
                                    return 0
                                item = self.producer.observed(source, row, authority, created_ms=now_ms)
                                if item is None:
                                    raise HistoryPaused("unknown_evidence")
                                refreshed.append(item)
                            policy_revision = self._knowledge.capture_policy_state()[0]
                            published = 0
                            for draft in drafts:
                                if screen_draft(draft):
                                    report.note_refusal(screen_draft(draft))
                                    continue
                                candidate = self._candidate(draft, refreshed, record)
                                if candidate is None or _screen(candidate):
                                    report.note_refusal("invalid_candidate")
                                    continue
                                result = self._knowledge.capture(candidate, context=self._context(record, refreshed))
                                if not result.ok:
                                    raise KnowledgeError("denied_unknown_basis", "capture publication refused")
                                published += len(result.statement_ids)
                            check(snapshot)
                            if self._knowledge.capture_policy_state()[0] != policy_revision:
                                raise HistoryPaused("policy_changed")
                            self._knowledge.mark_capture_job(record.job_id, "done",
                                reason=f"published={published}" if published else "no_candidates", ts_ms=now_ms)
                            self._job_outcome(record.job_id, "published" if published else "no_candidates")
                            return published
                    report.published += await self.projector.worker_snapshot(publish)
                except (HistoryPaused, asyncio.CancelledError):
                    self._requeue(record.job_id, now_ms=now_ms, reason="history_paused")
                    raise
                except Exception:
                    # Publication failures propagate; stale recovery retries the entire atomic batch.
                    self._requeue(record.job_id, now_ms=now_ms, reason="capture_failed")
                    raise
            return report

    def _job_outcome(self, job_id: str, outcome: str) -> None:
        for row in self._knowledge._store.query(
                "SELECT message_id,revision FROM knowledge_history_capture WHERE job_id=?", (job_id,)):
            self.producer._outcome(row["message_id"], row["revision"], outcome, job_id)

    def _cancel_job(self, job_id: str, reason: str) -> None:
        with self._knowledge._store.transaction():
            self._knowledge.mark_capture_job(job_id, "cancelled", reason=reason)
            self._job_outcome(job_id, reason)

    def _requeue(self, job_id: str, *, now_ms: int, reason: str) -> None:
        row = self._knowledge._store.query_one("SELECT state FROM knowledge_jobs WHERE job_id=?", (job_id,))
        if row is not None and row["state"] not in ("done", "cancelled"):
            self._knowledge.requeue_capture_job(job_id, due_ms=now_ms+self._retry_delay_ms,
                                               reason=reason, ts_ms=now_ms)
