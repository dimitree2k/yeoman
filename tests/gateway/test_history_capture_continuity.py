"""Synthetic reference vectors, real Knowledge transactions and worker-thread snapshots."""
import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict

import pytest
from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused, HistoryProjector
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.history.schema import create
from yeoman_gateway.knowledge._capture_worker import StatementDraft
from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.models import TrustedCaptureContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_shared.raw_archive.records import SourceBoundary

from tests.gateway.convhist.test_hist_queries import contact, event, identifier, message

PHONE = "10001@s.whatsapp.net"
GROUP = "synthetic@g.us"
MS = 1791000000000


class History:
    """Model committed projection arrivals/replacements without inventing live raw text."""
    def __init__(self, path):
        self.path = path
        self.generation = 1
        self.lines = 10
        self.extra_files = {}
        self.status = "ready"
        with closing(sqlite3.connect(path)) as db, db:
            create(db)
            contact(db, "a")
            identifier(db, "a", PHONE, start=1)
            event(db, "roster", "member_snapshot", MS - 1,
                  {"complete": True, "participants": [[PHONE]]}, chat=GROUP)
            self.runtime(db)
        self.projector = object.__new__(HistoryProjector)
        self.projector.db_path = path
        self.projector._operation_lock = asyncio.Lock()
        self.projector._executor = ThreadPoolExecutor(max_workers=1)
        self.projector._stopping = False
        self.projector._status = "ready"
        self.projector._reason = None
        async def admit():
            if self.status != "ready":
                raise HistoryPaused(self.status)
            return HistoryBoundary(self.generation, self.vector())
        self.projector._admit = admit
        self.projector.health = lambda: {"status": self.status, "reason": None, "generation": self.generation}

    def vector(self):
        files = {"whatsapp/2026-10.jsonl": self.lines, **self.extra_files}
        return tuple(SourceBoundary(path, lines, lines * 100, "synthetic")
                     for path, lines in sorted(files.items()))

    def runtime(self, db):
        db.execute("INSERT OR REPLACE INTO projector_state VALUES ('@runtime',0,0,'',4,?)",
                   (json.dumps({"generation": self.generation, "status": "ready"}),))

    def add(self, mid, *, line=None, chat=PHONE, known=True, provenance="native", direction="in", refs=None):
        if line is None:
            self.lines += 1
            line = self.lines
        for ref in refs or ():
            path, _, number = ref.rpartition("#")
            if path.startswith("whatsapp/") and path != "whatsapp/2026-10.jsonl":
                self.extra_files[path] = max(self.extra_files.get(path, 0), int(number))
        with closing(sqlite3.connect(self.path)) as db, db:
            message(db, mid, chat=chat, ms=MS, text=f"Synthetic source {mid}",
                    identifier=PHONE if known else None, sender="a" if known else None,
                    provenance=provenance)
            db.execute("UPDATE messages SET direction=?,source_refs=? WHERE message_id=?",
                       (direction, json.dumps(refs or [f"whatsapp/2026-10.jsonl#{line}"]), mid))
        return mid

    @contextmanager
    def snapshot(self):
        reader = HistoryReader(self.path)
        snap = reader.open_snapshot(HistoryBoundary(self.generation, self.vector()))
        try:
            yield snap
        finally:
            snap.close()
            reader.close()

    def replace(self):
        self.generation += 1
        replacement = self.path.with_suffix(".replacement")
        with closing(sqlite3.connect(self.path)) as old, closing(sqlite3.connect(replacement)) as new, old, new:
            old.backup(new)
            self.runtime(new)
        replacement.replace(self.path)

    def close(self):
        self.projector._executor.shutdown()


@pytest.fixture
def case(tmp_path):
    old = tmp_path / "v2.db"
    store = KnowledgeStore(old)
    store.set_meta("migration_complete", "1")
    store.close()
    path = tmp_path / "v3.db"
    upgrade_history_knowledge(source=old, target=path)
    policy = RuntimeKnowledgePolicy(PolicyEngine(PolicyConfig.model_validate({
        "defaults": {"whoCanTalk": {"mode": "everyone"}},
        "channels": {"whatsapp": {"chats": {GROUP: {}}}},
    }), tmp_path))
    legacy = RuntimeKnowledgeSources()
    def open_service():
        return open_knowledge_store(path, workspace_id="synthetic", source_authority=legacy,
                                    policy_authority=policy, history_mode=True)
    knowledge = open_service()
    history = History(tmp_path / "history.db")
    yield history, knowledge, policy, open_service
    history.close()
    knowledge.close()


