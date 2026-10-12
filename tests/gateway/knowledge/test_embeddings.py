"""Synthetic statement production, hybrid rights and opt-in Pi search benchmark."""
from __future__ import annotations

import ast
import asyncio
import json
import math
import random
import sqlite3
import statistics
import sys
import threading
import time
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.knowledge._embedding_index import (
    _EMBEDDING_INDEX_SCHEMA,
    EMBEDDING_PREPROCESSING_VERSION,
    EmbeddingIndex,
)
from yeoman_gateway.knowledge._embeddings import KnowledgeEmbeddingWorker, embedding_status
from yeoman_gateway.knowledge.models import RecallQuery


class FakeEmbedder:
    model = "synthetic-embedder"
    dimension = 3

    def __init__(self):
        self.calls = []
        self.fail = False
        self.slow = False
        self.on_call = None

    async def embed_batch(self, texts, *, timeout=30):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("synthetic outage")
        if self.on_call:
            self.on_call()
        if self.slow:
            await asyncio.sleep(30)
        return [[1.0, 0.0, 0.0] if "orchard" in text or "apple" in text
                else [0.0, 1.0, 0.0] for text in texts]


def statement(h, text, *, private=False):
    tom = h.person("Tom")
    source = h.source(tom, author_only=private)
    return h.capture_text(text, source).statement_ids[0], source


def worker(h, fake=None):
    fake = fake or FakeEmbedder()
    return KnowledgeEmbeddingWorker(h.service._store, fake, workspace_id="test-workspace"), fake


async def test_backfill_new_and_edit_retire(knowledge_harness):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    w, fake = worker(h)
    assert await w.run_once(now_ms=1000) == 1
    assert await w.run_once(now_ms=1001) == 0
    assert len(fake.calls) == 1
    second, _ = statement(h, "new bicycle")
    assert await w.run_once(now_ms=1002) == 1
    with h.service._store.transaction() as conn:
        conn.execute("UPDATE memory2_nodes SET content='changed bicycle' WHERE id=?", (sid,))
    assert await w.run_once(now_ms=1003) == 1
    rows = h.service._store.query("SELECT node_id,status FROM memory2_embedding_index")
    assert [(r["node_id"], r["status"]) for r in rows].count((sid, "retired")) == 1
    assert sum(r["status"] == "active" for r in rows) == 2
    assert second in {r["node_id"] for r in rows}


async def test_revoke_and_supersede_retire(knowledge_harness):
    h = knowledge_harness
    sid, source = statement(h, "apple orchard")
    other, _ = statement(h, "old bicycle")
    w, _ = worker(h)
    assert await w.run_once(now_ms=1000) == 2
    h.service.invalidate_source(source, context=h.capture_context(source))
    with h.service._store.transaction() as conn:
        conn.execute("UPDATE knowledge_statements SET status='superseded',superseded_by=? WHERE statement_id=?", (sid, other))
    assert await w.run_once(now_ms=1001) == 0
    assert h.service._store.scalar("SELECT COUNT(*) FROM memory2_embedding_index WHERE status='active'") == 0


async def test_outage_backoff_and_batch_cap(knowledge_harness):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    store = h.service._store
    with store.transaction() as conn:
        for table, id_col in (("memory2_nodes", "id"), ("knowledge_statements", "statement_id")):
            original = dict(store.query_one(f"SELECT * FROM {table} WHERE {id_col}=?", (sid,)))
            cols = list(original)
            for i in range(65):
                row = dict(original)
                row[id_col] = f"synthetic-{i}"
                row["content_hash"] = f"synthetic-{i}"
                if table == "knowledge_statements":
                    row["dedupe_key"] = f"synthetic-{i}"
                conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row[c] for c in cols))
    w, fake = worker(h)
    fake.fail = True
    assert await w.run_once(now_ms=1000) == 0
    assert store.scalar("SELECT COUNT(*) FROM memory2_embedding_jobs WHERE state='failed'") == 64
    fake.fail = False
    assert await w.run_once(now_ms=1001) == 2
    assert await w.run_once(now_ms=2999) == 0
    assert await w.run_once(now_ms=3000) == 64
    assert [len(c) for c in fake.calls] == [64, 2, 64]
    status = embedding_status(store.connection)
    # The prepared-v3 fixture also contains one empty preservation statement.
    assert status["current_statements"] == 67
    assert status["profiles"][0]["vectored"] == 66
    assert status["profiles"][0]["pending"] == 1


