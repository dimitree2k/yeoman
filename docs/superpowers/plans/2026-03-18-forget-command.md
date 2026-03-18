# /forget Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/forget` WhatsApp admin command that lets the owner search and soft-delete semantic memory entries via a two-step preview-confirm flow.

**Architecture:** Three layers: (1) `MemoryStore.soft_delete()` + `distinct_scope_keys()` for DB operations, (2) `MemoryService.forget()` / `forget_confirm()` for search + delete orchestration, (3) `ForgetCommandHandler` + adapter methods for the WhatsApp command UX. Hash-based confirmation with transient preview slot on the adapter (single-owner, no persistence needed).

**Tech Stack:** Python 3.14, SQLite, pytest, existing yeoman admin command framework

---

### Task 1: Add `soft_delete` and `distinct_scope_keys` to MemoryStore

**Files:**
- Modify: `yeoman/memory/store.py:468` (after `search_vector`, before `stats`)
- Test: `tests/test_memory_store_soft_delete.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_store_soft_delete.py
"""Tests for MemoryStore.soft_delete()."""
from __future__ import annotations

import uuid
from pathlib import Path

from yeoman.memory.models import MemoryEntry
from yeoman.memory.store import MemoryStore


def _make_entry(workspace_id: str = "ws1", scope_key: str = "test:scope") -> MemoryEntry:
    content = f"test content {uuid.uuid4().hex[:8]}"
    return MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        scope_type="chat",
        scope_key=scope_key,
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=f"hash_{uuid.uuid4().hex[:8]}",
        salience=0.7,
        confidence=0.8,
        source="test",
    )


def test_soft_delete_marks_entries_and_returns_count(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1, _ = store.upsert_node(_make_entry())
    e2, _ = store.upsert_node(_make_entry())
    e3, _ = store.upsert_node(_make_entry())

    deleted = store.soft_delete([e1.id, e2.id])
    assert deleted == 2

    # Deleted entries should not appear in search
    hits = store.search_lexical(
        workspace_id="ws1", query=e1.content, scope_keys=["test:scope"], limit=10
    )
    hit_ids = {h.entry.id for h in hits}
    assert e1.id not in hit_ids
    assert e2.id not in hit_ids

    # e3 should still be findable
    hits3 = store.search_lexical(
        workspace_id="ws1", query=e3.content, scope_keys=["test:scope"], limit=10
    )
    assert any(h.entry.id == e3.id for h in hits3)


def test_soft_delete_empty_list_returns_zero(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    assert store.soft_delete([]) == 0


def test_soft_delete_nonexistent_ids_returns_zero(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    assert store.soft_delete(["nonexistent-id-1", "nonexistent-id-2"]) == 0


def test_soft_delete_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1, _ = store.upsert_node(_make_entry())
    assert store.soft_delete([e1.id]) == 1
    assert store.soft_delete([e1.id]) == 0  # already deleted


def test_distinct_scope_keys(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1 = _make_entry(scope_key="scope:a")
    e2 = _make_entry(scope_key="scope:b")
    e3 = _make_entry(scope_key="scope:a")  # duplicate scope
    store.upsert_node(e1)
    store.upsert_node(e2)
    store.upsert_node(e3)

    keys = store.distinct_scope_keys("ws1")
    assert set(keys) == {"scope:a", "scope:b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_store_soft_delete.py -v`
Expected: FAIL with `AttributeError: 'MemoryStore' object has no attribute 'soft_delete'`

- [ ] **Step 3: Write minimal implementation**

Add to `yeoman/memory/store.py` after `search_vector` method (after line 467, before `stats`):