def producer(knowledge, **kwargs):
    from yeoman_gateway.knowledge._history_capture import HistoryCaptureProducer
    kwargs.setdefault("batch_max", 1)
    idle = kwargs.pop("idle_ms_override", 1)
    return HistoryCaptureProducer(knowledge, idle_ms=idle, max_delay_ms=idle, **kwargs)


def handover(history, p, *, pending=(), processed=()):
    with history.snapshot() as snap:
        return p.prepare_handover(snap, pending=pending, processed=processed, legacy_boundary=(123, "legacy"))


def issue(history, knowledge, mids):
    with history.snapshot() as snap:
        q = HistoryQueries(snap)
        sources = HistoryKnowledgeSources(q, {}, knowledge.history_source_ledger, knowledge._legacy_authority)
        return tuple(sources.issue(mid) for mid in mids)


def outcomes(knowledge):
    return {r["message_id"]: r["outcome"] for r in knowledge._store.query(
        "SELECT * FROM knowledge_history_capture")}


def statements(knowledge):
    return [(r["event_id"], r["content"]) for r in knowledge._store.query(
        "SELECT s.event_id,k.content FROM knowledge_statement_sources s"
        " JOIN memory2_nodes k ON k.id=s.statement_id")]


def worker(history, knowledge, p, extractor=None, **kwargs):
    from yeoman_gateway.knowledge._history_capture import HistoryCaptureWorker
    return HistoryCaptureWorker(projector=history.projector, knowledge=knowledge, producer=p,
                                extractor=extractor or extract, stale_ms=60000, **kwargs)


def extract(items):
    return [StatementDraft(f"Expected statement {item.event_id}", source_index=i)
            for i, item in enumerate(items)]


@pytest.mark.asyncio
async def test_capture_no_gap_across_switch_pause_rebuild_and_late_pair(case):
    h, k, _, reopen = case
    h.add("historical", line=1)
    h.add("queued", line=2)
    h.add("raw-pending", line=3)
    refs = issue(h, k, ("historical", "queued", "raw-pending"))
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        sources = HistoryKnowledgeSources(q, {}, k.history_source_ledger, k._legacy_authority)
        with k.history_scope(q, sources):
            queued = k.enqueue_capture((refs[1],), context=TrustedCaptureContext(
                "legacy", k.policy_revision, "observed_source_batch", (refs[1],)),
                extractor_version="statement-capture-v1", ts_ms=MS)
    p = producer(k)
    receipt = handover(h, p, pending=refs[1:], processed=refs[:1])
    assert handover(h, p, pending=refs[1:], processed=refs[:1]) == receipt
    assert receipt["legacy_boundary"] == [123, "legacy"]
    seen = []
    def extracting(items):
        seen.extend(item.event_id for item in items)
        assert not h.projector._operation_lock.locked()
        return extract(items)
    w = worker(h, k, p, extracting)
    h.add("equal-time")
    h.status = "rebuilding"
    with pytest.raises(HistoryPaused):
        await w.run_due(now_ms=MS+100)
    # A request ref predates the vector; its late native result is beyond it.
    # This is a synthetic inbound projection; real bot outbound pairs are refused separately.
    h.add("late-pair", refs=["whatsapp/2026-10.jsonl#4", "whatsapp/2026-11.jsonl#1"])
    h.add("during-rebuild")
    with closing(sqlite3.connect(h.path)) as db, db:
        db.execute("UPDATE messages SET sent_ms=?,time_certainty='provider_timestamp' WHERE message_id='late-pair'",
                   (MS-5000,))
    h.replace()
    h.status = "ready"
    for _ in range(3):
        await w.run_due(now_ms=MS+1000)
    k.close()
    k = reopen()
    p = producer(k)
    w = worker(h, k, p, extracting)
    await w.run_due(now_ms=MS+100000)
    expected = {"queued", "raw-pending", "equal-time", "late-pair", "during-rebuild"}
    assert set(seen) == expected
    assert outcomes(k) == {**dict.fromkeys(expected, "published"), "historical": "processed"}
    assert set(statements(k)) == {(mid, f"Expected statement {mid}") for mid in expected}
    assert len(statements(k)) == 5
    jobs = k._store.query("SELECT job_id,state,sources_json FROM knowledge_jobs")
    assert all(r["state"] == "done" for r in jobs)
    assert {s["event_id"] for r in jobs for s in json.loads(r["sources_json"])} == expected
    assert queued.job_id in {r["job_id"] for r in jobs}
    k.close()


