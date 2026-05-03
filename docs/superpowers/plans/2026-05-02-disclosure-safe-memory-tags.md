# Disclosure-Safe Memory Tags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add owner-controlled memory sensitivity/topic metadata and prevent private or taboo memories from being injected raw into normal reply prompts.

**Architecture:** Keep the existing SQLite memory store and `MemoryEntry.meta_json`. Add a small disclosure helper module for metadata parsing, narrow deterministic classification, and rendering decisions, then apply it inside `MemoryService._render_hits()` before `[Retrieved Memory]` is built and inside automatic capture when new memories are written. Add CLI options for manual tagging, an edit command for existing memory metadata, and no-model retagging for old memories.

**Tech Stack:** Python, Typer, SQLite, pytest, existing Yeoman gateway memory/responder modules.

---

## File Structure

- Create: `packages/gateway/yeoman_gateway/memory/disclosure.py`
  - Owns metadata normalization, explicit-topic matching, owner-context checks, and render decisions.
- Modify: `packages/gateway/yeoman_gateway/memory/service.py`
  - Persists manual metadata, updates metadata by entry ID, and renders retrieved memory through the disclosure gate.
- Modify: `packages/gateway/yeoman_gateway/memory/store.py`
  - Adds small lookup/update helpers for one memory row.
- Modify: `packages/gateway/yeoman_gateway/cli/memory_commands.py`
  - Adds `--topics`, `--sensitivity`, `--disclosure`, `--subjects` to `memory add`, displays metadata in `memory search`, and adds `memory tag`.
- Modify: `tests/gateway/test_memory_disclosure.py`
  - Unit tests for metadata parsing and render behavior.
- Modify: `tests/test_memory_cli.py`
  - CLI coverage for manual metadata and tag updates.
- Modify: `tests/test_responder_memory_recall.py`
  - Regression test proving taboo content is not injected raw into the responder prompt.

## Task 1: Disclosure Helper

**Files:**
- Create: `packages/gateway/yeoman_gateway/memory/disclosure.py`
- Test: `tests/gateway/test_memory_disclosure.py`

- [ ] **Step 1: Write failing disclosure tests**

Add tests that construct `MemoryEntry` values with `meta_json` and assert:

```python
def test_taboo_memory_renders_guardrail_without_raw_content() -> None:
    hit = _hit(
        "Timo's father died last year.",
        {"topics": ["funeral"], "sensitivity": "taboo", "disclosure_mode": "never_initiate"},
    )
    rendered = render_disclosed_hits([hit], query="Why is Timo quiet?", owner_context=False)
    assert "father died" not in rendered
    assert "[Private Context Guardrails]" in rendered


def test_taboo_memory_renders_raw_when_owner_explicitly_raises_topic() -> None:
    hit = _hit(
        "Timo's father died last year.",
        {"topics": ["funeral"], "sensitivity": "taboo", "disclosure_mode": "never_initiate"},
    )
    rendered = render_disclosed_hits([hit], query="funeral details?", owner_context=True)
    assert "father died" in rendered
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/gateway/test_memory_disclosure.py -q
```

Expected: import failure because `yeoman_gateway.memory.disclosure` does not exist.

- [ ] **Step 3: Implement helper**

Create:

```python
SENSITIVITIES = {"normal", "sensitive", "private", "taboo"}
DISCLOSURE_MODES = {"speakable", "context_only", "owner_only", "never_initiate"}

@dataclass(frozen=True, slots=True)
class DisclosureMetadata:
    topics: tuple[str, ...] = ()
    sensitivity: str = "normal"
    disclosure_mode: str = "speakable"
    subjects: tuple[str, ...] = ()
    notes: str | None = None
```

Expose:

```python
def normalize_metadata(raw: Mapping[str, object] | str | None) -> DisclosureMetadata: ...
def metadata_to_json_dict(...) -> dict[str, object]: ...
def render_disclosed_hits(hits: list[MemoryHit], *, query: str, owner_context: bool, max_chars: int, include_trace: bool = False) -> str: ...
```

- [ ] **Step 4: Run helper tests and verify they pass**

Run:

```bash
uv run pytest tests/gateway/test_memory_disclosure.py -q
```

Expected: all tests pass.

## Task 2: Memory Store And Service Integration

**Files:**
- Modify: `packages/gateway/yeoman_gateway/memory/store.py`
- Modify: `packages/gateway/yeoman_gateway/memory/service.py`
- Test: `tests/gateway/test_memory_disclosure.py`
- Test: `tests/test_responder_memory_recall.py`

- [ ] **Step 1: Add failing service tests**

Add tests proving:

```python
def test_record_manual_persists_disclosure_metadata(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    entry, _ = svc.record_manual(..., topics=["funeral"], sensitivity="taboo", disclosure_mode="never_initiate")
    assert json.loads(entry.meta_json)["topics"] == ["funeral"]
```

and:

```python
@pytest.mark.asyncio
async def test_responder_does_not_inject_taboo_memory_raw(tmp_path: Path) -> None:
    # Seed taboo memory, send semantically related but non-explicit message,
    # inspect provider.messages_seen system prompts.
    assert "father died" not in json.dumps(provider.messages_seen[-1])
    assert "Private Context Guardrails" in json.dumps(provider.messages_seen[-1])
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/gateway/test_memory_disclosure.py tests/test_responder_memory_recall.py::test_responder_does_not_inject_taboo_memory_raw -q
```

Expected: missing arguments/methods or raw memory still injected.

- [ ] **Step 3: Implement store helpers**

Add:

```python
def get_node(self, entry_id: str, *, workspace_id: str) -> MemoryEntry | None: ...
def update_node_meta(self, entry_id: str, *, workspace_id: str, meta_json: str) -> MemoryEntry | None: ...
```

- [ ] **Step 4: Implement service metadata paths**

Update `record_manual()` to accept optional `topics`, `sensitivity`,
`disclosure_mode`, and `subjects`. Add:

```python
def update_disclosure_metadata(
    self,
    entry_id: str,
    *,
    topics: list[str] | None = None,
    sensitivity: str | None = None,
    disclosure_mode: str | None = None,
    subjects: list[str] | None = None,
) -> MemoryEntry | None: ...
```

Update `_render_hits()` to call `render_disclosed_hits(...)`.

- [ ] **Step 5: Run service/responder tests**

Run:

```bash
uv run pytest tests/gateway/test_memory_disclosure.py tests/test_responder_memory_recall.py -q
```

Expected: all selected tests pass.

## Task 3: Memory CLI Metadata Controls

**Files:**
- Modify: `packages/gateway/yeoman_gateway/cli/memory_commands.py`
- Test: `tests/test_memory_cli.py`

- [ ] **Step 1: Add failing CLI tests**

Extend `test_memory_cli_commands_end_to_end()` or add a focused test:

```python
add = runner.invoke(app, ["memory", "add", "--text", "...", "--kind", "fact", "--topics", "funeral,family", "--sensitivity", "taboo", "--disclosure", "never_initiate"])
assert add.exit_code == 0
search = runner.invoke(app, ["memory", "search", "--query", "funeral"])
assert "taboo" in search.output
assert "funeral" in search.output
```

Add `memory tag` coverage:

```python
tag = runner.invoke(app, ["memory", "tag", entry_id, "--sensitivity", "private", "--topics", "family"])
assert tag.exit_code == 0
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```bash
uv run pytest tests/test_memory_cli.py -q
```

Expected: unknown options/command.

- [ ] **Step 3: Implement CLI options and command**

Add choice validation for:

```python
MEMORY_SENSITIVITIES = {"normal", "sensitive", "private", "taboo"}
MEMORY_DISCLOSURES = {"speakable", "context_only", "owner_only", "never_initiate"}
```

Add comma-list parsing helper. Pass metadata to `record_manual()`. Add table
columns `Sensitivity` and `Topics`. Add `memory tag`.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
uv run pytest tests/test_memory_cli.py -q
```

Expected: all CLI tests pass.

## Task 4: Focused Verification And Runtime Restart

**Files:**
- Runtime command only; no source files expected.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
uv run pytest tests/gateway/test_memory_disclosure.py tests/test_memory_cli.py tests/test_responder_memory_recall.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run lint on touched Python files**

Run:

```bash
uv run ruff check packages/gateway/yeoman_gateway/memory/disclosure.py packages/gateway/yeoman_gateway/memory/service.py packages/gateway/yeoman_gateway/memory/store.py packages/gateway/yeoman_gateway/cli/memory_commands.py tests/gateway/test_memory_disclosure.py tests/test_memory_cli.py tests/test_responder_memory_recall.py
```

Expected: no lint errors.

- [ ] **Step 3: Restart gateway**

Run:

```bash
yeoman gateway restart
yeoman gateway status
```

Expected: gateway reports healthy/running after restart.

## Self-Review

Spec coverage:

- Metadata schema: Task 1 and Task 2.
- Pre-generation disclosure gate: Task 1 and Task 2.
- CLI owner controls: Task 3.
- No graph dependency: no graph files or graph tables appear in the plan.
- No post-generation review: no responder output-review step appears in the plan.

Placeholder scan:

- This plan contains no deferred implementation placeholders.

Type consistency:

- `sensitivity`, `disclosure_mode`, `topics`, and `subjects` are used consistently across helper, service, and CLI tasks.