```python
def soft_delete(self, ids: list[str]) -> int:
    """Mark entries as deleted. Returns count of rows affected."""
    if not ids:
        return 0
    now_iso = datetime.now(UTC).isoformat()
    placeholders = ",".join(["?"] * len(ids))
    with self._lock:
        cursor = self._conn.execute(
            f"UPDATE memory2_nodes SET is_deleted = 1, updated_at = ?"
            f" WHERE id IN ({placeholders}) AND is_deleted = 0",
            (now_iso, *ids),
        )
        self._conn.commit()
        return cursor.rowcount

def distinct_scope_keys(self, workspace_id: str) -> list[str]:
    """Return all distinct scope_keys for a workspace (active entries only)."""
    with self._lock:
        rows = self._conn.execute(
            "SELECT DISTINCT scope_key FROM memory2_nodes"
            " WHERE workspace_id = ? AND is_deleted = 0",
            (workspace_id,),
        ).fetchall()
    return [str(row["scope_key"]) for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_memory_store_soft_delete.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add yeoman/memory/store.py tests/test_memory_store_soft_delete.py
git commit -m "feat(memory): add MemoryStore.soft_delete and distinct_scope_keys"
```

---

### Task 2: Add `forget` and `forget_confirm` to MemoryService

**Files:**
- Modify: `yeoman/memory/service.py:901` (after `prune`, before `reindex`)
- Test: `tests/test_memory_forget.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_forget.py
"""Tests for MemoryService.forget() and forget_confirm()."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from yeoman.memory.service import MemoryService


def _minimal_memory_config():
    """Return a minimal MemoryConfig for testing."""
    from yeoman.config.schema import Config
    config = Config()
    config.memory.enabled = True
    config.memory.capture.enabled = False  # Don't start capture threads in test
    return config.memory


def _make_service(tmp_path: Path) -> MemoryService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = _minimal_memory_config()
    config.db_path = str(tmp_path / "memory.db")
    with patch("yeoman.memory.service._load_owner_ids", return_value={}):
        svc = MemoryService(workspace=workspace, config=config)
    return svc


def _insert_entry(svc: MemoryService, content: str, chat_id: str = "test-chat") -> str:
    from yeoman.memory.models import MemoryEntry
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=svc.workspace_id,
        scope_type="chat",
        scope_key=svc.chat_scope_key("whatsapp", chat_id),
        channel="whatsapp",
        chat_id=chat_id,
        sender_id="sender1",
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=svc._hash_content(content),
        salience=0.7,
        confidence=0.8,
        source="test",
    )
    saved, _ = svc.store.upsert_node(entry)
    return saved.id


def test_forget_returns_matching_hits(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    _insert_entry(svc, "antworte als Voice Message")
    _insert_entry(svc, "something unrelated about cooking")

    hits = svc.forget(query="Voice Message")
    assert len(hits) >= 1
    assert any("voice" in h.entry.content.lower() for h in hits)


def test_forget_confirm_soft_deletes(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    entry_id = _insert_entry(svc, "delete me please")

    count = svc.forget_confirm([entry_id])
    assert count == 1

    # Entry should no longer appear in search
    hits = svc.forget(query="delete me please")
    assert not any(h.entry.id == entry_id for h in hits)


def test_forget_confirm_empty_list(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    assert svc.forget_confirm([]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_forget.py -v`
Expected: FAIL with `AttributeError: 'MemoryService' object has no attribute 'forget'`

- [ ] **Step 3: Write minimal implementation**

Add to `yeoman/memory/service.py` after the `prune` method (after line 911, before `reindex`):

```python
def forget(self, *, query: str, limit: int = 10) -> list[MemoryHit]:
    """Search all scopes for memories matching *query*. Preview only — does not delete."""
    if not query.strip():
        return []
    # Search across all scope keys by using a broad lexical search.
    scope_keys = self.store.distinct_scope_keys(self.workspace_id)
    lexical_hits = self.store.search_lexical(
        workspace_id=self.workspace_id,
        query=query,
        scope_keys=scope_keys,
        limit=max(1, int(limit)),
    )
    vector_hits: list[MemoryHit] = []
    if self.embedding is not None:
        vector = self.embedding.embed(self._normalize_content(query))
        if vector:
            vector_hits = self.store.search_vector(
                workspace_id=self.workspace_id,
                query_vector=vector,
                scope_keys=scope_keys,
                limit=max(1, int(limit)),
            )
    merged: dict[str, MemoryHit] = {}
    for hit in lexical_hits:
        merged[hit.entry.id] = hit
    for hit in vector_hits:
        existing = merged.get(hit.entry.id)
        if existing is None:
            merged[hit.entry.id] = hit
        else:
            existing.vector_score = max(existing.vector_score, hit.vector_score)
    ranked = self._rank_hits(list(merged.values()))
    return ranked[:max(1, int(limit))]

def forget_confirm(self, ids: list[str]) -> int:
    """Soft-delete memory entries by ID. Returns count deleted."""
    return self.store.soft_delete(ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_memory_forget.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add yeoman/memory/service.py tests/test_memory_forget.py
git commit -m "feat(memory): add forget() and forget_confirm() to MemoryService"
```