@pytest.mark.asyncio
async def test_capture_expected_refusals_are_separate_from_promotable_continuity(case):
    h, k, _, _ = case
    p = producer(k)
    handover(h, p)
    h.add("unknown", known=False)
    h.add("bot-outbound", direction="out")
    h.add("derived", provenance="derived_only")
    h.add("no-policy", chat="outside@g.us")
    await worker(h, k, p).run_due(now_ms=MS+1000)
    states = outcomes(k)
    assert states["unknown"] == "pending"
    assert states["bot-outbound"] == "not_inbound"
    assert states["derived"] == "derived_only"
    assert states["no-policy"] == "not_policy_chat"
    assert statements(k) == []


@pytest.mark.asyncio
async def test_pending_unknowns_do_not_starve_other_chat(case):
    h, k, _, _ = case
    p = producer(k, window_limit=500)
    handover(h, p)
    for i in range(501):
        h.add(f"a{i:04}", known=False)
    h.add("z-authorized", chat=GROUP)
    seen = []
    def extracting(items):
        seen.extend(i.event_id for i in items)
        return extract(items)
    w = worker(h, k, p, extracting)
    for _ in range(3):
        report = await w.run_due(now_ms=MS+1000)
        assert report.examined <= 500
    assert seen == ["z-authorized"]
    assert statements(k) == [("z-authorized", "Expected statement z-authorized")]
    assert outcomes(k) == {**dict.fromkeys((f"a{i:04}" for i in range(501)), "pending"),
                           "z-authorized": "published"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "edit", "purge", "cancel", "policy"])
async def test_capture_pause_during_model_call_requeues_without_publishing(case, change):
    h, k, policy, _ = case
    p = producer(k)
    handover(h, p)
    h.add("m")
    entered, release = threading.Event(), threading.Event()
    def extracting(items):
        entered.set()
        assert release.wait(30)
        return extract(items)
    w = worker(h, k, p, extracting)
    task = asyncio.create_task(w.run_due(now_ms=MS+1000))
    assert await asyncio.to_thread(entered.wait, 30)
    assert not h.projector._operation_lock.locked()
    if change == "pause":
        h.status = "rebuilding"
    elif change == "policy":
        policy.engine = PolicyEngine(PolicyConfig.model_validate({
            "defaults": {"whoCanTalk": {"mode": "allowlist", "senders": []}},
        }), h.path.parent)
        policy.policy_revision += 1
    elif change == "cancel":
        for row in k._store.query("SELECT job_id FROM knowledge_jobs"):
            k.mark_capture_job(row["job_id"], "cancelled")
    else:
        with closing(sqlite3.connect(h.path)) as db, db:
            if change == "purge":
                db.execute("DELETE FROM messages WHERE message_id='m'")
            else:
                event(db, "edit", "edit", MS+1, {"text": "Changed source"}, target="m", chat=PHONE)
    release.set()
    if change == "pause":
        with pytest.raises(HistoryPaused):
            await task
        assert k._store.scalar("SELECT state FROM knowledge_jobs") == "queued"
        h.replace()
        h.status = "ready"
        await worker(h, k, p).run_due(now_ms=MS+100000)
        assert outcomes(k)["m"] == "published"
    else:
        await task
        assert statements(k) == []
        assert k._store.scalar("SELECT state FROM knowledge_jobs") in ("cancelled", "skipped")


