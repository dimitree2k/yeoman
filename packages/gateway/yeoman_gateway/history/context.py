"""Task-local leases and synchronous admission checks for history-dependent effects."""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .live import HistoryPaused
from .queries import HistoryQueries

if TYPE_CHECKING:
    from .live import HistoryProjector
    from .reader import HistorySnapshot

_HISTORY: ContextVar[HistorySnapshot | None] = ContextVar('history_snapshot', default=None)
_PROJECTOR: ContextVar[HistoryProjector | None] = ContextVar('history_projector', default=None)


def current_history_snapshot() -> HistorySnapshot | None:
    snapshot = _HISTORY.get()
    return snapshot if snapshot is not None and not snapshot._closed else None


@asynccontextmanager
async def history_turn(projector: HistoryProjector) -> AsyncIterator[HistorySnapshot]:
    """A nested consumer borrows the root lease; only its owner closes it."""
    current = current_history_snapshot()
    if current is not None:
        if _PROJECTOR.get() is not projector:
            raise HistoryPaused('history_scope_mismatch')
        require_history_effect(projector, current)
        yield current
        return
    snapshot = await projector.read_turn()
    token = _HISTORY.set(snapshot)
    owner = _PROJECTOR.set(projector)
    try:
        yield snapshot
    finally:
        _PROJECTOR.reset(owner)
        _HISTORY.reset(token)
        snapshot.close()


def validate_history_effect_generation(projector: HistoryProjector, generation: int) -> None:
    health = projector.health()
    if health['status'] != 'ready' or health['generation'] != generation:
        raise HistoryPaused('generation_invalidated')


def require_history_effect(projector: HistoryProjector, snapshot: HistorySnapshot) -> None:
    snapshot.assert_current(snapshot.generation)
    validate_history_effect_generation(projector, snapshot.generation)


def history_effect_metadata() -> dict[str, int]:
    snapshot = current_history_snapshot()
    if snapshot is None:
        return {}
    projector = _PROJECTOR.get()
    if projector is None:
        raise HistoryPaused('history_scope_required')
    require_history_effect(projector, snapshot)
    return {'history_generation': snapshot.generation}


@contextmanager
def history_knowledge_scope(snapshot: HistorySnapshot, knowledge: Any) -> Iterator[None]:
    if knowledge is None:
        yield
        return
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources

    queries = HistoryQueries(snapshot)
    sources = HistoryKnowledgeSources(queries, {}, knowledge.history_source_ledger,
                                      knowledge._legacy_authority)
    with knowledge.history_scope(queries, sources):
        yield


def validate_history_evidence(evidence: Any) -> bool:
    proof = evidence.get('history') if isinstance(evidence, Mapping) else None
    if proof is None:
        return True
    snapshot = current_history_snapshot()
    if not isinstance(proof, Mapping) or snapshot is None or isinstance(proof.get('generation'), bool) or proof.get('generation') != snapshot.generation:
        return False
    history_effect_metadata()
    queries = HistoryQueries(snapshot)
    revisions = proof.get('revisions')
    return isinstance(revisions, Mapping) and bool(revisions) and all(isinstance(revision, str) and queries.content_fingerprint(key) == revision
                                  for key, revision in revisions.items())