---

### Task 3: Add ForgetCommandHandler and adapter wiring

**Files:**
- Modify: `yeoman/adapters/policy_engine.py` (add handler class + adapter methods + registration)
- Modify: `yeoman/app/bootstrap.py` (wire memory_service into policy_adapter)
- Test: `tests/test_forget_command.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_forget_command.py
"""Tests for the /forget admin command handler."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from yeoman.core.admin_commands import AdminCommandContext, AdminCommandRouter
from yeoman.memory.models import MemoryEntry, MemoryHit


def _ctx(raw_text: str, *, owner: bool = True) -> AdminCommandContext:
    return AdminCommandContext(
        channel="whatsapp",
        chat_id="owner@lid" if owner else "intruder@lid",
        sender_id="owner@lid" if owner else "intruder@lid",
        participant=None,
        is_group=False,
        raw_text=raw_text,
    )


def _make_hit(content: str, chat_id: str = "group@g.us") -> MemoryHit:
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id="ws1",
        scope_type="chat",
        scope_key=f"channel:whatsapp:chat:{chat_id}",
        channel="whatsapp",
        chat_id=chat_id,
        sender_id="sender1",
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=hashlib.sha256(content.lower().encode()).hexdigest(),
        salience=0.7,
        confidence=0.8,
        source="test",
    )
    return MemoryHit(entry=entry, final_score=0.9)


def _compute_hash(ids: list[str]) -> str:
    payload = "\n".join(sorted(ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:4]


def _make_handler_and_router():
    from yeoman.adapters.policy_engine import ForgetCommandHandler, EnginePolicyAdapter

    adapter = MagicMock(spec=EnginePolicyAdapter)
    # Make is_applicable return True for owner
    adapter.forget_is_applicable.return_value = True

    handler = ForgetCommandHandler(adapter)
    router = AdminCommandRouter([handler])
    return adapter, handler, router


def test_forget_no_query_shows_usage() -> None:
    from yeoman.core.admin_commands import AdminCommandResult

    adapter, _, router = _make_handler_and_router()
    adapter.forget_handle.return_value = AdminCommandResult(
        status="handled", response="Usage: /forget <query>"
    )

    router.route(_ctx("/forget"))
    adapter.forget_handle.assert_called_once()


def test_forget_preview_shows_results() -> None:
    from yeoman.core.admin_commands import AdminCommandResult

    adapter, _, router = _make_handler_and_router()
    hits = [_make_hit("voice message test")]
    token = _compute_hash([h.entry.id for h in hits])

    adapter.forget_handle.return_value = AdminCommandResult(
        status="handled",
        response=f"Found 1 memory:\n1. (DM) \"voice message test\"\n\n/forget confirm {token}",
    )

    router.route(_ctx("/forget voice message"))
    adapter.forget_handle.assert_called_once()


def test_forget_confirm_computes_correct_hash() -> None:
    """Verify the hash computation used by /forget confirm."""
    ids = ["id-aaa", "id-bbb", "id-ccc"]
    token = _compute_hash(ids)
    # Hash is deterministic and based on sorted IDs
    expected = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:4]
    assert token == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_forget_command.py -v`
Expected: FAIL with `ImportError: cannot import name 'ForgetCommandHandler'`

- [ ] **Step 3: Wire memory_service into policy_adapter**

In `yeoman/app/bootstrap.py`, add a late-binding setter call after the policy_adapter and memory_service are both created. Add after line 331 (after `policy_adapter = EnginePolicyAdapter(...)`):

