"""Plan 05 / Aufgabe 2: permission is decided before retrieval, not after rendering."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from yeoman_gateway.memory.read_gate import FactPermissionCache, FactReadGate
from yeoman_gateway.memory.shared_facts import (
    FactReadContext,
    FactSource,
    SharedFact,
    can_read_shared,
)
from yeoman_gateway.memory.store import MemoryStore

WORKSPACE_SCOPE = "gruppe-a"
T0 = 1_700_000_000_000
SECRET = "SYNTHETISCHES-GEHEIMTOKEN-4711"


def _ctx(
    *, principal: str, members: set[str] | None, chat: str = WORKSPACE_SCOPE, now_ms: int = T0
) -> FactReadContext:
    return FactReadContext(
        principal_id=principal,
        chat_scope_key=chat,
        current_members=None if members is None else frozenset(members),
        audience_snapshot_id="snap1",
        epoch=1,
        now_ms=now_ms,
        owner=False,
    )


def _fact(
    *,
    fact_id: str = "f1",
    audience: set[str] | None = None,
    visibility: str = "chat_shared",
    content: str = "Der Stammtisch ist donnerstags.",
    valid_until_ms: int | None = None,
    revoked_at_ms: int | None = None,
    superseded_by: str | None = None,
    status: str = "assertion",
    chat: str = WORKSPACE_SCOPE,
) -> SharedFact:
    return SharedFact(
        fact_id=fact_id,
        workspace_id="ws1",
        chat_scope_key=chat,
        content=content,
        author_principal="member-old",
        assertion_status=status,  # type: ignore[arg-type]
        visibility_scope=visibility,  # type: ignore[arg-type]
        group_rule="chat_members_at_source",
        valid_from_ms=T0,
        valid_until_ms=valid_until_ms,
        superseded_by=superseded_by,
        revoked_at_ms=revoked_at_ms,
        extractor_version="v1",
        sources=(
            FactSource(
                source_event_id="ev1",
                source_revision=1,
                source_trace_id="tr1",
                author_principal="member-old",
                source_channel="whatsapp",
                source_chat_id=chat,
                occurred_ms=T0,
            ),
        ),
        allowed_principals=frozenset(),
        audience=frozenset(audience if audience is not None else {"member-old"}),
    )


def _store_with_facts(tmp_path: Path, *facts: SharedFact) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory.db")
    for fact in facts:
        store.upsert_fact(fact)
    return store


def test_new_group_member_does_not_inherit_old_personal_fact() -> None:
    assert not can_read_shared(
        fact=_fact(audience={"member-old"}, visibility="chat_shared"),
        read_context=_ctx(principal="member-new", members={"member-old", "member-new"}),
    )


def test_unknown_membership_is_never_injected() -> None:
    assert not can_read_shared(
        fact=_fact(audience={"member-old"}),
        read_context=_ctx(principal="member-old", members=None),
    )


def test_author_only_fact_is_readable_by_its_author_only() -> None:
    fact = _fact(audience=set(), visibility="author_only")

    assert can_read_shared(fact=fact, read_context=_ctx(principal="member-old", members=None))
    assert not can_read_shared(fact=fact, read_context=_ctx(principal="member-new", members=None))


def test_revoked_and_expired_facts_are_not_readable() -> None:
    assert not can_read_shared(
        fact=_fact(revoked_at_ms=T0 + 1),
        read_context=_ctx(principal="member-old", members={"member-old"}),
    )
    assert not can_read_shared(
        fact=_fact(valid_until_ms=T0),
        read_context=_ctx(principal="member-old", members={"member-old"}, now_ms=T0),
    )
    assert not can_read_shared(
        fact=_fact(superseded_by="f2"),
        read_context=_ctx(principal="member-old", members={"member-old"}),
    )


def test_gate_predicate_never_returns_a_forbidden_fact(tmp_path: Path) -> None:
    store = _store_with_facts(
        tmp_path,
        _fact(fact_id="allowed", audience={"member-old"}),
        _fact(fact_id="forbidden", audience={"member-new"}),
    )
    gate = FactReadGate(store)

    visible = gate.allowed_fact_ids(_ctx(principal="member-old", members={"member-old"}))

    assert visible == frozenset({"allowed"})
    store.close()


def test_gate_predicate_is_empty_for_unknown_membership(tmp_path: Path) -> None:
    store = _store_with_facts(tmp_path, _fact(fact_id="allowed", audience={"member-old"}))
    gate = FactReadGate(store)

    assert gate.allowed_fact_ids(_ctx(principal="member-old", members=None)) == frozenset()
    assert gate.allowed_fact_ids(_ctx(principal="member-old", members=set())) == frozenset()
    store.close()


def test_gate_binds_ids_instead_of_interpolating_them(tmp_path: Path) -> None:
    store = _store_with_facts(tmp_path, _fact(fact_id="allowed", audience={"member-old"}))
    gate = FactReadGate(store)
    hostile = "member' OR 1=1 --"

    assert gate.allowed_fact_ids(_ctx(principal=hostile, members={hostile})) == frozenset()
    store.close()


def test_expiry_and_revocation_remove_candidates(tmp_path: Path) -> None:
    store = _store_with_facts(
        tmp_path,
        _fact(fact_id="live", audience={"member-old"}),
        _fact(fact_id="expired", audience={"member-old"}, valid_until_ms=T0 + 10),
        _fact(fact_id="revoked", audience={"member-old"}, revoked_at_ms=T0 + 1),
    )
    gate = FactReadGate(store)

    early = gate.allowed_fact_ids(
        _ctx(principal="member-old", members={"member-old"}, now_ms=T0 + 5)
    )
    late = gate.allowed_fact_ids(
        _ctx(principal="member-old", members={"member-old"}, now_ms=T0 + 20)
    )

    assert early == frozenset({"live", "expired"})
    assert late == frozenset({"live"})
    store.close()


def test_recheck_drops_a_fact_revoked_after_retrieval(tmp_path: Path) -> None:
    store = _store_with_facts(tmp_path, _fact(fact_id="f1", audience={"member-old"}))
    gate = FactReadGate(store)
    context = _ctx(principal="member-old", members={"member-old"})
    assert gate.recheck(["f1"], context) == frozenset({"f1"})

    store.set_fact_status("f1", status="revoked", now_ms=T0 + 5)

    assert gate.recheck(["f1"], context) == frozenset()
    store.close()


def test_permission_cache_follows_the_acl_epoch(tmp_path: Path) -> None:
    store = _store_with_facts(tmp_path, _fact(fact_id="f1", audience={"member-old"}))
    cache = FactPermissionCache()
    gate = FactReadGate(store, cache=cache)

    assert cache.audience_for(store, "f1") == frozenset({"member-old"})
    first_epoch = store.acl_epoch()
    assert cache.epoch_of(store) == first_epoch
    lookups_before = cache.lookups

    # Same epoch: the audience stays cached.
    assert cache.audience_for(store, "f1") == frozenset({"member-old"})
    assert cache.lookups == lookups_before

    store.set_fact_status("f1", status="revoked", now_ms=T0 + 1)

    assert store.acl_epoch() == first_epoch + 1
    assert gate.recheck(["f1"], _ctx(principal="member-old", members={"member-old"})) == frozenset()
    store.close()


def test_membership_is_evaluated_live_and_not_cached(tmp_path: Path) -> None:
    store = _store_with_facts(tmp_path, _fact(fact_id="f1", audience={"member-old"}))
    gate = FactReadGate(store)

    inside = gate.allowed_fact_ids(_ctx(principal="member-old", members={"member-old"}))
    outside = gate.allowed_fact_ids(_ctx(principal="member-old", members={"member-new"}))

    assert inside == frozenset({"f1"})
    assert outside == frozenset()
    store.close()


@dataclass
class _RecordingProvider:
    """Stands in for the embedding service and records every text it is asked to embed."""

    seen: list[str] = field(default_factory=list)
    vector: list[float] = field(default_factory=lambda: [0.5] * 8)

    def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        return list(self.vector)


def _service(tmp_path: Path, *, shared: bool = True):
    from unittest.mock import patch

    from yeoman_gateway.memory.service import MemoryService
    from yeoman_shared.config.schema import Config

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "memory.db")
    cfg.memory.capture.enabled = False
    cfg.memory.embedding.enabled = False
    cfg.memory.shared.enabled = shared
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=cfg.memory)


def _publish(service, fact: SharedFact) -> SharedFact:
    """Facts live under the memory service's own workspace id."""
    return service.store.upsert_fact(replace(fact, workspace_id=service.workspace_id))

