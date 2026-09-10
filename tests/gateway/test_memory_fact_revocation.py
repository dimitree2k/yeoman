"""Plan 05 / Aufgabe 4: corrections and deletions reach the facts they produced."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from yeoman_gateway.memory.read_gate import FactPermissionCache, FactReadGate
from yeoman_gateway.memory.shared_facts import (
    FactReadContext,
    FactSource,
    SharedFact,
)
from yeoman_shared.config.schema import Config

T0 = 1_700_000_000_000
WORKSPACE = "ws1"
CHAT = "gruppe-a"


class _Event:
    def __init__(self, event_id: str, kind: str) -> None:
        self.event_id = event_id
        self.kind = kind
        self.payload: dict | None = {"text": "Quelltext"}
        self.payload_available = True


class _Journal:
    def __init__(self, kinds: dict[str, str]) -> None:
        self._events = {event_id: _Event(event_id, kind) for event_id, kind in kinds.items()}

    def get_event(self, event_id: str) -> _Event | None:
        return self._events.get(event_id)


def _service(tmp_path: Path):
    from yeoman_gateway.memory.service import MemoryService

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "memory.db")
    cfg.memory.capture.enabled = False
    cfg.memory.embedding.enabled = False
    cfg.memory.shared.enabled = True
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=cfg.memory)


def _fact(fact_id: str, *, workspace_id: str, source_event_id: str = "ev-1",
          revision: int = 1, content: str = "Quelltext") -> SharedFact:
    return SharedFact(
        fact_id=fact_id,
        workspace_id=workspace_id,
        chat_scope_key=CHAT,
        content=content,
        author_principal="member-old",
        assertion_status="assertion",
        visibility_scope="chat_shared",
        group_rule="chat_members_at_source",
        valid_from_ms=T0,
        extractor_version="v1",
        sources=(
            FactSource(
                source_event_id=source_event_id,
                source_revision=revision,
                author_principal="member-old",
                source_chat_id=CHAT,
            ),
        ),
        audience=frozenset({"member-old"}),
        created_ms=T0,
        updated_ms=T0,
    )


def _ctx(*, principal: str = "member-old", members=("member-old",), now_ms: int = T0 + 1):
    return FactReadContext(
        principal_id=principal,
        chat_scope_key=CHAT,
        current_members=frozenset(members),
        now_ms=now_ms,
        epoch=1,
    )


def test_deletion_revokes_derived_fact_and_removes_source_text(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    fact_id = store.upsert_fact(_fact("f1", workspace_id=service.workspace_id)).fact_id
    epoch_before = int(store.get_meta("acl_epoch"))

    report = service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    assert report.revoked == (fact_id,)
    assert store.get_node(fact_id, workspace_id=service.workspace_id).content == ""
    assert int(store.get_meta("acl_epoch")) > epoch_before
    assert report.remaining_copies
    assert service.invalidate_sources(["ev-1"], now_ms=T0 + 20).revoked == ()
    service.close()


def test_deleted_source_text_leaves_search_fts_and_vectors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    fact_id = store.upsert_fact(
        _fact("f1", workspace_id=service.workspace_id, content="Der Stammtisch ist donnerstags.")
    ).fact_id
    store._conn.execute(
        "INSERT INTO memory2_embeddings (entry_id, workspace_id, model, dims, vector, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (fact_id, service.workspace_id, "test", 2, b"\x00\x00\x00\x00", "2026-09-10T00:00:00Z"),
    )
    store._conn.commit()
    assert store.search_lexical(
        workspace_id=service.workspace_id, query="Stammtisch", scope_keys=[CHAT]
    )

    service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    assert store.search_lexical(
        workspace_id=service.workspace_id, query="Stammtisch", scope_keys=[CHAT]
    ) == []
    fts = store._conn.execute(
        "SELECT COUNT(*) FROM memory2_nodes_fts WHERE entry_id = ?", (fact_id,)
    ).fetchone()[0]
    vectors = store._conn.execute(
        "SELECT COUNT(*) FROM memory2_embeddings WHERE entry_id = ?", (fact_id,)
    ).fetchone()[0]
    assert fts == 0
    assert vectors == 0
    service.close()


def test_fact_source_tombstone_stays_referenceable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    fact_id = store.upsert_fact(_fact("f1", workspace_id=service.workspace_id)).fact_id

    service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    sources = store.list_fact_sources(fact_id)
    assert [(item.source_event_id, item.source_revision) for item in sources] == [("ev-1", 1)]
    assert store.facts_by_source_event("ev-1") == [fact_id]
    service.close()


def test_edit_supersedes_instead_of_revoking(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.journal = _Journal({"ev-2": "edit"})
    store = service.store
    fact_id = store.upsert_fact(
        _fact("f1", workspace_id=service.workspace_id, source_event_id="ev-2")
    ).fact_id

    report = service.invalidate_sources(["ev-2"], now_ms=T0 + 10)

    assert report.superseded == (fact_id,)
    assert report.revoked == ()
    fact = store.get_fact(fact_id)
    assert fact is not None
    assert fact.assertion_status == "superseded"
    assert fact.superseded_by == "edit:ev-2"
    assert FactReadGate(store).recheck([fact_id], _ctx()) == frozenset()
    assert service.invalidate_sources(["ev-2"], now_ms=T0 + 20).superseded == ()
    service.close()


def test_permission_cache_is_empty_after_invalidation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    fact_id = store.upsert_fact(_fact("f1", workspace_id=service.workspace_id)).fact_id
    cache = FactPermissionCache()
    gate = FactReadGate(store, cache=cache)
    assert cache.audience_for(store, fact_id) == frozenset({"member-old"})
    assert gate.allowed_fact_ids(_ctx()) == frozenset({fact_id})

    service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    assert gate.allowed_fact_ids(_ctx()) == frozenset()
    assert gate.recheck([fact_id], _ctx()) == frozenset()
    service.close()


def test_cancelled_job_for_a_deleted_source_never_runs(tmp_path: Path) -> None:
    from yeoman_gateway.memory.extraction_jobs import SharedFactExtractionQueue

    service = _service(tmp_path)
    store = service.store
    store.upsert_fact(_fact("f1", workspace_id=service.workspace_id))
    queue = SharedFactExtractionQueue(
        store=store, extractor=lambda events: [], journal=_Journal({"ev-1": "delete"})
    )
    service.extraction = queue
    queue.enqueue(
        turn_ref="tu_1",
        source_refs=[("ev-1", 1)],
        now_ms=T0,
        workspace_id=service.workspace_id,
        chat_scope_key=CHAT,
    )

    report = service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    assert report.jobs_cancelled == 1
    assert queue.run_due(now_ms=T0 + 20).processed == 0
    assert store.list_fact_jobs()[0]["state"] == "cancelled"
    service.close()


def test_private_fact_never_becomes_group_public_by_invalidation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    private = replace(
        _fact("f1", workspace_id=service.workspace_id),
        visibility_scope="author_only",
        audience=frozenset(),
    )
    fact_id = store.upsert_fact(private).fact_id

    service.invalidate_sources(["ev-1"], now_ms=T0 + 10)

    fact = store.get_fact(fact_id)
    assert fact is not None
    assert fact.visibility_scope == "author_only"
    assert fact.audience == frozenset()
    service.close()


def test_legacy_writes_never_create_fact_rows(tmp_path: Path) -> None:
    service = _service(tmp_path)
    store = service.store
    store.upsert_fact(_fact("f1", workspace_id=service.workspace_id))
    before = len(store.list_facts())

    service.backfill_from_workspace_files()  # type: ignore[attr-defined]

    assert len(store.list_facts()) == before
    assert store.list_facts(include_inactive=False)[0].fact_id == "f1"
    service.close()