@pytest.mark.asyncio
async def test_capture_queue_full_and_crash_after_enqueue_do_not_skip(case, monkeypatch):
    h, k, _, _ = case
    p = producer(k, batch_max=1, max_waiting=1)
    handover(h, p)
    h.add("a")
    h.add("b")
    original = k.enqueue_capture
    calls = []
    def crash(*args, **kwargs):
        receipt = original(*args, **kwargs)
        calls.append(receipt.job_id)
        raise RuntimeError("after enqueue")
    monkeypatch.setattr(k, "enqueue_capture", crash)
    with h.snapshot() as snap, pytest.raises(RuntimeError, match="after enqueue"):
        p.run_due(snap, now_ms=MS+1000)
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs") == 0
    monkeypatch.setattr(k, "enqueue_capture", original)
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+1000)
    assert calls[0] in {r["job_id"] for r in k._store.query("SELECT job_id FROM knowledge_jobs")}
    assert "pending" in outcomes(k).values()
    w = worker(h, k, p)
    for _ in range(3):
        await w.run_due(now_ms=MS+100000)
    assert outcomes(k) == {"a": "published", "b": "published"}
    assert len(statements(k)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capture", "finish", "ledger", "commit"])
async def test_capture_crash_after_first_candidate_rolls_back_batch_and_retry_publishes_once(case, monkeypatch, failure):
    h, k, _, reopen = case
    p = producer(k)
    handover(h, p)
    h.add("m")
    def first(items):
        return [StatementDraft("First discarded wording one"), StatementDraft("First discarded wording two")]
    w = worker(h, k, p, first)
    baseline = {t: k._store.scalar(f"SELECT count(*) FROM {t}") for t in (
        "knowledge_statements", "knowledge_statement_sources", "knowledge_statement_people", "knowledge_statement_audit")}
    original_capture = k.capture
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        if failure == "capture":
            raise RuntimeError("synthetic crash")
        if failure == "commit":
            k._store.fail_next_commit = True
        return result
    monkeypatch.setattr(k, "capture", capture)
    if failure == "finish":
        original_mark = k.mark_capture_job
        def mark(job, state, **kwargs):
            result = original_mark(job, state, **kwargs)
            if state == "done":
                raise RuntimeError("synthetic crash")
            return result
        monkeypatch.setattr(k, "mark_capture_job", mark)
    if failure == "ledger":
        original_outcome = p._outcome
        def outcome(*args, **kwargs):
            original_outcome(*args, **kwargs)
            if args[2] == "published":
                raise RuntimeError("synthetic crash")
        monkeypatch.setattr(p, "_outcome", outcome)
    with pytest.raises(Exception, match="synthetic crash|injected commit"):
        await w.run_due(now_ms=MS+1000)
    k.close()
    k = reopen()
    assert {t: k._store.scalar(f"SELECT count(*) FROM {t}") for t in baseline} == baseline
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs WHERE state='done'") == 0
    assert outcomes(k)["m"] != "published"
    def retry(items):
        return [StatementDraft("Retry complete wording one"), StatementDraft("Retry complete wording two")]
    p = producer(k)
    w = worker(h, k, p, retry)
    await w.run_due(now_ms=MS+1000000)
    await w.run_due(now_ms=MS+1000001)
    assert set(statements(k)) == {("m", "Retry complete wording one"), ("m", "Retry complete wording two")}
    assert len(statements(k)) == 2
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs WHERE state='done'") == 1
    assert outcomes(k)["m"] == "published"
    k.close()


@pytest.mark.asyncio
async def test_capture_skips_non_policy_groups_and_starts_forward_when_added(case):
    h, k, policy, _ = case
    p = producer(k)
    handover(h, p)
    outside = "outside@g.us"
    with closing(sqlite3.connect(h.path)) as db, db:
        event(db, "outside-roster", "member_snapshot", MS-1,
              {"complete": True, "participants": [[PHONE]]}, chat=outside)
    h.add("old-outside", chat=outside)
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert outcomes(k)["old-outside"] == "not_policy_chat"
    h.add("before-observed-revision", chat=outside)
    policy.engine = PolicyEngine(PolicyConfig.model_validate({
        "defaults": {"whoCanTalk": {"mode": "everyone"}},
        "channels": {"whatsapp": {"chats": {GROUP: {}, outside: {}}}},
    }), h.path.parent)
    policy.policy_revision += 1
    await worker(h, k, p).run_due(now_ms=MS+1001)
    h.add("forward", chat=outside)
    await worker(h, k, p).run_due(now_ms=MS+10000)
    assert statements(k) == [("forward", "Expected statement forward")]
    assert outcomes(k)["before-observed-revision"] == "not_policy_chat"


def test_handover_rejects_unmapped_or_unclassified_pre_vector(case):
    h, k, _, _ = case
    h.add("unclassified", line=1)
    with pytest.raises(ValueError, match="unclassified"):
        handover(h, producer(k))
    refs = issue(h, k, ("unclassified",))
    receipt = handover(h, producer(k), pending=refs)
    assert receipt["pending"] == [asdict(refs[0])]
    assert statements(k) == []


@pytest.mark.asyncio
async def test_single_candidate_window_alternates_discovery_and_pending_retry(case):
    h, k, _, _ = case
    p = producer(k, window_limit=1)
    handover(h, p)
    h.add("a-unknown", known=False)
    await worker(h, k, p).run_due(now_ms=MS+1000)
    with closing(sqlite3.connect(h.path)) as db, db:
        db.execute("UPDATE messages SET sender_contact_id='a',sender_identifier=?,sender_basis='native_identifier'"
                   " WHERE message_id='a-unknown'", (PHONE,))
    h.add("z-new")
    await worker(h, k, p).run_due(now_ms=MS+2000)
    assert outcomes(k)["a-unknown"] == "published"
    await worker(h, k, p).run_due(now_ms=MS+3000)
    assert outcomes(k)["z-new"] == "published"


@pytest.mark.asyncio
async def test_provider_failure_respects_retry_delay_and_attempt_limit(case):
    h, k, _, _ = case
    p = producer(k)
    handover(h, p)
    h.add("m")
    calls = []
    def fail(items):
        calls.append(items[0].event_id)
        raise RuntimeError("provider fixture failure")
    w = worker(h, k, p, fail, max_attempts=2, retry_delay_ms=1000)
    report = await w.run_due(now_ms=MS+1000)
    assert report.refused["provider_error"] == 1
    assert k._store.scalar("SELECT state FROM knowledge_jobs") == "queued"
    await w.run_due(now_ms=MS+1001)
    assert calls == ["m"]
    report = await w.run_due(now_ms=MS+2000)
    assert report.failed == 1
    assert k._store.scalar("SELECT state FROM knowledge_jobs") == "failed"
    assert outcomes(k)["m"] != "published"
    assert statements(k) == []


@pytest.mark.asyncio
async def test_unprocessed_edit_queues_current_revision_once_and_preserves_job_identity(case):
    h, k, _, _ = case
    p = producer(k, idle_ms_override=3000, batch_max=8)
    handover(h, p)
    h.add("m")
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+1000)
    with closing(sqlite3.connect(h.path)) as db, db:
        event(db, "edit", "edit", MS+1, {"text": "Current synthetic content"}, target="m", chat=PHONE)
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+2000)
    w = worker(h, k, p)
    for moment in (MS+10000, MS+11000, MS+12000):
        await w.run_due(now_ms=moment)
    assert statements(k) == [("m", "Expected statement m")]
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs") == 1
    assert all(r["outcome"] != "pending" for r in k._store.query("SELECT outcome FROM knowledge_history_capture"))