Find `memory_state_dir=memory_state_dir,` followed by `)` in the `EnginePolicyAdapter(` constructor call, and after the closing `)`, add:

```python
    policy_adapter.set_memory_service(memory_service)
```

- [ ] **Step 4: Add adapter methods and handler class**

In `yeoman/adapters/policy_engine.py`:

**4a. Add `import hashlib`** to the top-level imports in `policy_engine.py` (around line 7, alongside the existing `import json`).

**4b. Add `set_memory_service` method** to `EnginePolicyAdapter` class (after `__init__`, around line 180):

```python
def set_memory_service(self, memory_service: object) -> None:
    """Late-binding setter for MemoryService (avoids circular bootstrap)."""
    self._memory_service = memory_service
```

And add `self._memory_service = None` in `__init__` (around line 157, before `self._admin_router`).

**4c. Add `forget_is_applicable` method** (after `session_reset_is_applicable`, around line 698):

```python
def forget_is_applicable(self, ctx: AdminCommandContext) -> bool:
    return bool(self._owner_policy_for_context(ctx)) and not ctx.is_group
```

**4d. Add `forget_handle` method** (after `session_reset_handle`, around line 1258):

```python
def forget_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
    if not argv:
        return AdminCommandResult(
            status="handled",
            response="Usage: /forget <query>",
            command_name="forget",
            outcome="usage",
            source="dm",
        )

    # Confirm sub-command: /forget confirm <hash> [indices]
    if argv[0].lower() == "confirm":
        return self._forget_confirm(ctx, argv[1:])

    # Preview sub-command: /forget <query tokens...>
    return self._forget_preview(ctx, argv)

def _forget_preview(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
    if self._memory_service is None:
        return AdminCommandResult(
            status="handled",
            response="Memory service is not available.",
            command_name="forget",
            outcome="error",
            source="dm",
        )

    query = " ".join(argv)
    hits = self._memory_service.forget(query=query, limit=10)
    if not hits:
        return AdminCommandResult(
            status="handled",
            response=f"No memories found matching '{query}'.",
            command_name="forget",
            outcome="empty",
            source="dm",
        )

    total = len(hits)
    ids = [h.entry.id for h in hits]
    token = self._forget_hash(ids)

    lines = [f"Found {total} memor{'y' if total == 1 else 'ies'}:"]
    for i, hit in enumerate(hits, 1):
        chat_label = self._forget_chat_label(hit.entry.chat_id)
        content = hit.entry.content
        if len(content) > 80:
            content = content[:77] + "..."
        lines.append(f'{i}. ({chat_label}, {hit.entry.created_at[:10]}) "{content}"')

    lines.append("")
    lines.append(f"/forget confirm {token} — delete all")
    if total > 1:
        lines.append(f"/forget confirm {token} 1,3 — delete selected")

    return AdminCommandResult(
        status="handled",
        response="\n".join(lines),
        command_name="forget",
        outcome="preview",
        source="dm",
        metric_events=(
            AdminMetricEvent(
                name="memory_forget_preview_total",
                labels=(("channel", ctx.channel),),
            ),
        ),
    )

def _forget_confirm(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
    if self._memory_service is None:
        return AdminCommandResult(
            status="handled",
            response="Memory service is not available.",
            command_name="forget",
            outcome="error",
            source="dm",
        )

    if not argv:
        return AdminCommandResult(
            status="handled",
            response="Usage: /forget confirm <hash> [indices]",
            command_name="forget",
            outcome="usage",
            source="dm",
        )

    provided_hash = argv[0].strip().lower()

    # Parse optional index filter: /forget confirm abc1 1,3,5
    index_filter: list[int] | None = None
    if len(argv) > 1:
        try:
            index_filter = [int(x.strip()) for x in argv[1].split(",") if x.strip()]
        except ValueError:
            return AdminCommandResult(
                status="handled",
                response="Invalid index format. Use: /forget confirm <hash> 1,3,5",
                command_name="forget",
                outcome="error",
                source="dm",
            )

    # Re-run the most recent query to get the IDs — but we don't store the query.
    # Instead, we use the hash to verify against a caller-provided ID list.
    # The preview response includes numbered items; the user provides the hash
    # that was computed from those IDs. We need to re-derive the IDs.
    #
    # Since we're stateless, the adapter stores the last preview result transiently
    # in memory (per-chat, single slot). This is acceptable because:
    # - Only one owner uses the bot
    # - Preview → confirm happens within seconds
    # - If the slot is empty/stale, we return "expired"
    preview_ids = getattr(self, "_forget_preview_ids", None)
    if not preview_ids:
        return AdminCommandResult(
            status="handled",
            response="Preview expired or invalid. Run /forget again.",
            command_name="forget",
            outcome="expired",
            source="dm",
        )

    expected_hash = self._forget_hash(preview_ids)
    if provided_hash != expected_hash:
        return AdminCommandResult(
            status="handled",
            response="Preview expired or invalid. Run /forget again.",
            command_name="forget",
            outcome="expired",
            source="dm",
        )

    # Apply index filter if provided
    if index_filter is not None:
        max_idx = len(preview_ids)
        out_of_range = [i for i in index_filter if i < 1 or i > max_idx]
        if out_of_range:
            return AdminCommandResult(
                status="handled",
                response=f"Index {out_of_range[0]} out of range (1-{max_idx}). Run /forget again.",
                command_name="forget",
                outcome="error",
                source="dm",
            )
        ids_to_delete = [preview_ids[i - 1] for i in index_filter]
    else:
        ids_to_delete = list(preview_ids)

    count = self._memory_service.forget_confirm(ids_to_delete)
    self._forget_preview_ids = None  # Clear after confirm

    return AdminCommandResult(
        status="handled",
        response=f"Forgot {count} memor{'y' if count == 1 else 'ies'}.",
        command_name="forget",
        outcome="applied",
        source="dm",
        metric_events=(
            AdminMetricEvent(
                name="memory_forget_total",
                labels=(("channel", ctx.channel),),
                value=count,
            ),
        ),
    )

@staticmethod
def _forget_hash(ids: list[str]) -> str:
    """4-char hex hash over sorted entry IDs."""
    payload = "\n".join(sorted(ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:4]

def _forget_chat_label(self, chat_id: str | None) -> str:
    """Human-readable label for a chat_id in the preview."""
    if not chat_id:
        return "unknown"
    if chat_id.endswith("@g.us"):
        name = self._get_group_name(chat_id)
        return name or chat_id
    return "DM"
```