async def test_change_during_provider_call_not_published(knowledge_harness):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    w, fake = worker(h)
    def revoke():
        with h.service._store.transaction() as conn:
            conn.execute("UPDATE knowledge_statements SET status='revoked',revoked_at_ms=1 WHERE statement_id=?", (sid,))
    fake.on_call = revoke
    assert await w.run_once(now_ms=1000) == 0
    assert h.service._store.scalar("SELECT COUNT(*) FROM memory2_embedding_index") == 0


def test_hybrid_semantic_rights_parity_and_timeout(knowledge_harness):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    private, _ = statement(h, "private apple orchard", private=True)
    tom, alex = h.person("Tom"), h.person("Alex")
    context = h.read_context(tom)
    query = RecallQuery("orchardsemantic", limit=10)
    assert h.service.recall(query, context=context, require_match=True).statement_ids == ()
    fake = FakeEmbedder()
    w = h.service.configure_embeddings(fake)
    asyncio.run(w.run_once(now_ms=1000))
    w.start()
    assert w._ready.wait(30)
    assert w._vector_cache_ready.wait(30)
    try:
        # FTS token misses, but fake query vector matches the statement vector.
        query = RecallQuery("orchardsemantic", limit=10)
        result = h.service.recall(query, context=context, require_match=True)
        assert sid in result.statement_ids
        assert sid in h.service.recall(RecallQuery("apple"), context=context).statement_ids
        before = len(fake.calls)
        w.embed("orchardsemantic")
        assert len(fake.calls) == before  # query LRU hit
        fake.slow = True
        h._raw_service._embedder = None
        baseline = h.service.recall(RecallQuery("apple timeout"), context=context, require_match=True)
        h._raw_service._embedder = w
        started = time.monotonic()
        fallback = h.service.recall(RecallQuery("apple timeout"), context=context, require_match=True)
        assert time.monotonic() - started < 1.8
        assert fallback == baseline
        fake.slow = False
        outsider = h.read_context(alex)
        assert private not in h.service.recall(query, context=outsider, require_match=True).statement_ids
        assert sid not in h.service.recall(query, context=outsider, require_match=True).statement_ids
    finally:
        asyncio.run(w.stop())


def index_store():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for sql in _EMBEDDING_INDEX_SCHEMA:
        conn.execute(sql)
    return SimpleNamespace(connection=conn, lock=threading.RLock())


def insert_vector(conn, sid, vector, *, model="synthetic-embedder", version=EMBEDDING_PREPROCESSING_VERSION, scope="scope"):
    conn.execute("INSERT INTO memory2_embedding_index "
        "(document_id,content_hash,source_revision_hash,model_id,dimension,preprocessing_version,workspace_id,scope_key,node_id,vector,created_ms) "
        "VALUES (?,'hash','hash',?,?,?,'workspace',?,?,?,0)",
        (sid, model, len(vector), version, scope, sid, array('f',vector).tobytes()))


def test_search_all_rows_ranking_mismatch_and_incremental_retirement():
    owner = index_store()
    conn = owner.connection
    index = EmbeddingIndex(owner=owner)
    insert_vector(conn, "best-old", [1,0,0], scope="another-scope")
    for i in range(300):
        insert_vector(conn, f"recent-{i}", [0.1,1,0])
    insert_vector(conn, "wrong-model", [1,0,0], model="wrong")
    insert_vector(conn, "wrong-dim", [1,0])
    insert_vector(conn, "wrong-version", [1,0,0], version="wrong")
    def search():
        return index.search_embedding_index(workspace_id="workspace", query_vector=[1,0,0], scope_keys=[], limit=1,
            model_id="synthetic-embedder", preprocessing_version=EMBEDDING_PREPROCESSING_VERSION)
    assert search()[0]["node_id"] == "best-old"
    conn.execute("UPDATE memory2_embedding_index SET status='retired' WHERE node_id='best-old'")
    assert search()[0]["node_id"].startswith("recent-")
    insert_vector(conn, "new-best", [1,0,0])
    assert search()[0]["node_id"] == "new-best"
    conn.close()