def test_forbidden_fact_never_reaches_provider_or_prompt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    provider = _RecordingProvider()
    service.embedding = provider  # type: ignore[assignment]
    _publish(service, _fact(fact_id="visible", audience={"member-old"}))
    _publish(
        service,
        # Deliberately the better lexical match: permission must win over relevance.
        _fact(fact_id="hidden", audience={"member-new"},
              content=f"Der Stammtisch ist donnerstags. {SECRET}"),
    )
    provider.seen.clear()

    result = service.retrieve_for_context(
        query="Wann ist der Stammtisch?",
        read_context=_ctx(principal="member-old", members={"member-old", "member-new"}),
    )

    assert SECRET not in "\n".join(provider.seen)
    assert SECRET not in result.text
    assert SECRET not in repr(result.used_source_refs)
    assert "hidden" not in result.used_source_refs
    # The forbidden fact must never even become a candidate: had the ACL only been
    # applied during the recheck, the retrieval would have scored it first.
    assert result.denied_count == 0
    assert result.used_source_refs.get("visible") == (("ev1", 1),)
    service.close()


def test_retrieval_is_inert_while_shared_facts_are_disabled(tmp_path: Path) -> None:
    service = _service(tmp_path, shared=False)
    provider = _RecordingProvider()
    service.embedding = provider  # type: ignore[assignment]
    _publish(service, _fact(fact_id="visible", audience={"member-old"}))
    provider.seen.clear()

    result = service.retrieve_for_context(
        query="Wann ist der Stammtisch?",
        read_context=_ctx(principal="member-old", members={"member-old"}),
    )

    assert result.text == ""
    assert result.used_source_refs == {}
    assert provider.seen == []
    service.close()


def test_unknown_membership_yields_no_retrieval_and_no_provider_call(tmp_path: Path) -> None:
    service = _service(tmp_path)
    provider = _RecordingProvider()
    service.embedding = provider  # type: ignore[assignment]
    _publish(service, _fact(fact_id="visible", audience={"member-old"}))
    provider.seen.clear()

    result = service.retrieve_for_context(
        query="Wann ist der Stammtisch?",
        read_context=_ctx(principal="member-old", members=None),
    )

    assert result.text == ""
    assert provider.seen == []
    service.close()