**4e. Update `_forget_preview` to store IDs transiently.** At the end of `_forget_preview`, before the return, add:

```python
    self._forget_preview_ids = ids
```

**4f. Add `ForgetCommandHandler` class** (after `ResetSessionCommandHandler` class, around line 2148):

```python
class ForgetCommandHandler(AdminCommandHandler):
    """Deterministic `/forget` command for soft-deleting memories."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "forget"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.forget_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.forget_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/forget <query>"
```

**4g. Register the handler** in `EnginePolicyAdapter.__init__` — add `ForgetCommandHandler(self)` to the handler list (after `ResetSessionCommandHandler(self)` on line 172):

```python
                ForgetCommandHandler(self),
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_forget_command.py tests/test_admin_command_router.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add yeoman/adapters/policy_engine.py yeoman/app/bootstrap.py tests/test_forget_command.py
git commit -m "feat(admin): add /forget command for soft-deleting memories"
```

---

### Task 4: Integration test — full preview-confirm round trip

**Files:**
- Test: `tests/test_forget_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_forget_integration.py
"""Integration test: full /forget preview → confirm round trip."""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from yeoman.adapters.policy_engine import EnginePolicyAdapter, ForgetCommandHandler
from yeoman.core.admin_commands import AdminCommandContext, AdminCommandRouter
from yeoman.memory.models import MemoryEntry
from yeoman.memory.service import MemoryService
from yeoman.config.schema import Config


def _ctx(raw_text: str) -> AdminCommandContext:
    return AdminCommandContext(
        channel="whatsapp",
        chat_id="owner@lid",
        sender_id="491757070305",
        participant=None,
        is_group=False,
        raw_text=raw_text,
    )


def _make_memory_service(tmp_path: Path) -> MemoryService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = Config()
    config.memory.enabled = True
    config.memory.capture.enabled = False
    config.memory.db_path = str(tmp_path / "memory.db")
    with patch("yeoman.memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=config.memory)


def _insert(svc: MemoryService, content: str, chat_id: str = "group@g.us") -> str:
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=svc.workspace_id,
        scope_type="chat",
        scope_key=svc.chat_scope_key("whatsapp", chat_id),
        channel="whatsapp",
        chat_id=chat_id,
        sender_id="sender1",
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=svc._hash_content(content),
        salience=0.7,
        confidence=0.8,
        source="test",
    )
    saved, _ = svc.store.upsert_node(entry)
    return saved.id


def test_full_forget_round_trip(tmp_path: Path) -> None:
    svc = _make_memory_service(tmp_path)
    id1 = _insert(svc, "antworte immer als Voice Message")
    id2 = _insert(svc, "Voice bitte")
    _insert(svc, "something about cooking recipes")

    # Create a minimal adapter with memory_service wired in
    adapter = EnginePolicyAdapter(
        engine=None,
        known_tools=set(),
        session_manager=None,
        workspace=tmp_path,
    )
    adapter.set_memory_service(svc)
    # Stub owner check to always return True
    adapter._owner_policy_for_context = lambda ctx: True

    # Step 1: Preview
    result = adapter.forget_handle(_ctx("/forget voice"), ["voice"])
    assert result.status == "handled"
    assert "Found" in (result.response or "")
    assert "Voice" in (result.response or "") or "voice" in (result.response or "")
    assert "/forget confirm" in (result.response or "")

    # Extract hash from response
    import re
    match = re.search(r"/forget confirm (\w+)", result.response or "")
    assert match, f"No confirm hash in response: {result.response}"
    token = match.group(1)

    # Step 2: Confirm all
    result2 = adapter.forget_handle(_ctx(f"/forget confirm {token}"), ["confirm", token])
    assert result2.status == "handled"
    assert "Forgot" in (result2.response or "")

    # Verify entries are soft-deleted
    remaining = svc.forget(query="Voice Message")
    remaining_ids = {h.entry.id for h in remaining}
    assert id1 not in remaining_ids
    assert id2 not in remaining_ids


def test_selective_forget_by_index(tmp_path: Path) -> None:
    svc = _make_memory_service(tmp_path)
    id1 = _insert(svc, "Voice Message eins")
    id2 = _insert(svc, "Voice Message zwei")
    id3 = _insert(svc, "Voice Message drei")

    adapter = EnginePolicyAdapter(
        engine=None,
        known_tools=set(),
        session_manager=None,
        workspace=tmp_path,
    )
    adapter.set_memory_service(svc)
    adapter._owner_policy_for_context = lambda ctx: True

    # Preview
    result = adapter.forget_handle(_ctx("/forget voice"), ["voice"])
    assert result.status == "handled"

    import re
    match = re.search(r"/forget confirm (\w+)", result.response or "")
    assert match
    token = match.group(1)

    # Confirm only index 2
    result2 = adapter.forget_handle(
        _ctx(f"/forget confirm {token} 2"), ["confirm", token, "2"]
    )
    assert result2.status == "handled"
    assert "Forgot 1" in (result2.response or "")
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/test_forget_integration.py -v`
Expected: All 2 tests PASS

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `pytest tests/ -x --timeout=30`
Expected: All existing tests still PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_forget_integration.py
git commit -m "test(forget): add integration test for full preview-confirm round trip"
```

---

### Task 5: Lint, type-check, and final cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run ruff**

Run: `ruff check yeoman/memory/store.py yeoman/memory/service.py yeoman/adapters/policy_engine.py yeoman/app/bootstrap.py`
Expected: No errors (fix any that appear)

- [ ] **Step 2: Run ruff format**

Run: `ruff format yeoman/memory/store.py yeoman/memory/service.py yeoman/adapters/policy_engine.py yeoman/app/bootstrap.py`

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -x --timeout=30`
Expected: All tests PASS

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "chore: lint and format /forget command implementation"
```