@pytest.mark.perf
def test_pi_search_benchmark():
    import resource


    owner = index_store()
    conn = owner.connection
    rng = random.Random(23)
    vectors = [array("f", (rng.uniform(-1, 1) for _ in range(1536))) for _ in range(10000)]
    conn.executemany("INSERT INTO memory2_embedding_index "
        "(document_id,content_hash,source_revision_hash,model_id,dimension,preprocessing_version,workspace_id,node_id,vector,created_ms) "
        "VALUES (?,'hash','hash','perf',1536,?,'workspace',?,?,0)",
        ((str(i),EMBEDDING_PREPROCESSING_VERSION,str(i),v.tobytes()) for i,v in enumerate(vectors)))
    index = EmbeddingIndex(owner=owner)
    def search(q):
        return index.search_embedding_index(workspace_id="workspace",query_vector=list(q),scope_keys=[],limit=12,
            model_id="perf",preprocessing_version=EMBEDDING_PREPROCESSING_VERSION)
    started = time.perf_counter()
    assert search(vectors[0])[0]["node_id"] == "0"
    build = time.perf_counter()-started
    durations = []
    for q in vectors[1:201]:
        started = time.perf_counter()
        assert search(q)
        durations.append((time.perf_counter()-started)*1000)
    blocks = next(iter(index._caches.values()))["blocks"]
    matrix_bytes = sum(sys.getsizeof(vector) + sys.getsizeof(code) + sys.getsizeof(norm)
                       for _, vectors in blocks for vector, code, norm in vectors)
    metadata_bytes = sum(sys.getsizeof(item)+sum(sys.getsizeof(v) for v in item.values()) for items,_ in blocks for item in items)
    seen = set()
    def cache_size(value):
        if id(value) in seen:
            return 0
        seen.add(id(value))
        size = sys.getsizeof(value)
        if isinstance(value, dict):
            size += sum(cache_size(k) + cache_size(v) for k, v in value.items())
        elif isinstance(value, (tuple, list)):
            size += sum(cache_size(v) for v in value)
        return size
    cache_bytes = cache_size(index._caches)
    p95 = statistics.quantiles(durations,n=20)[18]
    print(json.dumps(dict(dimension=1536,vectors=10000,build_s=build,p50_ms=statistics.median(durations),p95_ms=p95,
                         cache_vector_fingerprint_bytes=matrix_bytes,cache_metadata_bytes=metadata_bytes,
                         cache_bytes=cache_bytes,samples=len(durations),pool=128,
                         process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)),flush=True)
    assert build <= 3.0  # background warm-up; recall never waits for it
    assert p95 <= 80
    conn.close()


async def test_async_provider_batch_route_and_timeout(monkeypatch):
    import litellm
    from yeoman_gateway.knowledge._embeddings import KnowledgeEmbedder
    from yeoman_gateway.providers import litellm_provider

    provider = SimpleNamespace(api_key="synthetic",api_base="https://synthetic.invalid",
                               extra_headers={"synthetic":"test"},_resolve_model=lambda model: "openrouter/"+model)
    monkeypatch.setattr(litellm_provider, "LiteLLMProvider", lambda **kwargs: provider)
    profile = SimpleNamespace(kind="embedding",model="openai/text-embedding-3-small",provider="openrouter")
    config = SimpleNamespace(memory=SimpleNamespace(embedding=SimpleNamespace(route="memory.embed")),
        models=SimpleNamespace(routes={"memory.embed":"test"},profiles={"test":profile}),
        get_provider=lambda *args,**kwargs: provider)
    client = KnowledgeEmbedder(config)
    calls = []
    async def response(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[{"index":1,"embedding":[0,1]}, {"index":0,"embedding":[1,0]}])
    monkeypatch.setattr(litellm, "aembedding", response)
    assert await client.embed_batch(["first   text", "second text"]) == [[1,0],[0,1]]
    assert calls[0]["model"] == "openrouter/openai/text-embedding-3-small"
    assert calls[0]["input"] == ["first text", "second text"]
    assert calls[0]["encoding_format"] == "float"
    assert client.model == profile.model
    async def invalid(**kwargs):
        return SimpleNamespace(data=[{"index":0,"embedding":[1,0]}, {"index":0,"embedding":[0,1]}])
    monkeypatch.setattr(litellm, "aembedding", invalid)
    with pytest.raises(ValueError):
        await client.embed_batch(["first", "second"])
    async def slow(**kwargs):
        await asyncio.sleep(30)
    monkeypatch.setattr(litellm, "aembedding", slow)
    with pytest.raises(TimeoutError):
        await client.embed_batch(["synthetic"], timeout=0.01)
    with pytest.raises(ValueError):
        await client.embed_batch(["synthetic"]*65)