def test_builder_selection_keeps_default_worker_and_selects_history(case, monkeypatch):
    from yeoman_gateway.app.bootstrap import build_statement_capture
    from yeoman_gateway.knowledge import _capture_worker
    from yeoman_gateway.knowledge._history_capture import HistoryCaptureWorker
    from yeoman_shared.config.loader import load_config
    h, k, _, _ = case
    monkeypatch.setattr(_capture_worker, "StatementExtractor", lambda **kwargs: extract)
    base = {"knowledge": {"captureEnabled": True}}
    path = h.path.parent / "settings.json"
    path.write_text(json.dumps(base))
    dormant = load_config(path)
    legacy = build_statement_capture(dormant, knowledge=k, processing=object(), projector=h.projector)
    assert type(legacy).__name__ == "StatementCaptureWorker"
    path.write_text(json.dumps({**base, "history": {
        "liveProjectionEnabled": True, "readers": {"knowledge": True}}}))
    selected = load_config(path)
    assert isinstance(build_statement_capture(selected, knowledge=k, processing=None, projector=h.projector),
                      HistoryCaptureWorker)
    assert k._store.scalar("SELECT count(*) FROM knowledge_history_capture_state") == 0


@pytest.mark.asyncio
async def test_handover_excludes_backfill_owner_and_lineage_only_refs(case):
    h, k, _, _ = case
    h.add("backfill", refs=["backfill/frozen.jsonl#1"])
    h.add("owner-only", refs=["owner/attestations.jsonl#2"])
    h.add("derived-only-ref", refs=["derived/contact-ids.jsonl#3"])
    p = producer(k)
    handover(h, p)
    h.add("forward")
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert outcomes(k) == {"forward": "published"}
    assert statements(k) == [("forward", "Expected statement forward")]


