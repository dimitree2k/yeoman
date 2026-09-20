"""Embedding client and the versioned, asynchronous embedding index (Phase 2 / Task 2).

Text becomes searchable lexically *before* a provider is involved; the provider only ever
sees bounded, source-linked sections; and every published vector carries the full document
key ``(document_id, content_hash, source_revision_hash, model_id, dimension,
preprocessing_version)`` so two models, two dimensions or two preprocessing versions can
never be compared.  Vectors are a rebuildable index, never memory truth.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

from loguru import logger

from yeoman_gateway.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from yeoman_shared.config.schema import Config, ModelProfile

#: Bump when the text normalization that feeds the provider changes.  It is part of the
#: document key, so a bump re-indexes instead of silently mixing two preprocessings.
EMBEDDING_PREPROCESSING_VERSION = "memory-text-v1"

#: Hard upper bound for one embedded section.  Sections never span a source, an audience
#: or a chat boundary: they are produced per source text, so those boundaries are
#: structural rather than checked afterwards.
MAX_EMBEDDING_SECTION_CHARS = 2000

#: Job states reused from the existing durable extraction machinery.
JOB_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "done",
    "skipped",
    "cancelled",
    "failed",
)


class MemoryEmbeddingService:
    """Resolve embedding route and fetch vectors via LiteLLM."""

    def __init__(self, *, config: "Config", route_key: str) -> None:
        self._config = config
        self._route_key = route_key
        self._profile = self._resolve_profile()
        self._model = (self._profile.model or "").strip()
        self._provider = self._create_provider(self._model, self._profile.provider)

    @property
    def model(self) -> str:
        return self._model

    def _resolve_profile(self) -> "ModelProfile":
        route_name = self._config.models.routes.get(self._route_key)
        if not route_name:
            raise ValueError(f"models.routes missing '{self._route_key}'")
        profile = self._config.models.profiles.get(route_name)
        if profile is None:
            raise ValueError(
                f"models.routes['{self._route_key}'] points to missing profile '{route_name}'"
            )
        if profile.kind != "embedding":
            raise ValueError(
                f"route '{self._route_key}' must target kind='embedding', got '{profile.kind}'"
            )
        if not (profile.model or "").strip():
            raise ValueError(f"profile '{route_name}' does not define a model")
        return profile

    def _create_provider(self, model: str, provider_name: str | None) -> LiteLLMProvider:
        provider_cfg = self._config.get_provider(model, provider_name=provider_name)
        if provider_cfg is None:
            raise ValueError(
                f"no provider with credentials for embedding route '{self._route_key}' "
                f"(model={model!r}, provider={provider_name or 'auto'!r})"
            )
        api_key = provider_cfg.api_key if provider_cfg.api_key else None
        api_base = provider_cfg.api_base
        extra_headers = provider_cfg.extra_headers
        return LiteLLMProvider(
            api_key=api_key,
            api_base=api_base,
            default_model=model,
            extra_headers=extra_headers,
        )

    def embed(self, text: str) -> list[float] | None:
        compact = " ".join(text.split()).strip()
        if not compact:
            return None

        try:
            from litellm import embedding

            model = self._provider._resolve_model(self._model)
            # encoding_format="float" is required by OpenRouter's /v1/embeddings
            # schema (Zod-validated; rejects the call when missing). OpenAI's
            # native endpoint accepts it as well, so this is safe across gateways.
            kwargs: dict[str, Any] = {
                "model": model,
                "input": [compact],
                "encoding_format": "float",
            }
            if self._provider.api_key:
                kwargs["api_key"] = self._provider.api_key
            if self._provider.api_base:
                kwargs["api_base"] = self._provider.api_base
            if self._provider.extra_headers:
                kwargs["extra_headers"] = self._provider.extra_headers
            response = embedding(**kwargs)
            data = getattr(response, "data", None)
            if not data:
                return None
            vector = data[0].get("embedding") if isinstance(data[0], dict) else None
            if vector is None:
                vector = getattr(data[0], "embedding", None)
            if not isinstance(vector, list):
                return None
            return [float(v) for v in vector]
        except Exception as exc:
            logger.debug("memory embedding failed: {}", exc)
            return None


# ── sectioning ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceSection:
    """One bounded, non-overlapping slice of a single source text."""

    index: int
    start: int
    end: int
    text: str


def chunk_source_text(
    text: str, *, max_chars: int = MAX_EMBEDDING_SECTION_CHARS
) -> tuple[SourceSection, ...]:
    """Split one source text into consecutive sections of at most ``max_chars``.

    Offsets are exact and the slices are contiguous and non-overlapping, so a rendered
    hit can always be traced back to its position in the source.  The function is called
    once per (source, audience, chat) text, which is what keeps a section from spanning a
    source, audience or chat boundary.
    """
    limit = max(1, int(max_chars))
    if not text:
        return ()
    sections: list[SourceSection] = []
    start = 0
    index = 0
    total = len(text)
    while start < total:
        end = min(start + limit, total)
        if end < total:
            # Prefer a whitespace boundary so a word is not cut in half.
            window = text.rfind(" ", start, end)
            if window > start:
                end = window + 1
        sections.append(SourceSection(index=index, start=start, end=end, text=text[start:end]))
        index += 1
        start = end
    return tuple(sections)


# ── document identity ────────────────────────────────────────────────────────


def source_revision_hash(
    *,
    event_id: str,
    revision: int,
    content_hash: str,
    audience_fingerprint: str = "",
) -> str:
    """Identity of one source revision *including* its audience.

    A changed source, source revision or audience therefore yields a different index
    identity instead of quietly reusing vectors computed under other rights.
    """
    raw = json.dumps(
        [str(event_id), int(revision), str(content_hash), str(audience_fingerprint)],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def audience_fingerprint(audience: object | None) -> str:
    """A stable fingerprint of a stored audience snapshot.  Unknown is not a fingerprint."""
    if audience is None:
        return ""
    if isinstance(audience, dict):
        status = str(audience.get("status", "unknown"))
        members = audience.get("members") or ()
        snapshot_id = audience.get("snapshot_id")
    else:
        status = str(getattr(audience, "status", "unknown"))
        members = getattr(audience, "members", ()) or ()
        snapshot_id = getattr(audience, "snapshot_id", None)
    raw = json.dumps(
        [status, sorted({str(item) for item in members}), str(snapshot_id or "")],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def embedding_document_key(
    *,
    document_id: str,
    content_hash: str,
    source_revision_hash_value: str,
    model_id: str,
    dimension: int,
    preprocessing_version: str,
) -> tuple[str, str, str, str, int, str]:
    return (
        str(document_id),
        str(content_hash),
        str(source_revision_hash_value),
        str(model_id),
        int(dimension),
        str(preprocessing_version),
    )


def embedding_job_key(
    *,
    node_id: str,
    source_revision_hash_value: str,
    model_id: str,
    dimension: int,
    preprocessing_version: str,
) -> str:
    """One durable job per (node, source revision, model, dimension, preprocessing)."""
    raw = json.dumps(
        [
            str(node_id),
            str(source_revision_hash_value),
            str(model_id),
            int(dimension),
            str(preprocessing_version),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def section_document_id(node_id: str, section_index: int) -> str:
    return f"emb:{node_id}:s{int(section_index)}"


# ── the async index worker ───────────────────────────────────────────────────


@dataclass(slots=True)
class EmbeddingRunReport:
    """What one ``run_due`` pass did.  Reasons are counted, never hidden."""

    processed: int = 0
    published: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class MemoryEmbeddingQueue:
    """Durable embedding jobs on the existing store, driven by the existing worker shape.

    The queue is deliberately not a new framework: it is the same bounded, durable,
    ``run_due``-driven state machine the shared-fact extraction queue already uses, and it
    writes into the same SQLite file through the same store owner.
    """

    def __init__(
        self,
        *,
        store: Any,
        embedder: Any,
        authority: Any | None = None,
        clock: Callable[[], int] | None = None,
        preprocessing_version: str = EMBEDDING_PREPROCESSING_VERSION,
        dimensions: int | None = None,
        max_attempts: int = 3,
        retry_delay_ms: int = 60_000,
    ) -> None:
        self._store = store
        self._embedder = embedder
        # Prefer an explicitly supplied proof owner; otherwise use the one the store was
        # opened with, so a store that already has live authority also gates its vectors.
        self._authority = (
            authority if authority is not None else getattr(store, "source_authority", None)
        )
        self._clock = clock if clock is not None else (lambda: int(time.time() * 1000))
        self._preprocessing_version = str(preprocessing_version)
        self._dimensions = int(dimensions) if dimensions else None
        self._max_attempts = max(1, int(max_attempts))
        self._retry_delay_ms = max(1_000, int(retry_delay_ms))
        #: Observable totals, so the queue that owns this worker can report them.
        self.published_sections = 0
        self.failed_jobs = 0

    # -- identity ---------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return str(getattr(self._embedder, "model", "") or "unknown")

    @property
    def dimension(self) -> int:
        """The model's vector width: configured, else learned from a publish, else 0.

        A width of 0 means "not known yet"; the first successful publish records it, so
        the job key stops being dimension-blind instead of silently accepting whatever the
        provider returns.
        """
        if self._dimensions:
            return self._dimensions
        configured = int(getattr(self._embedder, "dims", 0) or 0)
        if configured:
            return configured
        reader = getattr(self._store, "embedding_dimension", None)
        if callable(reader):
            return int(reader(self.model_id) or 0)
        return 0

    @property
    def preprocessing_version(self) -> str:
        return self._preprocessing_version

    # -- queue ------------------------------------------------------------------

    @property
    def waiting(self) -> int:
        return int(self._store.count_embedding_jobs(state="queued")) + int(
            self._store.count_embedding_jobs(state="running")
        )

    def enqueue_node(
        self,
        entry: Any,
        *,
        now_ms: int | None = None,
        conversation_ids: Iterable[str] = (),
        page_number: int | None = None,
        audience: object | None = None,
        source_event_id: str | None = None,
        source_revision: int | None = None,
        source_refs: Iterable[tuple[str, int]] = (),
    ) -> str:
        """Queue one node for embedding.  Returns the durable job key.

        The node is already committed and FTS-visible by the time this is called; the
        provider is not touched here.
        """
        now = int(now_ms if now_ms is not None else self._clock())
        metadata = _entry_metadata(entry)
        event_id = str(source_event_id or metadata.get("source_event_id") or "")
        if source_revision is not None:
            revision = int(source_revision)
        else:
            revision = int(metadata.get("source_revision") or 0)
        content = str(getattr(entry, "content", "") or "")
        content_hash = str(getattr(entry, "content_hash", "") or "")
        if not content.strip():
            raise ValueError("an embedding document needs text")
        resolved_audience = audience if audience is not None else {
            "status": metadata.get("audience_status", "unknown"),
            "members": metadata.get("audience_members") or (),
            "snapshot_id": metadata.get("audience_snapshot_id"),
        }
        revision_hash = source_revision_hash(
            event_id=event_id,
            revision=revision,
            content_hash=content_hash,
            audience_fingerprint=audience_fingerprint(resolved_audience),
        )
        page = page_number
        if page is None and metadata.get("page_number") is not None:
            page = int(metadata["page_number"])
        job_key = embedding_job_key(
            node_id=str(entry.id),
            source_revision_hash_value=revision_hash,
            model_id=self.model_id,
            dimension=self.dimension,
            preprocessing_version=self._preprocessing_version,
        )
        self._store.upsert_embedding_job(
            job_key=job_key,
            workspace_id=str(getattr(entry, "workspace_id", "") or ""),
            node_id=str(entry.id),
            scope_key=str(getattr(entry, "scope_key", "") or ""),
            channel=str(getattr(entry, "channel", "") or ""),
            chat_id=str(getattr(entry, "chat_id", "") or ""),
            source_event_id=event_id,
            source_revision=revision,
            source_revision_hash=revision_hash,
            model_id=self.model_id,
            dimension=self.dimension,
            preprocessing_version=self._preprocessing_version,
            conversation_ids=tuple(conversation_ids),
            source_refs=tuple(
                (str(item[0]), int(item[1])) for item in source_refs
            )
            or ((event_id, revision),),
            page_number=page,
            state="queued",
            due_ms=now,
            now_ms=now,
        )
        return job_key

    # -- work -------------------------------------------------------------------

    def recover_stale(self, *, now_ms: int, stale_ms: int = 600_000) -> int:
        """Re-queue jobs a crash or an unexpected error left ``running``."""
        requeued = 0
        for job in self._store.list_embedding_jobs(state="running", limit=200):
            if int(now_ms) - int(job.get("updated_ms") or 0) < max(60_000, int(stale_ms)):
                continue
            self._mark(
                job,
                state="queued",
                reason="recovered_after_crash",
                due_ms=int(now_ms),
                now_ms=int(now_ms),
            )
            requeued += 1
        return requeued

    def requeue_due_retries(self, *, now_ms: int) -> int:
        """Re-queue failed jobs whose backoff has elapsed.  Restart-safe by construction."""
        requeued = 0
        for job in self._store.list_embedding_jobs(state="failed", limit=200):
            if int(job.get("due_ms") or 0) > int(now_ms):
                continue
            if int(job.get("attempts") or 0) >= self._max_attempts:
                continue
            self._mark(job, state="queued", reason=None, due_ms=int(now_ms), now_ms=int(now_ms))
            requeued += 1
        return requeued

    def run_due(self, *, now_ms: int | None = None, limit: int | None = None) -> EmbeddingRunReport:
        """Process due jobs synchronously; the worker thread simply calls this."""
        now = int(now_ms if now_ms is not None else self._clock())
        report = EmbeddingRunReport()
        self.recover_stale(now_ms=now)
        self.requeue_due_retries(now_ms=now)
        jobs = self._store.list_embedding_jobs(
            state="queued", due_before_ms=now, limit=limit or 20
        )
        for job in jobs:
            self._run_job(job, now_ms=now, report=report)
        return report

    def _run_job(self, job: Any, *, now_ms: int, report: EmbeddingRunReport) -> None:
        self._mark(job, state="running", reason=None, due_ms=int(job["due_ms"]), now_ms=now_ms)
        report.processed += 1

        entry = self._load_entry(job)
        if entry is None:
            self._finish(job, state="skipped", reason="source_unavailable", now_ms=now_ms)
            report.skipped += 1
            report.note("source_unavailable")
            return

        # Authority and revocation are re-checked *before* any provider submission.
        denial = self._prove_source(job)
        if denial:
            self._finish(job, state="skipped", reason=denial, now_ms=now_ms)
            report.skipped += 1
            report.note(denial)
            return

        sections = chunk_source_text(str(getattr(entry, "content", "") or ""))
        if not sections:
            self._finish(job, state="skipped", reason="no_text", now_ms=now_ms)
            report.skipped += 1
            report.note("no_text")
            return

        vectors: list[list[float]] = []
        expected = int(job.get("dimension") or 0)
        for section in sections:
            try:
                vector = self._embedder.embed(section.text)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("memory embedding provider failed: {}", exc)
                vector = None
            if not vector:
                self._fail(job, reason="provider_unavailable", now_ms=now_ms)
                report.failed += 1
                report.note("provider_unavailable")
                return
            if expected and len(vector) != expected:
                self._fail(job, reason="dimension_mismatch", now_ms=now_ms)
                report.failed += 1
                report.note("dimension_mismatch")
                return
            vectors.append([float(item) for item in vector])

        # Publish the replacement first; only then retire the older active rows.
        kept_keys: list[tuple[str, str, str, str, int, str]] = []
        for section, vector in zip(sections, vectors, strict=True):
            document_id = section_document_id(str(job["node_id"]), section.index)
            section_hash = hashlib.sha256(section.text.encode("utf-8")).hexdigest()[:32]
            kept_keys.append(
                (
                    document_id,
                    section_hash,
                    str(job["source_revision_hash"]),
                    str(job["model_id"]),
                    len(vector) if not expected else expected,
                    str(job["preprocessing_version"]),
                )
            )
            self._store.publish_embedding_section(
                document_id=document_id,
                content_hash=section_hash,
                source_revision_hash=str(job["source_revision_hash"]),
                model_id=str(job["model_id"]),
                dimension=len(vector) if not expected else expected,
                preprocessing_version=str(job["preprocessing_version"]),
                workspace_id=str(job["workspace_id"]),
                scope_key=str(job["scope_key"]),
                channel=str(job["channel"]),
                chat_id=str(job["chat_id"]),
                source_event_id=str(job["source_event_id"]),
                source_revision=int(job["source_revision"]),
                node_id=str(job["node_id"]),
                section_index=section.index,
                section_start=section.start,
                section_end=section.end,
                page_number=job.get("page_number"),
                conversation_ids=job.get("conversation_ids") or (),
                vector=vector,
                now_ms=now_ms,
            )
        self._store.retire_embedding_sections(
            node_id=str(job["node_id"]),
            keep_keys=kept_keys,
            now_ms=now_ms,
        )
        # A shared fact also keeps the node-level vector the existing fact retrieval path
        # reads, so the versioned index and the pre-existing search stay consistent.
        if self._is_shared_fact(str(job["node_id"])):
            self._store.set_fact_embedding(
                str(job["node_id"]),
                workspace_id=str(job["workspace_id"]),
                model=str(job["model_id"]),
                vector=list(vectors[0]),
            )
        self.published_sections += len(vectors)
        remember = getattr(self._store, "remember_embedding_dimension", None)
        if callable(remember) and kept_keys:
            remember(str(job["model_id"]), int(kept_keys[0][4]))
        self._finish(job, state="done", reason=None, now_ms=now_ms)
        report.published += 1
        report.note("published")

    def _is_shared_fact(self, node_id: str) -> bool:
        row = self._store.query_one(
            "SELECT 1 FROM memory2_facts WHERE fact_id = ? LIMIT 1", (str(node_id),)
        )
        return row is not None

    def _load_entry(self, job: Any) -> Any | None:
        getter = getattr(self._store, "get_node", None)
        if not callable(getter):
            return None
        try:
            entry = getter(str(job["node_id"]), workspace_id=str(job["workspace_id"]))
        except Exception:  # pragma: no cover - defensive
            return None
        if entry is None or bool(getattr(entry, "is_deleted", False)):
            return None
        return entry

    def _prove_source(self, job: Any) -> str | None:
        """Re-read the live authority for *every* source this document rests on.

        A statement can rest on several sources, so proving only the first would let a
        revoked secondary source still reach the provider.  Unknown, unproven or revoked
        at any position means: no call at all.  Returns a denial reason, or ``None`` when
        the document may proceed.

        With no proof owner configured at all there is no revocation information in the
        process, so the node-level gate that already ran when the entry was loaded
        (existence, soft-delete, redaction) is the whole decision - exactly as it is for
        every other lexical memory path.  As soon as an authority is present it decides.
        """
        if self._authority is None:
            return None
        refs = list(job.get("source_refs") or ())
        primary_event = str(job.get("source_event_id") or "")
        if not refs:
            refs = [(primary_event, int(job.get("source_revision") or 0))]
        if not any(str(event_id) for event_id, _revision in refs):
            return "unknown_source"
        getter = getattr(self._authority, "verify_source_ref", None)
        if not callable(getter):
            return "unknown_authority"
        revoked = getattr(self._authority, "source_revoked", None)
        verify = getattr(self._authority, "verify_source", None)
        for event_id, revision in refs:
            if not str(event_id):
                return "unknown_source"
            try:
                source = getter(str(event_id), int(revision))
            except Exception:  # pragma: no cover - defensive
                return "unknown_authority"
            if source is None:
                return "unknown_source"
            if callable(revoked) and revoked(source):
                return "source_revoked"
            if callable(verify) and not verify(source):
                return "unauthorized"
        return None

    # -- job bookkeeping ---------------------------------------------------------

    def _mark(
        self,
        job: Any,
        *,
        state: str,
        reason: str | None,
        due_ms: int,
        now_ms: int,
        attempts: int | None = None,
    ) -> None:
        self._store.upsert_embedding_job(
            job_key=str(job["job_key"]),
            workspace_id=str(job["workspace_id"]),
            node_id=str(job["node_id"]),
            scope_key=str(job["scope_key"]),
            channel=str(job["channel"]),
            chat_id=str(job["chat_id"]),
            source_event_id=str(job["source_event_id"]),
            source_revision=int(job["source_revision"]),
            source_revision_hash=str(job["source_revision_hash"]),
            model_id=str(job["model_id"]),
            dimension=int(job["dimension"]),
            preprocessing_version=str(job["preprocessing_version"]),
            conversation_ids=tuple(job.get("conversation_ids") or ()),
            page_number=job.get("page_number"),
            state=state,
            reason=reason,
            due_ms=int(due_ms),
            attempts=attempts,
            now_ms=int(now_ms),
        )

    def _finish(
        self, job: Any, *, state: str, reason: str | None, now_ms: int
    ) -> None:
        attempts = int(job.get("attempts") or 0) + (1 if state in ("done", "failed", "skipped") else 0)
        self._mark(
            job,
            state=state,
            reason=reason,
            due_ms=int(job["due_ms"]),
            now_ms=now_ms,
            attempts=attempts,
        )

    def _fail(self, job: Any, *, reason: str, now_ms: int) -> None:
        self.failed_jobs += 1
        attempts = int(job.get("attempts") or 0) + 1
        due_ms = (
            int(now_ms) + self._retry_delay_ms
            if attempts < self._max_attempts
            else int(now_ms) + 365 * 24 * 3_600_000
        )
        self._mark(
            job, state="failed", reason=reason, due_ms=due_ms, now_ms=now_ms, attempts=attempts
        )


def _entry_metadata(entry: Any) -> dict[str, Any]:
    raw = getattr(entry, "meta_json", None)
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