async def test_cli_status_is_read_only(knowledge_harness):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    h = knowledge_harness
    statement(h, "apple orchard")
    w, _ = worker(h)
    await w.run_once(now_ms=1000)
    store = h.service._store
    before = store.connection.total_changes
    result = CliRunner().invoke(knowledge_app, ["embeddings","status","--target",str(store.db_path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["profiles"][0]["vectored"] == 1
    assert store.connection.total_changes == before
    # The status reader cannot create a missing database.
    absent = store.db_path.parent / "absent.db"
    result = CliRunner().invoke(knowledge_app, ["embeddings","status","--target",str(absent)])
    assert result.exit_code != 0
    assert not absent.exists()


def test_retired_vectors_leave_cache_and_reactivation_loads_latest_value():
    owner = index_store()
    conn = owner.connection
    index = EmbeddingIndex(owner=owner)
    insert_vector(conn, "first", [1,0,0])
    insert_vector(conn, "second", [0.5,1,0])
    def search():
        return index.search_embedding_index(workspace_id="workspace", query_vector=[1,0,0], scope_keys=[],
            model_id="synthetic-embedder", preprocessing_version=EMBEDDING_PREPROCESSING_VERSION)
    assert search()[0]["node_id"] == "first"
    conn.execute("UPDATE memory2_embedding_index SET status='retired' WHERE node_id='first'")
    assert search()[0]["node_id"] == "second"
    blocks = next(iter(index._caches.values()))["blocks"]
    assert sum(len(items) for items,_ in blocks) == 1
    conn.execute("UPDATE memory2_embedding_index SET status='active',vector=? WHERE node_id='first'", (array('f',[0,1,0]).tobytes(),))
    assert search()[0]["node_id"] == "second"
    conn.close()


async def test_hybrid_keeps_caller_result_limit(knowledge_harness):
    h = knowledge_harness
    for i in range(3):
        statement(h, f"apple orchard {i}")
    w, fake = worker(h)
    await w.run_once(now_ms=1000)
    h._raw_service._embedder = SimpleNamespace(model=fake.model, embed=lambda text: [1,0,0])
    context = h.read_context(h.person("Tom"))
    result = h.service.recall(RecallQuery("orchardsemantic",limit=1), context=context, require_match=True)
    assert len(result.statement_ids) == 1


async def test_idle_signature_skips_current_and_writes(knowledge_harness, monkeypatch):
    h = knowledge_harness
    statement(h, "apple orchard")
    w, fake = worker(h)
    calls = []
    original = w._current
    def current():
        calls.append(True)
        return original()
    monkeypatch.setattr(w, "_current", current)
    assert await w.run_once(now_ms=1000) == 1
    assert len(calls) == 1  # unchanged post-provider inputs reuse the snapshot
    def forbidden(*args, **kwargs):
        pytest.fail("idle pass read content or opened a write transaction")
    monkeypatch.setattr(w, "_current", forbidden)
    monkeypatch.setattr(w.store, "transaction", forbidden)
    assert await w.run_once(now_ms=1001) == 0
    assert len(fake.calls) == 1


@pytest.mark.parametrize("mutation", ["revoke", "supersede", "edit", "delete"])
async def test_input_signature_detects_lifecycle_changes(knowledge_harness, mutation):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    w, _ = worker(h)
    assert await w.run_once(now_ms=1000) == 1
    before = w._signature()
    with w.store.transaction() as conn:
        if mutation == "revoke":
            conn.execute("UPDATE knowledge_statements SET status='revoked',revoked_at_ms=1 WHERE statement_id=?", (sid,))
        elif mutation == "supersede":
            conn.execute("UPDATE knowledge_statements SET status='superseded',superseded_by='replacement' WHERE statement_id=?", (sid,))
        elif mutation == "edit":
            # Same-length content change without timestamp/hash updates must invalidate.
            conn.execute("UPDATE memory2_nodes SET content='other orchard' WHERE id=?", (sid,))
        else:
            conn.execute("UPDATE memory2_nodes SET is_deleted=1 WHERE id=?", (sid,))
    assert w._signature() != before
    assert await w.run_once(now_ms=1001) == (1 if mutation == "edit" else 0)
    assert w.store.scalar("SELECT COUNT(*) FROM memory2_embedding_index WHERE status='retired'") == 1


async def test_external_connection_edit_invalidates_signature(knowledge_harness):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    w, _ = worker(h)
    await w.run_once(now_ms=1000)
    before = w._signature()
    with sqlite3.connect(w.store.db_path) as conn:
        conn.execute("UPDATE memory2_nodes SET content='other orchard' WHERE id=?", (sid,))
    assert w._signature() != before
    assert await w.run_once(now_ms=1001) == 1


async def test_worker_idle_backoff_and_due_work(knowledge_harness, monkeypatch):
    h = knowledge_harness
    statement(h, "apple orchard")
    w, _ = worker(h)
    w.idle_seconds = 37.0
    delays = []
    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._work()
    assert delays == [w.poll_seconds, 37.0]


async def test_due_retry_reuses_inputs_without_reconciliation(knowledge_harness, monkeypatch):
    h = knowledge_harness
    statement(h, "apple orchard")
    w, fake = worker(h)
    fake.fail = True
    assert await w.run_once(now_ms=1000) == 0
    def forbidden(*args, **kwargs):
        pytest.fail("unchanged retry reloaded or reconciled inputs")
    monkeypatch.setattr(w, "_current", forbidden)
    monkeypatch.setattr(w, "_reconcile", forbidden)
    assert await w.run_once(now_ms=1001) == 0
    fake.fail = False
    assert await w.run_once(now_ms=3000) == 1


async def test_backoff_preserves_soon_due_retry(knowledge_harness, monkeypatch):
    h = knowledge_harness
    statement(h, "apple orchard")
    w, fake = worker(h)
    fake.fail = True
    delays = []
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    async def sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._work()
    assert delays == [w.poll_seconds]


@pytest.mark.perf
async def test_pi_idle_pass_benchmark(knowledge_harness, monkeypatch):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard " + "synthetic text " * 15)
    store = h.service._store
    with store.transaction() as conn:
        for table, id_col in (("memory2_nodes", "id"), ("knowledge_statements", "statement_id")):
            original = dict(store.query_one(f"SELECT * FROM {table} WHERE {id_col}=?", (sid,)))
            cols = list(original)
            records = []
            for i in range(6999):
                row = dict(original)
                row[id_col] = f"idle-{i}"
                row["content_hash"] = f"idle-{i}"
                if table == "knowledge_statements":
                    row["dedupe_key"] = f"idle-{i}"
                else:
                    row["content"] += f" {i}"
                records.append(tuple(row[c] for c in cols))
            conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", records)
    w, _ = worker(h)
    started = time.perf_counter()
    current = w._current()
    uncached_ms = (time.perf_counter() - started) * 1000
    assert len(current) == 7000
    with store.transaction() as conn:
        conn.executemany("INSERT INTO memory2_embedding_index "
            "(document_id,content_hash,source_revision_hash,model_id,dimension,preprocessing_version,workspace_id,node_id,vector,created_ms) "
            "VALUES (?,?,?,?,3,?,'test-workspace',?,?,0)",
            ((sid,row["embedding_hash"],row["embedding_hash"],w.model,EMBEDDING_PREPROCESSING_VERSION,sid,array('f',[1,0,0]).tobytes())
             for sid,row in current.items()))
        conn.executemany("INSERT INTO memory2_embedding_jobs "
            "(job_key,workspace_id,node_id,model_id,dimension,preprocessing_version,state,due_ms,created_ms,updated_ms) "
            "VALUES (?,'test-workspace',?,?,3,?,'done',0,0,0)",
            ((sid,sid,w.model,EMBEDDING_PREPROCESSING_VERSION) for sid in current))
    assert await w.run_once(now_ms=1000) == 0  # prepare/cache once
    def forbidden(*args, **kwargs):
        pytest.fail("idle benchmark read content or opened a write transaction")
    monkeypatch.setattr(w, "_current", forbidden)
    monkeypatch.setattr(store, "transaction", forbidden)
    times = []
    for _ in range(100):
        started = time.perf_counter()
        assert await w.run_once(now_ms=1001) == 0
        times.append((time.perf_counter() - started) * 1000)
    print(json.dumps(dict(statements=7000,completed_jobs=7000,uncached_current_ms=uncached_ms,
                         idle_median_ms=statistics.median(times),idle_p95_ms=statistics.quantiles(times,n=20)[18],
                         idle_max_ms=max(times),samples=100,idle_current_calls=0,idle_write_transactions=0)),flush=True)


async def test_changed_provider_inputs_keep_short_interval(knowledge_harness, monkeypatch):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    w, fake = worker(h)
    def edit():
        with w.store.transaction() as conn:
            conn.execute("UPDATE memory2_nodes SET content='other orchard' WHERE id=?", (sid,))
    fake.on_call = edit
    delays = []
    async def sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._work()
    assert delays == [w.poll_seconds]


def test_synthetic_ranking_matches_exact_cosine():
    owner = index_store()
    rng = random.Random(17)
    vectors = [array("f", (rng.uniform(-1, 1) for _ in range(16))) for _ in range(40)]
    query = vectors[7]
    for i, vector in enumerate(vectors):
        insert_vector(owner.connection, str(i), vector)
    expected = sorted(range(len(vectors)), key=lambda i: (
        math.sumprod(vectors[i], query) / math.sqrt(math.sumprod(vectors[i], vectors[i]) *
                                                  math.sumprod(query, query)), str(i)), reverse=True)[:10]
    result = EmbeddingIndex(owner=owner).search_embedding_index(
        workspace_id="workspace", query_vector=list(query), scope_keys=[], limit=10)
    assert [row["node_id"] for row in result] == [str(i) for i in expected]
    owner.connection.close()


def test_no_module_imports_numpy():
    root = Path(__file__).resolve().parents[3]
    for folder in ("packages", "tests"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    assert all(alias.name.split(".")[0] != "numpy" for alias in node.names), path
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] != "numpy", path


def test_fingerprints_match_stored_and_query_float32_including_signed_zero():
    from yeoman_gateway.knowledge._embedding_index import _fingerprint

    values = [0.0, -0.0, 1.0, -1.0, 1e-46, -1e-46]
    published = array("f", values).tobytes()
    query = array("f", list(array("f", published))).tobytes()
    assert _fingerprint(published) == _fingerprint(query)
    assert _fingerprint(array("f", [0.0]).tobytes()) == 0
    assert _fingerprint(array("f", [-0.0]).tobytes()) == 1
    owner = index_store()
    insert_vector(owner.connection, "zeros", values)
    result = EmbeddingIndex(owner=owner).search_embedding_index(
        workspace_id="workspace", query_vector=values, scope_keys=[])
    assert result[0]["score"] == pytest.approx(1.0)
    owner.connection.close()


def test_first_recall_during_cache_warming_is_lexical_and_does_not_wait(knowledge_harness, monkeypatch):
    h = knowledge_harness
    sid, _ = statement(h, "apple orchard")
    context = h.read_context(h.person("Tom"))
    query = RecallQuery("apple")
    baseline = h.service.recall(query, context=context, require_match=True)
    w = h.service.configure_embeddings(FakeEmbedder())
    entered, release = threading.Event(), threading.Event()
    original = w._warm_cache
    def blocked_warm():
        entered.set()
        assert release.wait(30)
        original()
    monkeypatch.setattr(w, "_warm_cache", blocked_warm)
    def forbidden(*args, **kwargs):
        pytest.fail("warming recall attempted vector generation/search")
    monkeypatch.setattr(w, "embed", forbidden)
    reasons = []
    monkeypatch.setattr(w, "_reason", reasons.append)
    w.start()
    try:
        assert entered.wait(30)
        started = time.monotonic()
        result = h.service.recall(query, context=context, require_match=True)
        assert time.monotonic() - started < 0.5
        assert result == baseline
        assert sid in result.statement_ids
        assert "vector_cache_warming" in reasons
        assert not w._vector_cache_ready.is_set()
    finally:
        release.set()
        asyncio.run(w.stop())
