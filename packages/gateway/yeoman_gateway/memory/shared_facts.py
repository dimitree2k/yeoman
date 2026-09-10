"""Shared chat facts: source-bound, audience-bound, additive to the legacy memory store.

Plan 05. A shared fact is a memory node with provenance and an explicit audience. The
legacy recall path stays untouched: facts live in their own tables, and a legacy node
without a fact row can therefore never acquire shared-fact rights.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.memory.store import MemoryStore

AssertionStatus = Literal["assertion", "confirmed", "superseded", "revoked", "expired"]
VisibilityScope = Literal["chat_shared", "principals", "author_only"]
GroupRule = Literal["chat_members_at_source", "explicit_principals", "author_only", "none"]

ASSERTION_STATUSES: tuple[str, ...] = (
    "assertion",
    "confirmed",
    "superseded",
    "revoked",
    "expired",
)
VISIBILITY_SCOPES: tuple[str, ...] = ("chat_shared", "principals", "author_only")
GROUP_RULES: tuple[str, ...] = (
    "chat_members_at_source",
    "explicit_principals",
    "author_only",
    "none",
)

SHARED_FACT_EXTRACTOR_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class FactSource:
    """One proven source of a fact: journal event plus the revision that carried it."""

    source_event_id: str
    source_revision: int
    source_trace_id: str = ""
    author_principal: str = ""
    source_channel: str = ""
    source_chat_id: str = ""
    occurred_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SharedFact:
    """A fact with provenance, an audience and a lifecycle."""

    fact_id: str
    workspace_id: str
    chat_scope_key: str
    content: str
    author_principal: str
    assertion_status: AssertionStatus
    visibility_scope: VisibilityScope
    group_rule: GroupRule
    valid_from_ms: int
    extractor_version: str
    sources: tuple[FactSource, ...] = ()
    allowed_principals: frozenset[str] = frozenset()
    audience: frozenset[str] = frozenset()
    audience_snapshot_id: str | None = None
    valid_until_ms: int | None = None
    superseded_by: str | None = None
    revoked_at_ms: int | None = None
    created_ms: int = 0
    updated_ms: int = 0


@dataclass(frozen=True, slots=True)
class FactReadContext:
    """Read-side facts about the reader. Built from runtime state, never from a model."""

    principal_id: str
    chat_scope_key: str
    current_members: frozenset[str] | None
    audience_snapshot_id: str | None = None
    epoch: int = 0
    now_ms: int = 0
    owner: bool = False

    @property
    def membership_known(self) -> bool:
        """True only when a proven membership list exists for the chat right now."""
        return self.current_members is not None


def effective_audience(
    *,
    source_audiences: tuple[tuple[frozenset[str] | None, str | None], ...],
    group_rule: GroupRule,
    allowed_principals: frozenset[str],
    audience_snapshots: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    """Resolve who may read a fact.

    Multiple sources intersect, never union: a fact is only as public as its most
    private source. An unknown or missing snapshot resolves to the empty set, so
    unpublished provenance fails closed instead of leaking to everyone.
    """
    if group_rule in ("author_only", "none"):
        return frozenset()

    resolved: list[frozenset[str]] = []
    for audience, snapshot_id in source_audiences:
        members = audience
        if members is None and snapshot_id is not None:
            members = audience_snapshots.get(snapshot_id)
        if members is None:
            return frozenset()
        resolved.append(frozenset(members))

    if not resolved:
        return frozenset()

    effective = frozenset(resolved[0])
    for members in resolved[1:]:
        effective &= members

    if group_rule == "explicit_principals":
        effective &= frozenset(allowed_principals)

    return effective


def can_read_shared(*, fact: SharedFact, read_context: FactReadContext) -> bool:
    """Pure read rule. Order matters and is part of the contract (Plan 05, Aufgabe 2)."""
    if fact.visibility_scope == "author_only":
        return fact.author_principal == read_context.principal_id
    if not read_context.membership_known:
        return False
    if fact.revoked_at_ms is not None or fact.superseded_by:
        return False
    if fact.valid_until_ms is not None and read_context.now_ms >= fact.valid_until_ms:
        return False
    if read_context.principal_id not in fact.audience:
        return False
    assert read_context.current_members is not None  # guaranteed by membership_known
    return read_context.principal_id in read_context.current_members


def fact_content_hash(fact_id: str, content: str) -> str:
    """Per-fact content hash.

    ``MemoryStore.upsert_node`` merges rows that share ``(workspace, scope, sector,
    content_hash)``. Two facts with identical text must stay two rows - the fact row
    points at its own node id - so the fact id is part of the hash.
    """
    return hashlib.sha256(f"{fact_id}\x00{content}".encode()).hexdigest()


@dataclass(slots=True)
class InvalidationReport:
    """Outcome of revoking or superseding the sources of a fact."""

    revoked: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    jobs_cancelled: int = 0
    remaining_copies: int = 0


@dataclass(slots=True)
class FactRetrievalResult:
    """What a read produced: filtered text plus the source revisions it rests on."""

    text: str = ""
    hits: tuple[Any, ...] = ()
    used_source_refs: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    denied_count: int = 0


class SharedFactStore:
    """Facade over ``MemoryStore`` fact methods: one connection, no second lock."""

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    @property
    def store(self) -> "MemoryStore":
        return self._store

    def upsert_fact(self, fact: SharedFact) -> SharedFact:
        return self._store.upsert_fact(fact)

    def get_fact(self, fact_id: str) -> SharedFact | None:
        return self._store.get_fact(fact_id)

    def list_facts(
        self,
        *,
        workspace_id: str | None = None,
        chat_scope_key: str | None = None,
        include_inactive: bool = True,
        limit: int | None = None,
    ) -> list[SharedFact]:
        return self._store.list_facts(
            workspace_id=workspace_id,
            chat_scope_key=chat_scope_key,
            include_inactive=include_inactive,
            limit=limit,
        )

    def set_fact_status(
        self,
        fact_id: str,
        *,
        status: AssertionStatus,
        now_ms: int,
        superseded_by: str | None = None,
    ) -> bool:
        return self._store.set_fact_status(
            fact_id, status=status, now_ms=now_ms, superseded_by=superseded_by
        )

    def redact_fact(self, fact_id: str, *, now_ms: int) -> bool:
        return self._store.redact_fact(fact_id, now_ms=now_ms)

    def sources_json(self, fact: SharedFact) -> str:
        return json.dumps(
            [
                {
                    "source_event_id": source.source_event_id,
                    "source_revision": source.source_revision,
                }
                for source in fact.sources
            ],
            separators=(",", ":"),
        )