@pytest.mark.asyncio
async def test_handover_preserves_immutable_legacy_job_alias(case):
    from yeoman_gateway.knowledge._history_sources import HistorySourceAlias
    from yeoman_gateway.knowledge.models import SourceRef
    h, k, _, _ = case
    h.add("canonical", line=1)
    issued = SourceRef("legacy-event", 7, "whatsapp", PHONE, "whatsapp:10001", MS)
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        alias = HistorySourceAlias(issued, "canonical", 7, "a",
                                   q.content_fingerprint("canonical"), q.audience("canonical"))
        ledger = k.history_source_ledger
        ledger.persist_aliases({issued.key: alias})
        authority = HistoryKnowledgeSources(q, {}, ledger, k._legacy_authority)
        with k.history_scope(q, authority):
            receipt = k.enqueue_capture((issued,), context=TrustedCaptureContext(
                "legacy-job", k.policy_revision, "observed_source_batch", (issued,)),
                extractor_version="statement-capture-v1", ts_ms=MS)
    p = producer(k)
    handover(h, p, pending=(issued,))
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert outcomes(k) == {"canonical": "published"}
    assert statements(k) == [("legacy-event", "Expected statement legacy-event")]
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs") == 1
    assert k._store.scalar("SELECT job_id FROM knowledge_jobs") == receipt.job_id
    assert ledger.alias(issued.key).issued == issued


@pytest.mark.asyncio
async def test_queue_full_batch_keeps_job_identity_when_new_sources_arrive(case):
    h, k, _, _ = case
    p = producer(k, batch_max=1, max_waiting=1)
    handover(h, p)
    h.add("0-fill")
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+1000)
    p.batch_max = 2
    h.add("a", chat=GROUP)
    h.add("b", chat=GROUP)
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+1001)
    skipped = k._store.query_one("SELECT job_id,sources_json FROM knowledge_jobs WHERE state='skipped'")
    assert skipped is not None
    old_id = skipped["job_id"]
    old_sources = {s["event_id"] for s in json.loads(skipped["sources_json"])}
    w = worker(h, k, p)
    await w.run_due(now_ms=MS+2000)
    h.add("c", chat=GROUP)
    for moment in (MS+3000, MS+4000, MS+5000):
        await w.run_due(now_ms=moment)
    assert k._store.query_one("SELECT state FROM knowledge_jobs WHERE job_id=?", (old_id,))["state"] == "done"
    assert {mid for mid, _ in statements(k)} >= old_sources
    assert len(statements(k)) == 4


@pytest.mark.asyncio
async def test_handover_and_worker_preserve_other_channel_jobs(case):
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    from yeoman_gateway.knowledge.models import SourceRef
    h, k, _, _ = case
    source = SourceRef("telegram-source", 1, "telegram", "synthetic-chat", "telegram:10001", MS)
    k._legacy_authority.register_source(source, EvidenceAudience.author_only())
    with h.snapshot() as snap:
        q = HistoryQueries(snap)
        authority = HistoryKnowledgeSources(q, {}, k.history_source_ledger, k._legacy_authority)
        with k.history_scope(q, authority):
            job = k.enqueue_capture((source,), context=TrustedCaptureContext(
                "other-channel", k.policy_revision, "observed_source_batch", (source,)),
                extractor_version="statement-capture-v1", ts_ms=MS)
    before = dict(k._store.query_one("SELECT * FROM knowledge_jobs WHERE job_id=?", (job.job_id,)))
    p = producer(k)
    handover(h, p)
    h.add("whatsapp-forward")
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert dict(k._store.query_one("SELECT * FROM knowledge_jobs WHERE job_id=?", (job.job_id,))) == before
    assert outcomes(k) == {"whatsapp-forward": "published"}


