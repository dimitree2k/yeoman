"""Read gates for shared facts: decide permission in SQL, before any retrieval.

Plan 05, Aufgabe 2. Two rules shape this module:

* Permission is a **candidate filter**, not a post-processing step. Forbidden rows are
  never retrieved, never embedded and never ranked, so their content cannot reach a
  provider, a prompt or a metric label.
* ``current_members`` is live state and is therefore evaluated on every call; only the
  audience resolution (which is derived from stored fact rows) is cached, keyed by the
  store's ``acl_epoch``. A rights change bumps the epoch and invalidates the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from yeoman_gateway.memory.shared_facts import (
    FactReadContext,
    SharedFact,
    can_read_shared,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from yeoman_gateway.memory.store import MemoryStore

#: Fact states that may still be read.
READABLE_STATUSES: tuple[str, ...] = ("assertion", "confirmed")


@dataclass(frozen=True, slots=True)
class FactAclPredicate:
    """A pure SQL fragment plus its parameters. Ids are bound, never interpolated."""

    sql: str
    params: tuple[Any, ...] = ()

    def combine(self, *others: "FactAclPredicate") -> "FactAclPredicate":
        parts = [self, *others]
        return FactAclPredicate(
            sql=" AND ".join(part.sql for part in parts if part.sql),
            params=tuple(item for part in parts for item in part.params),
        )


@dataclass(slots=True)
class FactPermissionCache:
    """Caches audience resolution per ``acl_epoch``. Never caches membership."""

    _epoch: int = -1
    _audiences: dict[str, frozenset[str]] = field(default_factory=dict)
    lookups: int = 0

    def epoch_of(self, store: "MemoryStore") -> int:
        return store.acl_epoch()

    def audience_for(self, store: "MemoryStore", fact_id: str) -> frozenset[str]:
        epoch = self.epoch_of(store)
        if epoch != self._epoch:
            self._audiences.clear()
            self._epoch = epoch
        if fact_id not in self._audiences:
            self.lookups += 1
            rows = store.fact_audience(fact_id)
            self._audiences[fact_id] = frozenset(rows)
        return self._audiences[fact_id]

    def invalidate(self) -> None:
        self._audiences.clear()
        self._epoch = -1


class FactReadGate:
    """Builds SQL predicates and rechecks candidates against the current fact rows."""

    def __init__(self, store: "MemoryStore", *, cache: FactPermissionCache | None = None) -> None:
        self._store = store
        self._cache = cache if cache is not None else FactPermissionCache()

    @property
    def cache(self) -> FactPermissionCache:
        return self._cache

    def predicate(self, read_context: FactReadContext) -> FactAclPredicate:
        """The candidate filter for one reader.

        A fact row must exist (legacy nodes are not shared facts), must be readable
        now, its author-only or audience rows must contain the reader, and the reader
        must still be a member of the chat. Unknown membership yields an unsatisfiable
        predicate rather than an unfiltered search.
        """
        if not read_context.membership_known:
            return FactAclPredicate(sql="0", params=())

        members = read_context.current_members or frozenset()
        if read_context.principal_id not in members:
            # Not a member any more: nothing in this chat is readable, whatever the
            # fact's stored audience says.
            return FactAclPredicate(sql="0", params=())

        # The reader - not merely some member of the chat - must be named in the fact.
        sql = (
            "EXISTS (SELECT 1 FROM memory2_facts f"
            " WHERE f.fact_id = n.id"
            "   AND f.chat_scope_key = ?"
            "   AND f.assertion_status IN ('assertion','confirmed')"
            "   AND f.revoked_at_ms IS NULL"
            "   AND f.superseded_by IS NULL"
            "   AND (f.valid_until_ms IS NULL OR f.valid_until_ms > ?)"
            "   AND ((f.visibility_scope = 'author_only' AND f.author_principal = ?)"
            "        OR (f.visibility_scope <> 'author_only'"
            "            AND EXISTS (SELECT 1 FROM memory2_fact_principals p"
            "                         WHERE p.fact_id = f.fact_id"
            "                           AND p.role = 'audience'"
            "                           AND p.principal_id = ?))))"
        )
        params: tuple[Any, ...] = (
            read_context.chat_scope_key,
            int(read_context.now_ms),
            read_context.principal_id,
            read_context.principal_id,
        )
        return FactAclPredicate(sql=sql, params=params)

    def allowed_fact_ids(
        self, read_context: FactReadContext, *, limit: int | None = None
    ) -> frozenset[str]:
        """Every fact id this reader may read right now (diagnostics and tests)."""
        predicate = self.predicate(read_context)
        if predicate.sql == "0":
            return frozenset()
        rows = self._store.select_fact_ids(
            sql=predicate.sql, params=predicate.params, limit=limit
        )
        return frozenset(rows)

    def recheck(
        self, fact_ids: Sequence[str], read_context: FactReadContext
    ) -> frozenset[str]:
        """Re-verify candidates against the *current* fact rows, just before output.

        Retrieval and rendering are not atomic: a revocation, an expiry or a new
        ``acl_epoch`` may have happened in between. Anything that is no longer
        readable is dropped here instead of being rendered.
        """
        allowed: set[str] = set()
        for fact_id in dict.fromkeys(str(item) for item in fact_ids):
            fact = self._store.get_fact(fact_id)
            if fact is None:
                continue
            if can_read_shared(fact=fact, read_context=read_context):
                allowed.add(fact_id)
        return frozenset(allowed)


def chat_scope_key(channel: str, chat_id: str) -> str:
    """The one scope-key convention shared with ``MemoryService.chat_scope_key``."""
    return f"channel:{channel}:chat:{chat_id}"


def build_read_context(
    *,
    principal_id: str,
    channel: str,
    chat_id: str,
    chat_registry: Any | None = None,
    policy: Any | None = None,
    now_ms: int = 0,
    epoch: int = 0,
    owner: bool = False,
    is_direct: bool = False,
    counterpart: str | None = None,
) -> FactReadContext:
    """Assemble a read context from trusted runtime state, never from model arguments.

    Membership comes from the proven participant list of the chat registry. A missing
    or empty list means "unknown" (``None``), which suppresses injection entirely - for
    a direct chat the only member is the conversation partner.
    """
    scope_key = chat_scope_key(channel, chat_id)
    members: frozenset[str] | None = None

    if is_direct:
        candidates = {principal_id}
        if counterpart:
            candidates.add(str(counterpart))
        members = frozenset(candidates)
    elif chat_registry is not None:
        raw = _registry_members(chat_registry, channel=channel, chat_id=chat_id)
        if raw:
            members = frozenset(str(item) for item in raw)

    return FactReadContext(
        principal_id=str(principal_id),
        chat_scope_key=scope_key,
        current_members=members,
        audience_snapshot_id=None,
        epoch=int(epoch),
        now_ms=int(now_ms),
        owner=bool(owner),
    )


def _registry_members(chat_registry: Any, *, channel: str, chat_id: str) -> set[str]:
    """Read the proven participant list, tolerating both registry shapes."""
    for name in ("participants", "members_for", "known_members"):
        method = getattr(chat_registry, name, None)
        if method is None:
            continue
        try:
            raw = method(channel=channel, chat_id=chat_id) if name != "participants" else method(
                channel, chat_id
            )
        except TypeError:
            try:
                raw = method(chat_id)
            except Exception:  # pragma: no cover - defensive
                continue
        except Exception:  # pragma: no cover - defensive
            continue
        return _as_principal_set(raw)
    return set()


def _as_principal_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, Mapping):
        raw = raw.get("participants") or raw.get("members") or []
    members: set[str] = set()
    for item in raw or ():
        if isinstance(item, str):
            members.add(item)
            continue
        for attribute in ("principal_id", "id", "jid", "lid", "user_id"):
            value = getattr(item, attribute, None)
            if value:
                members.add(str(value))
                break
        else:
            if isinstance(item, Mapping):
                for attribute in ("principal_id", "id", "jid", "lid", "user_id"):
                    if item.get(attribute):
                        members.add(str(item[attribute]))
                        break
    return {member for member in members if member}


def fact_denied_by_status(fact: SharedFact | None, *, now_ms: int) -> bool:
    """True when a fact exists but is not readable for lifecycle reasons."""
    if fact is None:
        return True
    if fact.revoked_at_ms is not None or fact.superseded_by:
        return True
    if fact.assertion_status not in READABLE_STATUSES:
        return True
    return fact.valid_until_ms is not None and now_ms >= fact.valid_until_ms
