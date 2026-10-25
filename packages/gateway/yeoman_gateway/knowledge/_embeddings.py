"""Rebuildable statement vectors; provider work stays off capture and reply loops."""
from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
from array import array
from collections import OrderedDict
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from typing import Any

from loguru import logger

from yeoman_gateway.knowledge._embedding_index import (
    _EMBEDDING_INDEX_SCHEMA,
    EMBEDDING_PREPROCESSING_VERSION,
    EmbeddingIndex,
)
from yeoman_gateway.knowledge._retrieval import CURRENT_STATEMENT_SQL


def embedding_text(text: str) -> str:
    return " ".join(text.split())


def content_hash(text: str) -> str:
    return hashlib.sha256(embedding_text(text).encode()).hexdigest()


class KnowledgeEmbedder:
    """The existing routed LiteLLM provider, with async batched embedding calls."""

    def __init__(self, config: Any) -> None:
        from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

        route = config.memory.embedding.route or "memory.embed"
        profile = config.models.profiles[config.models.routes[route]]
        if profile.kind != "embedding" or not profile.model:
            raise ValueError("embedding_route_invalid")
        cfg = config.get_provider(profile.model, provider_name=profile.provider)
        if cfg is None:
            raise ValueError("embedding_provider_unavailable")
        self.model = profile.model
        self._requested_dimension = getattr(profile, "dimensions", None)
        self.dimension = self._requested_dimension or 1536
        self._provider = LiteLLMProvider(api_key=cfg.api_key or None, api_base=cfg.api_base,
                                        default_model=self.model, extra_headers=cfg.extra_headers)

    async def embed_batch(self, texts: list[str], *, timeout: float = 30.0) -> list[list[float]]:
        from litellm import aembedding

        if not 1 <= len(texts) <= 64:
            raise ValueError("embedding_batch_invalid")
        provider = self._provider
        kwargs: dict[str, Any] = dict(model=provider._resolve_model(self.model),
                                     input=[embedding_text(text) for text in texts],
                                     encoding_format="float", timeout=timeout)
        if self._requested_dimension:
            kwargs["dimensions"] = self._requested_dimension
        for name in ("api_key", "api_base", "extra_headers"):
            value = getattr(provider, name)
            if value:
                kwargs[name] = value
        async with asyncio.timeout(timeout):
            response = await aembedding(**kwargs)
        data = response.data
        rows = sorted(data, key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("embedding_response_invalid")
        return [row["embedding"] for row in rows]


class KnowledgeEmbeddingWorker:
    """One dedicated async loop, durable retries and a bounded query LRU."""

    def __init__(self, store: Any, embedder: Any, *, workspace_id: str,
                 poll_seconds: float = 2.0, query_timeout: float = 1.5,
                 idle_seconds: float = 30.0, embedding_index: EmbeddingIndex | None = None) -> None:
        self.store, self.embedder = store, embedder
        # Fresh schema-3 stores omit these legacy tables; retain their exact existing DDL.
        with store.transaction() as conn:
            for sql in _EMBEDDING_INDEX_SCHEMA:
                conn.execute(sql)
        self.workspace_id = workspace_id
        self._embedding_index = embedding_index or EmbeddingIndex(owner=store)
        self._vector_cache_ready = threading.Event()
        self.model, self.dimension = embedder.model, embedder.dimension
        self.poll_seconds = poll_seconds
        self.idle_seconds = idle_seconds
        self._current_signature: tuple[int, int] | None = None
        self._prepared_signature: tuple[int, int] | None = None
        self._current_rows: dict[str, dict[str, Any]] = {}
        self._job_keys: set[str] = set()
        self.query_timeout = min(1.5, query_timeout)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._cache_lock = threading.Lock()
        self._queries: OrderedDict[str, list[float]] = OrderedDict()
        self._logged: set[str] = set()

    def _reason(self, code: str) -> None:
        with self._cache_lock:
            if code in self._logged:
                return
            self._logged.add(code)
        logger.warning("knowledge embeddings reason={}", code)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="knowledge-embeddings", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        task = loop.create_task(self._work())
        self._ready.set()
        try:
            try:
                self._warm_cache()
            except Exception:
                self._reason("vector_cache_warm_failed")
            loop.run_forever()
        finally:
            task.cancel()
            for pending in asyncio.all_tasks(loop):
                pending.cancel()
            loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
            loop.close()

    def _warm_cache(self) -> None:
        self._embedding_index.warm_cache(
            workspace_id=self.workspace_id, model_id=self.model, dimension=self.dimension,
            preprocessing_version=EMBEDDING_PREPROCESSING_VERSION)
        self._vector_cache_ready.set()

    @property
    def vector_cache_ready(self) -> bool:
        ready = self._vector_cache_ready.is_set()
        if not ready:
            self._reason("vector_cache_warming")
        return ready

    async def stop(self) -> None:
        if self._thread is None:
            return
        await asyncio.to_thread(self._ready.wait, 30)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        await asyncio.to_thread(self._thread.join, 30)
        self._thread = None
        self._loop = None
        self._ready.clear()
        self._vector_cache_ready.clear()

    async def _work(self) -> None:
        with self._write() as conn:
            conn.execute("UPDATE memory2_embedding_jobs SET state='queued' WHERE state='running' AND workspace_id=? AND model_id=?",
                         (self.workspace_id, self.model))
        while True:
            delay = self.poll_seconds
            try:
                published = await self.run_once()
                if not published and self._signature() == self._prepared_signature:
                    due = self._next_due_ms()
                    delay = self.idle_seconds if due is None else min(
                        self.idle_seconds, max(self.poll_seconds, due / 1000 - time.time()))
            except Exception:
                self._reason("worker_failed")
            await asyncio.sleep(delay)

    def embed(self, text: str) -> list[float] | None:
        """Synchronous recall boundary; async I/O runs on the worker's dedicated loop."""
        if not self.vector_cache_ready:
            return None
        text = embedding_text(text)
        with self._cache_lock:
            if text in self._queries:
                self._queries.move_to_end(text)
                return list(self._queries[text])
        loop = self._loop
        if loop is None or not loop.is_running():
            self._reason("query_worker_unavailable")
            return None
        future = asyncio.run_coroutine_threadsafe(
            self.embedder.embed_batch([text], timeout=self.query_timeout), loop)
        try:
            vectors = future.result(timeout=self.query_timeout)
            self._validate(vectors, 1)
        except FutureTimeout:
            future.cancel()
            self._reason("query_timeout")
            return None
        except Exception:
            future.cancel()
            self._reason("query_provider_failed")
            return None
        with self._cache_lock:
            self._queries[text] = vectors[0]
            self._queries.move_to_end(text)
            while len(self._queries) > 128:
                self._queries.popitem(last=False)
        return list(vectors[0])

    def _validate(self, vectors: list[list[float]], count: int) -> None:
        if len(vectors) != count or any(len(v) != self.dimension or
                not all(math.isfinite(x) for x in v) or not any(v) for v in vectors):
            raise ValueError("embedding_response_invalid")

    def _current(self) -> dict[str, dict[str, Any]]:
        rows = self.store.query(
            "SELECT s.statement_id, s.scope_key, s.source_channel, s.source_chat_id, n.content "
            "FROM knowledge_statements s JOIN memory2_nodes n ON n.id=s.statement_id "
            f"WHERE s.workspace_id=? AND {CURRENT_STATEMENT_SQL} AND n.is_deleted=0",
            (self.workspace_id,))
        return {row["statement_id"]: dict(row, embedding_hash=content_hash(row["content"]))
                for row in rows if embedding_text(row["content"])}

    def _signature(self) -> tuple[int, int]:
        # Unlike timestamps, these markers also cover unmarked content edits/deletes.
        with self.store.lock:
            conn = self.store.connection
            return conn.total_changes, int(conn.execute("PRAGMA data_version").fetchone()[0])

    @contextmanager
    def _write(self) -> Iterator[Any]:
        """Advance cached signatures for our bookkeeping, never for another writer."""
        with self.store.lock:
            before = self._signature()
            with self.store.transaction() as conn:
                yield conn
            after = self._signature()
            if before[1] == after[1]:
                if self._current_signature == before:
                    self._current_signature = after
                if self._prepared_signature == before:
                    self._prepared_signature = after

    def _cached_current(self) -> dict[str, dict[str, Any]]:
        with self.store.lock:
            signature = self._signature()
            if signature != self._current_signature:
                self._current_rows = self._current()
                self._current_signature = signature
            return self._current_rows

    def _next_due_ms(self) -> int | None:
        row = self.store.query_one(
            "SELECT MIN(due_ms) FROM memory2_embedding_jobs WHERE workspace_id=? "
            "AND model_id=? AND dimension=? AND preprocessing_version=? AND state IN ('queued','failed')",
            (self.workspace_id, self.model, self.dimension, EMBEDDING_PREPROCESSING_VERSION))
        return None if row is None or row[0] is None else int(row[0])

    def _due_jobs(self, now: int) -> list[Any]:
        return self.store.query(
            "SELECT * FROM memory2_embedding_jobs WHERE workspace_id=? AND model_id=? "
            "AND dimension=? AND preprocessing_version=? AND state IN ('queued','failed') "
            "AND due_ms<=? ORDER BY due_ms LIMIT 64",
            (self.workspace_id, self.model, self.dimension, EMBEDDING_PREPROCESSING_VERSION, now))

    def _reconcile(self, current: dict[str, dict[str, Any]], now: int) -> None:
        version = EMBEDDING_PREPROCESSING_VERSION
        active = self.store.query(
            "SELECT rowid,node_id,content_hash,model_id,dimension,preprocessing_version "
            "FROM memory2_embedding_index WHERE workspace_id=? AND status='active'",
            (self.workspace_id,))
        retired, existing = [], set()
        for row in active:
            statement = current.get(row["node_id"])
            if statement is None or statement["embedding_hash"] != row["content_hash"]:
                retired.append((now, row["rowid"]))
            elif (row["model_id"], row["dimension"], row["preprocessing_version"]) == (self.model, self.dimension, version):
                existing.add(row["node_id"])
        keys, queued = set(), []
        for sid, row in current.items():
            key = hashlib.sha256(f"{sid}:{row['embedding_hash']}:{self.model}:{self.dimension}:{version}".encode()).hexdigest()
            keys.add(key)
            if sid not in existing:
                queued.append((key,self.workspace_id,sid,row["scope_key"],self.model,self.dimension,version,row["embedding_hash"],now,now,now))
        cancelled = [(now, row[0]) for row in self.store.query(
            "SELECT job_key FROM memory2_embedding_jobs WHERE workspace_id=? AND model_id=? "
            "AND state IN ('queued','failed','running')", (self.workspace_id,self.model)) if row[0] not in keys]
        reset = [(now,now,row[0]) for row in queued]
        self._job_keys = keys
        self._prepared_signature = self._current_signature
        if not retired and not queued and not cancelled:
            return
        with self._write() as conn:
            conn.executemany("UPDATE memory2_embedding_index SET status='retired',retired_ms=? WHERE rowid=?", retired)
            conn.executemany("INSERT OR IGNORE INTO memory2_embedding_jobs "
                "(job_key,workspace_id,node_id,scope_key,model_id,dimension,preprocessing_version,source_revision_hash,state,due_ms,created_ms,updated_ms) "
                "VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?)", queued)
            conn.executemany("UPDATE memory2_embedding_jobs SET state='queued',due_ms=?,updated_ms=? "
                "WHERE job_key=? AND state IN ('done','cancelled')", reset)
            conn.executemany("UPDATE memory2_embedding_jobs SET state='cancelled',updated_ms=? WHERE job_key=?", cancelled)

    async def run_once(self, *, now_ms: int | None = None) -> int:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        version = EMBEDDING_PREPROCESSING_VERSION
        if self._signature() != self._prepared_signature:
            current = self._cached_current()
            self._reconcile(current, now)
        jobs = [job for job in self._due_jobs(now) if job["job_key"] in self._job_keys]
        if not jobs:
            return 0
        current = self._cached_current()
        with self._write() as conn:
            conn.executemany("UPDATE memory2_embedding_jobs SET state='running',attempts=attempts+1,updated_ms=? WHERE job_key=?",
                             [(now,job["job_key"]) for job in jobs])
        try:
            vectors = await self.embedder.embed_batch([current[job["node_id"]]["content"] for job in jobs], timeout=30.0)
            self._validate(vectors, len(jobs))
        except Exception:
            self._reason("producer_provider_failed")
            retries = [(now + min(300_000, 2_000 * 2 ** min(8, job["attempts"])),now,job["job_key"]) for job in jobs]
            with self._write() as conn:
                conn.executemany("UPDATE memory2_embedding_jobs SET state='failed',reason='provider_failed',due_ms=?,updated_ms=? WHERE job_key=?", retries)
            return 0
        blobs = [array('f',vector).tobytes() for vector in vectors]
        published = 0
        # Keep shared-connection writers out between the final recheck and publication.
        with self.store.lock:
            fresh = self._cached_current()
            with self._write() as conn:
                for job, blob in zip(jobs, blobs, strict=True):
                    sid = job["node_id"]
                    row = fresh.get(sid)
                    if row is None or row["embedding_hash"] != job["source_revision_hash"]:
                        conn.execute("UPDATE memory2_embedding_jobs SET state='cancelled',updated_ms=? WHERE job_key=?", (now,job["job_key"]))
                        continue
                    conn.execute("INSERT INTO memory2_embedding_index "
                        "(document_id,content_hash,source_revision_hash,model_id,dimension,preprocessing_version,workspace_id,scope_key,node_id,vector,status,created_ms) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'active',?) "
                        "ON CONFLICT DO UPDATE SET status='active',retired_ms=NULL,vector=excluded.vector",
                        (sid,row["embedding_hash"],row["embedding_hash"],self.model,self.dimension,version,self.workspace_id,row["scope_key"],sid,blob,now))
                    conn.execute("UPDATE memory2_embedding_jobs SET state='done',reason=NULL,updated_ms=? WHERE job_key=?", (now,job["job_key"]))
                    published += 1
        return published


def embedding_status(conn: Any) -> dict[str, Any]:
    """Aggregate counters on a caller-owned read-only snapshot; never schedules work."""
    current = {row[0]: content_hash(row[1]) for row in conn.execute(
        "SELECT s.statement_id,n.content FROM knowledge_statements s "
        "JOIN memory2_nodes n ON n.id=s.statement_id "
        f"WHERE {CURRENT_STATEMENT_SQL} AND n.is_deleted=0")}
    profiles = list(conn.execute(
        "SELECT model_id,dimension,preprocessing_version FROM memory2_embedding_index "
        "UNION SELECT model_id,dimension,preprocessing_version FROM memory2_embedding_jobs"))
    results = []
    for model, dimension, version in profiles:
        vectored = {sid for sid, digest in conn.execute(
            "SELECT node_id,content_hash FROM memory2_embedding_index WHERE status='active' "
            "AND model_id=? AND dimension=? AND preprocessing_version=?", (model,dimension,version))
            if current.get(sid) == digest}
        failed = {sid for sid, digest in conn.execute(
            "SELECT node_id,source_revision_hash FROM memory2_embedding_jobs WHERE state='failed' "
            "AND model_id=? AND dimension=? AND preprocessing_version=?", (model,dimension,version))
            if current.get(sid) == digest and sid not in vectored}
        retired = conn.execute("SELECT COUNT(*) FROM memory2_embedding_index WHERE status='retired' "
            "AND model_id=? AND dimension=? AND preprocessing_version=?", (model,dimension,version)).fetchone()[0]
        results.append(dict(model=model,dimension=dimension,preprocessing_version=version,
                            vectored=len(vectored),pending=len(current)-len(vectored)-len(failed),
                            failed=len(failed),retired=retired))
    return dict(current_statements=len(current), profiles=results)