@pytest.mark.asyncio
async def test_policy_refusal_of_pending_source_is_terminal(case):
    h, k, policy, _ = case
    p = producer(k, batch_max=8, idle_ms_override=3000)
    handover(h, p)
    h.add("m", chat=GROUP)
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+1000)
    assert outcomes(k)["m"] == "pending"
    policy.engine = PolicyEngine(PolicyConfig.model_validate({
        "defaults": {"whoCanTalk": {"mode": "everyone"}},
    }), h.path.parent)
    policy.policy_revision += 1
    with h.snapshot() as snap:
        p.run_due(snap, now_ms=MS+2000)
    assert all(row["outcome"] == "not_policy_chat" for row in k._store.query(
        "SELECT outcome FROM knowledge_history_capture WHERE message_id='m'"))
    with h.snapshot() as snap:
        report = p.run_due(snap, now_ms=MS+3000)
    assert report.examined == 0


@pytest.mark.asyncio
async def test_explicit_group_entry_is_capture_opt_in_and_dm_uses_sender_defaults(case):
    h, k, policy, _ = case
    policy.engine = PolicyEngine(PolicyConfig.model_validate({
        "owners": {"whatsapp": ["10009"]},
        "defaults": {"whoCanTalk": {"mode": "owner_only"}},
        "channels": {"whatsapp": {"chats": {GROUP: {}}}},
    }), h.path.parent)
    policy.policy_revision += 1
    p = producer(k)
    handover(h, p)
    h.add("group-source", chat=GROUP)
    h.add("dm-source")
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert outcomes(k) == {"group-source": "published", "dm-source": "not_policy_chat"}
    assert statements(k) == [("group-source", "Expected statement group-source")]


def test_fake_policy_without_loaded_chat_policy_pauses_handover(case):
    from yeoman_gateway.knowledge.authority import FakePolicyAuthority
    h, k, _, _ = case
    k._policy = FakePolicyAuthority()
    with pytest.raises(HistoryPaused, match="policy_unavailable"):
        handover(h, producer(k))
    assert k._store.scalar("SELECT count(*) FROM knowledge_history_capture_state") == 0
    assert k._store.scalar("SELECT count(*) FROM knowledge_jobs") == 0


@pytest.mark.asyncio
async def test_backdated_source_uses_author_at_source_time_after_identifier_reuse(case):
    h, k, _, _ = case
    p = producer(k)
    handover(h, p)
    h.add("historical-author")
    with closing(sqlite3.connect(h.path)) as db, db:
        db.execute("UPDATE identifier_history SET valid_until_ms=? WHERE contact_id='a'", (MS+1,))
        contact(db, "new-owner")
        identifier(db, "new-owner", PHONE, start=MS+1)
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert statements(k) == [("historical-author", "Expected statement historical-author")]
    assert {row["person_id"] for row in k._store.query(
        "SELECT person_id FROM knowledge_statement_people WHERE role='speaker'")} == {"a"}
    assert k._store.scalar("SELECT author_principal FROM knowledge_statement_sources") == "whatsapp:10001"
    assert outcomes(k)["historical-author"] == "published"


@pytest.mark.asyncio
async def test_explicit_handover_refs_survive_legacy_copy_backfill_locators(case):
    h, k, _, _ = case
    h.add("processed-copy", refs=["backfill/legacy.jsonl#1"])
    h.add("pending-copy", refs=["backfill/legacy.jsonl#2"])
    processed, pending = issue(h, k, ("processed-copy", "pending-copy"))
    p = producer(k)
    handover(h, p, processed=(processed,), pending=(pending,))
    await worker(h, k, p).run_due(now_ms=MS+1000)
    assert outcomes(k) == {"processed-copy": "processed", "pending-copy": "published"}
    assert statements(k) == [("pending-copy", "Expected statement pending-copy")]
