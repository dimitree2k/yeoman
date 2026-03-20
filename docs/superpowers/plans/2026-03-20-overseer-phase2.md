# Overseer Phase 2: LLM Tier (Read-Only) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an LLM agent tier to the overseer — when a runbook sets `escalate_to_llm: true`, the trigger evaluator routes it to a new `agent/` module that can reason, query, and alert, but cannot write anything.

**Architecture:** A new `agent/` package provides context assembly, a blocking agent loop (Anthropic messages API with tool use), per-day budget tracking, and six read-only/safe tools. Integration is minimal: `service.py._on_runbook_triggered` branches on `escalate_to_llm`, and `state.py` gets two new budget fields.

**Tech Stack:** Python 3.14, anthropic SDK, pytest, existing yeoman_overseer Phase 1 modules

**Spec:** `docs/superpowers/specs/2026-03-20-overseer-phase2-design.md`

---

## File Map

**Create:**
```
packages/overseer/yeoman_overseer/agent/__init__.py
packages/overseer/yeoman_overseer/agent/budget.py
packages/overseer/yeoman_overseer/agent/context.py
packages/overseer/yeoman_overseer/agent/loop.py
packages/overseer/yeoman_overseer/agent/tools/__init__.py
packages/overseer/yeoman_overseer/agent/tools/read_file.py
packages/overseer/yeoman_overseer/agent/tools/query_db.py
packages/overseer/yeoman_overseer/agent/tools/query_memory.py
packages/overseer/yeoman_overseer/agent/tools/check_health.py
packages/overseer/yeoman_overseer/agent/tools/git_log.py
packages/overseer/yeoman_overseer/agent/tools/send_alert.py
packages/overseer/yeoman_overseer/starter_runbooks/memory-hygiene.md
packages/overseer/yeoman_overseer/starter_runbooks/governance-policy-audit.md
packages/overseer/yeoman_overseer/starter_runbooks/quality-response-sample.md
tests/overseer/test_schema_llm.py
tests/overseer/test_audit_llm.py
tests/overseer/test_agent_budget.py
tests/overseer/test_tool_read_file.py
tests/overseer/test_tool_query_db.py
tests/overseer/test_tool_query_memory.py
tests/overseer/test_tool_check_health.py
tests/overseer/test_tool_git_log.py
tests/overseer/test_tool_send_alert.py
tests/overseer/test_agent_context.py
tests/overseer/test_agent_loop.py
```

**Modify:**
```
packages/overseer/pyproject.toml               — add anthropic dependency
packages/overseer/yeoman_overseer/runbook/schema.py  — extend LLMBudget
packages/overseer/yeoman_overseer/audit/logger.py    — add LLM fields + domain tombstone filter
packages/overseer/yeoman_overseer/state.py           — extend budget dict, widen type
packages/overseer/yeoman_overseer/service.py         — add OverseerConfig token budget, wire agent
~/.yeoman/config.json                                — add overseerDefault model profile
```

---

### Task 1: Extend `LLMBudget` schema

**Files:**
- Modify: `packages/overseer/yeoman_overseer/runbook/schema.py`
- Test: `tests/overseer/test_schema_llm.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_schema_llm.py
from yeoman_overseer.runbook.schema import LLMBudget, RunbookFrontmatter

def test_llm_budget_new_defaults():
    b = LLMBudget()
    assert b.max_tokens == 30_000
    assert b.max_tool_calls == 100
    assert b.llm_profile == "overseerDefault"

def test_llm_budget_custom():
    b = LLMBudget(max_tokens=8000, max_tool_calls=20, llm_profile="overseerFast")
    assert b.llm_profile == "overseerFast"

def test_runbook_llm_budget_parses():
    import yaml
    raw = yaml.safe_load("""
name: test
domain: health
trigger:
  kind: cron
  expr: "0 * * * *"
escalate_to_llm: true
llm_budget:
  llm_profile: overseerFast
  max_tokens: 5000
""")
    from yeoman_overseer.runbook.schema import RunbookFrontmatter
    fm = RunbookFrontmatter(**raw)
    assert fm.escalate_to_llm is True
    assert fm.llm_budget.llm_profile == "overseerFast"
    assert fm.llm_budget.max_tokens == 5000
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && pytest tests/overseer/test_schema_llm.py -v
```
Expected: FAIL — `LLMBudget` has no `llm_profile` field, defaults are 4096/10.

- [ ] **Implement**

In `packages/overseer/yeoman_overseer/runbook/schema.py`, update `LLMBudget`:

```python
class LLMBudget(BaseModel):
    max_tokens: int = 30_000
    max_tool_calls: int = 100
    llm_profile: str = "overseerDefault"
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_schema_llm.py -v
```
Expected: 3 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/runbook/schema.py tests/overseer/test_schema_llm.py
git commit -m "feat(overseer): extend LLMBudget with llm_profile and raised defaults"
```

---

### Task 2: Extend `AuditEntry` and `query_tombstones`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/audit/logger.py`
- Test: `tests/overseer/test_audit_llm.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_audit_llm.py
import json, tempfile
from pathlib import Path
from yeoman_overseer.audit.logger import AuditEntry, AuditLogger, TombstoneEntry

def test_audit_entry_llm_fields_optional():
    e = AuditEntry(
        runbook="test", trigger="cron", action="escalate",
        target="", result="success", duration_ms=100,
        escalated_to_llm=True, domain="memory",
    )
    assert e.llm_tokens_used is None
    assert e.llm_tool_calls is None
    assert e.llm_profile is None
    assert e.reasoning_summary is None

def test_audit_entry_llm_fields_set():
    e = AuditEntry(
        runbook="test", trigger="cron", action="escalate",
        target="", result="success", duration_ms=500,
        escalated_to_llm=True, domain="memory",
        llm_tokens_used=1200, llm_tool_calls=3,
        llm_profile="overseerDefault", reasoning_summary="pruned 5 stale entries",
    )
    assert e.llm_tokens_used == 1200

def test_audit_entry_llm_roundtrips_json():
    e = AuditEntry(
        runbook="x", trigger="cron", action="a", target="", result="ok",
        duration_ms=10, escalated_to_llm=True, domain="health",
        llm_tokens_used=500, llm_tool_calls=2, llm_profile="p", reasoning_summary="r",
    )
    with tempfile.TemporaryDirectory() as d:
        logger = AuditLogger(Path(d))
        record = logger.append(e)
        assert record["llm_tokens_used"] == 500
        assert record["reasoning_summary"] == "r"

def test_query_tombstones_domain_filter():
    with tempfile.TemporaryDirectory() as d:
        logger = AuditLogger(Path(d))
        logger.write_tombstone(TombstoneEntry(
            entry_type="skill", name="weather", action="disable",
            reason="unused", runbook="audit", origin="auto",
        ))
        # domain field doesn't exist on TombstoneEntry yet - we're adding domain filter
        # to query method only; tombstones written without domain are returned for any domain
        results = logger.query_tombstones(domain="evolution")
        assert isinstance(results, list)
        results_no_filter = logger.query_tombstones()
        assert len(results_no_filter) == 1
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_audit_llm.py -v
```
Expected: FAIL — `AuditEntry` has no LLM fields, `query_tombstones` has no `domain` arg.

- [ ] **Implement**

In `packages/overseer/yeoman_overseer/audit/logger.py`:

Add fields to `AuditEntry` (after existing `budget_remaining`):
```python
@dataclass
class AuditEntry:
    runbook: str
    trigger: str
    action: str
    target: str
    result: str
    duration_ms: int
    escalated_to_llm: bool
    domain: str = ""
    budget_remaining: dict[str, Any] | None = None
    llm_tokens_used: int | None = None
    llm_tool_calls: int | None = None
    llm_profile: str | None = None
    reasoning_summary: str | None = None
```

Update `query_tombstones` signature:
```python
def query_tombstones(
    self, *, name: str | None = None, domain: str | None = None
) -> list[dict[str, Any]]:
    if not self._tombstone_path.exists():
        return []
    results: list[dict[str, Any]] = []
    for line in self._tombstone_path.read_text(encoding="utf-8").strip().splitlines():
        if not line:
            continue
        entry = json.loads(line)
        if name and entry.get("name") != name:
            continue
        # domain filter: skip only if tombstone has a domain set and it doesn't match
        if domain and entry.get("domain") and entry.get("domain") != domain:
            continue
        results.append(entry)
    return results
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_audit_llm.py -v
```
Expected: 4 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/audit/logger.py tests/overseer/test_audit_llm.py
git commit -m "feat(overseer): add LLM fields to AuditEntry and domain filter to query_tombstones"
```

---

### Task 3: Extend `OverseerState` budget and `OverseerConfig`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/state.py`
- Modify: `packages/overseer/yeoman_overseer/service.py`

- [ ] **Implement state changes** (no separate test — covered by Task 4's BudgetTracker tests)

In `packages/overseer/yeoman_overseer/state.py`:

Update `OverseerState.budget` field and `load()`:

```python
from typing import Any  # already imported

@dataclass
class OverseerState:
    ...
    budget: dict[str, Any] = field(
        default_factory=lambda: {
            "actions_hour": 0,
            "llm_daily": 0,
            "tokens_daily": 0,
            "budget_reset_date": "",
        }
    )
```

In `load()`, update the budget default:
```python
budget=raw.get("budget", {
    "actions_hour": 0, "llm_daily": 0,
    "tokens_daily": 0, "budget_reset_date": "",
}),
```

In `packages/overseer/yeoman_overseer/service.py`, add to `OverseerConfig`:
```python
@dataclass
class OverseerConfig:
    tick_interval_s: float = 1.0
    actions_per_hour: int = 30
    llm_calls_per_day: int = 20
    llm_tokens_per_day: int = 500_000      # new
    failure_threshold: int = 3
    max_quarantines: int = 3
```

- [ ] **Run existing overseer tests to confirm no regression**

```bash
pytest tests/overseer/ -v
```
Expected: all PASS

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/state.py packages/overseer/yeoman_overseer/service.py
git commit -m "feat(overseer): extend state budget dict and add llm_tokens_per_day to config"
```

---

### Task 4: Budget tracker

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/__init__.py`
- Create: `packages/overseer/yeoman_overseer/agent/budget.py`
- Test: `tests/overseer/test_agent_budget.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_agent_budget.py
from datetime import date
from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.state import OverseerState

def _tracker(calls_per_day=20, tokens_per_day=500_000):
    state = OverseerState()
    return BudgetTracker(state, calls_per_day=calls_per_day, tokens_per_day=tokens_per_day)

def test_can_call_llm_fresh():
    t = _tracker()
    assert t.can_call_llm("health") is True
    assert t.can_call_llm("memory") is True

def test_at_100_percent_blocks_all():
    t = _tracker(calls_per_day=1, tokens_per_day=100)
    t.consume(100, 1)
    assert t.can_call_llm("health") is False
    assert t.can_call_llm("memory") is False

def test_at_80_percent_blocks_non_health():
    t = _tracker(calls_per_day=10, tokens_per_day=1000)
    t.consume(800, 0)   # 80% of token budget
    assert t.can_call_llm("health") is True
    assert t.can_call_llm("memory") is False
    assert t.can_call_llm("governance") is False

def test_consume_persists_to_state():
    state = OverseerState()
    t = BudgetTracker(state, calls_per_day=20, tokens_per_day=500_000)
    t.consume(1500, 2)
    assert state.budget["tokens_daily"] == 1500
    assert state.budget["llm_daily"] == 2

def test_reset_on_new_day():
    state = OverseerState()
    state.budget["tokens_daily"] = 400_000
    state.budget["llm_daily"] = 15
    state.budget["budget_reset_date"] = "2020-01-01"   # old date
    t = BudgetTracker(state, calls_per_day=20, tokens_per_day=500_000)
    assert t.can_call_llm("memory") is True   # triggers reset
    assert state.budget["tokens_daily"] == 0
    assert state.budget["llm_daily"] == 0
    assert state.budget["budget_reset_date"] == date.today().isoformat()
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_agent_budget.py -v
```
Expected: FAIL — module does not exist.

- [ ] **Create `packages/overseer/yeoman_overseer/agent/__init__.py`** (empty)

- [ ] **Implement `packages/overseer/yeoman_overseer/agent/budget.py`**

```python
"""Daily LLM budget tracker — token + call ceilings with date-based reset."""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yeoman_overseer.state import OverseerState


class BudgetTracker:
    def __init__(
        self,
        state: OverseerState,
        *,
        calls_per_day: int,
        tokens_per_day: int,
    ) -> None:
        self._state = state
        self._calls_limit = calls_per_day
        self._tokens_limit = tokens_per_day

    def _reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self._state.budget.get("budget_reset_date") != today:
            self._state.budget["tokens_daily"] = 0
            self._state.budget["llm_daily"] = 0
            self._state.budget["budget_reset_date"] = today

    def _pct(self) -> float:
        """Return the higher of token% and call% consumed today."""
        self._reset_if_new_day()
        token_pct = self._state.budget.get("tokens_daily", 0) / self._tokens_limit
        call_pct = self._state.budget.get("llm_daily", 0) / self._calls_limit
        return max(token_pct, call_pct)

    def can_call_llm(self, domain: str) -> bool:
        pct = self._pct()
        if pct >= 1.0:
            return False
        if pct >= 0.8 and domain != "health":
            return False
        return True

    def consume(self, tokens: int, calls: int) -> None:
        self._reset_if_new_day()
        self._state.budget["tokens_daily"] = (
            self._state.budget.get("tokens_daily", 0) + tokens
        )
        self._state.budget["llm_daily"] = (
            self._state.budget.get("llm_daily", 0) + calls
        )
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_agent_budget.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/ tests/overseer/test_agent_budget.py
git commit -m "feat(overseer): add BudgetTracker with daily token+call ceilings and date reset"
```

---

### Task 5: Tool context and registry

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/__init__.py`

- [ ] **Implement** (no isolated test — covered by individual tool tests)

```python
# packages/overseer/yeoman_overseer/agent/tools/__init__.py
"""Tool registry: definitions for the Anthropic API and dispatch map."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_overseer.audit.logger import AuditLogger
from yeoman_overseer.comms.cascading import CascadingComms


@dataclass
class ToolContext:
    """Dependencies injected into every tool."""
    yeoman_home: Path
    source_dir: Path
    audit: AuditLogger
    comms: CascadingComms
    data_dir: Path


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a file under ~/.yeoman/ or ~/Documents/yeoman/. Sensitive paths (.env, secrets/, .git/) are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "query_db",
        "description": "Run a SELECT query on a SQLite database. Connection is read-only at the engine level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "db_path": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["db_path", "query"],
        },
    },
    {
        "name": "query_memory",
        "description": "Full-text search on the semantic memory database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_health",
        "description": "Run a built-in health check by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "check": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["check", "target"],
        },
    },
    {
        "name": "git_log",
        "description": "Read recent git log from the source repo or internal overseer git.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "enum": ["source", "internal"]},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "send_alert",
        "description": "Send an alert message via cascading comms (Telegram → SMTP → log).",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
    """Dispatch a tool call by name. Raises ValueError for unknown tools."""
    from yeoman_overseer.agent.tools import (
        check_health, git_log, query_db,
        query_memory, read_file, send_alert,
    )
    handlers: dict[str, Any] = {
        "read_file": read_file.execute,
        "query_db": query_db.execute,
        "query_memory": query_memory.execute,
        "check_health": check_health.execute,
        "git_log": git_log.execute,
        "send_alert": send_alert.execute,
    }
    if name not in handlers:
        raise ValueError(f"Unknown tool: {name!r}")
    result = handlers[name](args, ctx)
    if hasattr(result, "__await__"):  # handle async tool handlers (e.g. send_alert)
        result = await result
    return result
```

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/tools/__init__.py
git commit -m "feat(overseer): add ToolContext and tool registry"
```

---

### Task 6: `read_file` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/read_file.py`
- Test: `tests/overseer/test_tool_read_file.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_tool_read_file.py
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from yeoman_overseer.agent.tools.read_file import execute

def _ctx(home: Path, source: Path):
    ctx = MagicMock()
    ctx.yeoman_home = home
    ctx.source_dir = source
    return ctx

def test_read_allowed_file(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    (home / "config.json").write_text('{"x": 1}')
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / "config.json")}, ctx)
    assert '{"x": 1}' in result

def test_read_blocks_dot_env(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    (home / ".env").write_text("SECRET=abc")
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / ".env")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()

def test_read_blocks_secrets_dir(tmp_path):
    home = tmp_path / ".yeoman"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "creds.json").write_text('{}')
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(secrets / "creds.json")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()

def test_read_blocks_git_dir(tmp_path):
    home = tmp_path / ".yeoman"
    git_dir = home / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    (git_dir / "post-commit").write_text("#!/bin/bash\necho pwned")
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(git_dir / "post-commit")}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()

def test_read_blocks_path_outside_roots(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": "/etc/passwd"}, ctx)
    assert "blocked" in result.lower() or "denied" in result.lower()

def test_read_missing_file_returns_error(tmp_path):
    home = tmp_path / ".yeoman"
    home.mkdir()
    ctx = _ctx(home, tmp_path / "source")
    result = execute({"path": str(home / "nonexistent.txt")}, ctx)
    assert "not found" in result.lower() or "error" in result.lower()
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_tool_read_file.py -v
```
Expected: FAIL — module does not exist.

- [ ] **Implement `packages/overseer/yeoman_overseer/agent/tools/read_file.py`**

```python
"""read_file tool — read files under ~/.yeoman/ or ~/Documents/yeoman/."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext

_DENY_PARTS = {".env", "secrets", ".git"}


def _is_allowed(path: Path, ctx: ToolContext) -> bool:
    resolved = path.resolve()
    roots = [ctx.yeoman_home.resolve(), ctx.source_dir.resolve()]
    in_root = any(
        resolved == r or resolved.is_relative_to(r) for r in roots
    )
    if not in_root:
        return False
    # deny-list: reject if any path component matches
    for part in resolved.parts:
        if part in _DENY_PARTS:
            return False
    return True


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    path = Path(args["path"]).expanduser()
    if not _is_allowed(path, ctx):
        return f"[read_file] BLOCKED: {path} is outside allowed roots or in deny-list"
    if not path.exists():
        return f"[read_file] ERROR: file not found: {path}"
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"[read_file] ERROR: {exc}"
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_tool_read_file.py -v
```
Expected: 6 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/tools/read_file.py tests/overseer/test_tool_read_file.py
git commit -m "feat(overseer): add read_file tool with root allowlist and deny-list"
```

---

### Task 7: `query_db` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/query_db.py`
- Test: `tests/overseer/test_tool_query_db.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_tool_query_db.py
import sqlite3, tempfile
from pathlib import Path
from unittest.mock import MagicMock
from yeoman_overseer.agent.tools.query_db import execute

def _ctx():
    return MagicMock()

def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'alpha')")
    conn.execute("INSERT INTO items VALUES (2, 'beta')")
    conn.commit()
    conn.close()
    return db

def test_select_returns_rows(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "SELECT * FROM items"}, _ctx())
    assert "alpha" in result
    assert "beta" in result

def test_select_with_filter(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "SELECT name FROM items WHERE id = 1"}, _ctx())
    assert "alpha" in result

def test_write_attempt_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "INSERT INTO items VALUES (3, 'gamma')"}, _ctx())
    # Engine-level rejection: either sqlite3.OperationalError or our response contains error
    assert "error" in result.lower() or "readonly" in result.lower()

def test_drop_attempt_is_rejected(tmp_path):
    db = _make_db(tmp_path)
    result = execute({"db_path": str(db), "query": "DROP TABLE items"}, _ctx())
    assert "error" in result.lower() or "readonly" in result.lower()

def test_missing_db_returns_error(tmp_path):
    result = execute({"db_path": str(tmp_path / "nope.db"), "query": "SELECT 1"}, _ctx())
    assert "error" in result.lower()
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_tool_query_db.py -v
```
Expected: FAIL

- [ ] **Implement `packages/overseer/yeoman_overseer/agent/tools/query_db.py`**

```python
"""query_db tool — read-only SQLite access via mode=ro URI."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    db_path = Path(args["db_path"]).expanduser()
    query = args["query"]

    if not db_path.exists():
        return f"[query_db] ERROR: database not found: {db_path}"

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return f"[query_db] ERROR: {exc}"

    if not rows:
        return "[query_db] (no rows)"
    headers = rows[0].keys()
    lines = [" | ".join(str(r[h]) for h in headers) for r in rows]
    return "\n".join([" | ".join(headers), *lines])
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_tool_query_db.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/tools/query_db.py tests/overseer/test_tool_query_db.py
git commit -m "feat(overseer): add query_db tool with engine-level read-only enforcement"
```

---

### Task 8: `query_memory`, `check_health`, `git_log`, `send_alert` tools

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/query_memory.py`
- Create: `packages/overseer/yeoman_overseer/agent/tools/check_health.py`
- Create: `packages/overseer/yeoman_overseer/agent/tools/git_log.py`
- Create: `packages/overseer/yeoman_overseer/agent/tools/send_alert.py`
- Test: `tests/overseer/test_tool_remaining.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_tool_remaining.py
import sqlite3, subprocess, tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from yeoman_overseer.agent.tools.check_health import execute as check_health_execute
from yeoman_overseer.agent.tools.git_log import execute as git_log_execute
from yeoman_overseer.agent.tools.send_alert import execute as send_alert_execute

def _ctx(**kwargs):
    ctx = MagicMock()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx

# --- check_health ---
def test_check_health_delegates_to_checks():
    with patch("yeoman_overseer.agent.tools.check_health.run_check") as mock_check:
        mock_check.return_value = MagicMock(passed=True, value=72.5, message="ok")
        result = check_health_execute({"check": "disk_usage_above", "target": "/home"}, _ctx())
        assert "ok" in result or "72" in result
        mock_check.assert_called_once()

def test_check_health_unknown_check():
    result = check_health_execute({"check": "nonexistent_check", "target": "x"}, _ctx())
    assert "error" in result.lower() or "unknown" in result.lower()

# --- git_log ---
def test_git_log_source(tmp_path):
    # init a minimal git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    ctx = _ctx(source_dir=tmp_path)
    result = git_log_execute({"repo": "source", "limit": 5}, ctx)
    assert "init" in result

def test_git_log_empty_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    ctx = _ctx(source_dir=tmp_path)
    result = git_log_execute({"repo": "source", "limit": 5}, ctx)
    assert isinstance(result, str)  # no crash on empty repo

# --- send_alert ---
async def test_send_alert_calls_comms(tmp_path):
    from unittest.mock import AsyncMock
    comms = MagicMock()
    comms.send = AsyncMock(return_value=None)
    ctx = _ctx(comms=comms, audit=MagicMock())
    result = await send_alert_execute({"message": "test alert"}, ctx)
    comms.send.assert_called_once_with("test alert")
    assert "sent" in result.lower() or "ok" in result.lower()
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_tool_remaining.py -v
```
Expected: FAIL

- [ ] **Implement `check_health.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/check_health.py
"""check_health tool — delegates to trigger/checks.py."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def run_check(check_name: str, target: str) -> Any:
    """Import and run a check by name from trigger/checks."""
    from yeoman_overseer.trigger.checks import CHECKS
    if check_name not in CHECKS:
        raise ValueError(f"Unknown check: {check_name!r}")
    return CHECKS[check_name](target)


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        result = run_check(args["check"], args["target"])
        return f"[check_health] {args['check']}({args['target']}): passed={result.passed} value={result.value} message={result.message}"
    except ValueError as exc:
        return f"[check_health] ERROR: {exc}"
    except Exception as exc:
        return f"[check_health] ERROR: {exc}"
```

**Note:** The above assumes `trigger/checks.py` exposes a `CHECKS` dict mapping check names to callables. Check the actual implementation and adapt the import accordingly.

- [ ] **Implement `git_log.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/git_log.py
"""git_log tool — read git history from source or internal repo."""
from __future__ import annotations
import subprocess
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    repo = args.get("repo", "source")
    limit = int(args.get("limit", 20))
    cwd = ctx.source_dir if repo == "source" else ctx.data_dir
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--oneline", "--no-walk=unsorted"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # empty repo or other non-fatal error
            return f"[git_log] (no commits or git error: {result.stderr.strip()})"
        return result.stdout.strip() or "[git_log] (no commits)"
    except Exception as exc:
        return f"[git_log] ERROR: {exc}"
```

- [ ] **Implement `send_alert.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/send_alert.py
"""send_alert tool — send via CascadingComms (async), audit-logged."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


async def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    """CascadingComms.send is async — this tool must be async too."""
    message = args["message"]
    try:
        await ctx.comms.send(message)
        return "[send_alert] sent"
    except Exception as exc:
        return f"[send_alert] ERROR: {exc}"
```

- [ ] **Implement `query_memory.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/query_memory.py
"""query_memory tool — FTS search on memory.db."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    query = args["query"]
    limit = int(args.get("limit", 10))
    db_path = ctx.data_dir.parent / "memory" / "memory.db"

    if not db_path.exists():
        return "[query_memory] ERROR: memory.db not found"

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # FTS search via memory2_nodes_fts if available, else fallback
            rows = conn.execute(
                """
                SELECT n.id, n.content, n.salience, n.created_at
                FROM memory2_nodes n
                JOIN memory2_nodes_fts fts ON n.id = fts.rowid
                WHERE memory2_nodes_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS table not available — fallback to LIKE
            rows = conn.execute(
                "SELECT id, content, salience, created_at FROM memory2_nodes WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return f"[query_memory] ERROR: {exc}"

    if not rows:
        return "[query_memory] (no results)"
    return "\n".join(
        f"[{r['created_at']}] salience={r['salience']:.2f}: {r['content'][:200]}"
        for r in rows
    )
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_tool_remaining.py -v
```
Expected: all PASS

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/tools/ tests/overseer/test_tool_remaining.py
git commit -m "feat(overseer): add query_memory, check_health, git_log, send_alert tools"
```

---

### Task 9: Context assembler

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/context.py`
- Test: `tests/overseer/test_agent_context.py`

- [ ] **Write failing tests**

```python
# tests/overseer/test_agent_context.py
import json, tempfile
from pathlib import Path
from unittest.mock import MagicMock
from yeoman_overseer.agent.context import build_context, AgentContext
from yeoman_overseer.runbook.schema import RunbookFrontmatter, TriggerConfig, LLMBudget
from yeoman_overseer.runbook.parser import Runbook


def _runbook(domain="memory"):
    meta = RunbookFrontmatter(
        name="test-runbook",
        domain=domain,
        trigger=TriggerConfig(kind="cron", expr="0 3 * * *"),
        escalate_to_llm=True,
        llm_budget=LLMBudget(),
    )
    return Runbook(meta=meta, body="## Instructions\nCheck memory.")


def _audit(entries=None, tombstones=None):
    audit = MagicMock()
    audit.read_recent.return_value = entries or []
    audit.query_tombstones.return_value = tombstones or []
    return audit


def test_build_context_returns_agent_context():
    rb = _runbook()
    ctx = build_context(rb, {"disk_pct": 42}, _audit())
    assert isinstance(ctx, AgentContext)
    assert ctx.system_prompt
    assert ctx.user_message


def test_system_prompt_contains_identity():
    ctx = build_context(_runbook(), {}, _audit())
    assert "overseer" in ctx.system_prompt.lower()


def test_user_message_contains_runbook_name():
    ctx = build_context(_runbook(), {}, _audit())
    assert "test-runbook" in ctx.user_message


def test_user_message_contains_observations():
    ctx = build_context(_runbook(), {"error_rate": 0.05}, _audit())
    assert "error_rate" in ctx.user_message


def test_audit_log_filtered_by_domain():
    audit = _audit(entries=[
        {"domain": "memory", "runbook": "x", "action": "prune"},
        {"domain": "health", "runbook": "y", "action": "restart"},
    ])
    ctx = build_context(_runbook(domain="memory"), {}, audit)
    audit.read_recent.assert_called_once_with(limit=20, domain="memory")


def test_tombstones_filtered_by_domain():
    audit = _audit(tombstones=[{"name": "weather-skill", "domain": "memory"}])
    ctx = build_context(_runbook(domain="memory"), {}, audit)
    audit.query_tombstones.assert_called_once_with(domain="memory")
    assert "weather-skill" in ctx.user_message
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_agent_context.py -v
```
Expected: FAIL

- [ ] **Implement `packages/overseer/yeoman_overseer/agent/context.py`**

```python
"""Context assembly for LLM agent invocations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.audit.logger import AuditLogger
    from yeoman_overseer.runbook.parser import Runbook

_SYSTEM_PROMPT = """\
You are the yeoman overseer. You maintain system health, governance, and evolution.
You have no user contact. You report to the owner via digest, not conversation.
Take targeted, minimal actions. Prefer to observe and alert over modifying state.
If unsure, send an alert rather than acting."""


@dataclass
class AgentContext:
    system_prompt: str
    user_message: str


def build_context(
    runbook: Runbook,
    observations: dict[str, Any],
    audit: AuditLogger,
) -> AgentContext:
    audit_entries = audit.read_recent(limit=20, domain=runbook.meta.domain)
    tombstones = audit.query_tombstones(domain=runbook.meta.domain)

    parts = [
        f"## Active Runbook: {runbook.meta.name}",
        "",
        runbook.body.strip(),
        "",
        "## Observations",
        json.dumps(observations, indent=2),
    ]

    if audit_entries:
        parts += [
            "",
            f"## Recent Audit Log (domain={runbook.meta.domain}, last {len(audit_entries)})",
            *[json.dumps(e) for e in audit_entries],
        ]

    if tombstones:
        parts += [
            "",
            "## Recently Retired Features (tombstones)",
            *[f"- {t.get('name', '?')}: {t.get('reason', '?')}" for t in tombstones],
        ]

    return AgentContext(
        system_prompt=_SYSTEM_PROMPT,
        user_message="\n".join(parts),
    )
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_agent_context.py -v
```
Expected: 6 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/agent/context.py tests/overseer/test_agent_context.py
git commit -m "feat(overseer): add context assembler for LLM agent invocations"
```

---

### Task 10: Agent loop

**Files:**
- Modify: `packages/overseer/pyproject.toml` (add `anthropic`)
- Create: `packages/overseer/yeoman_overseer/agent/loop.py`
- Test: `tests/overseer/test_agent_loop.py`

- [ ] **Add `anthropic` dependency**

In `packages/overseer/pyproject.toml`, add to `dependencies`:
```toml
"anthropic>=0.50.0",
```

Then sync:
```bash
cd ~/Documents/yeoman && uv sync
```

- [ ] **Write failing tests**

```python
# tests/overseer/test_agent_loop.py
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
from yeoman_overseer.agent.loop import AgentLoop, AgentResult, BudgetExhaustedError
from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.runbook.schema import RunbookFrontmatter, TriggerConfig, LLMBudget
from yeoman_overseer.runbook.parser import Runbook
from yeoman_overseer.state import OverseerState


def _runbook(profile="overseerDefault", max_tool_calls=10, max_tokens=5000):
    meta = RunbookFrontmatter(
        name="test-runbook", domain="memory",
        trigger=TriggerConfig(kind="cron", expr="0 3 * * *"),
        escalate_to_llm=True,
        llm_budget=LLMBudget(
            llm_profile=profile,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
        ),
    )
    return Runbook(meta=meta, body="Check memory health.")


def _budget(exhausted=False):
    budget = MagicMock(spec=BudgetTracker)
    budget.can_call_llm.return_value = not exhausted
    return budget


def _fake_end_turn_response(summary="all good"):
    """Simulate an Anthropic response with stop_reason=end_turn."""
    block = MagicMock()
    block.type = "text"
    block.text = summary
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    return resp


def _fake_tool_then_end(tool_name="send_alert", tool_input=None, summary="done"):
    """Simulate a tool_use response followed by end_turn."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.id = "tu_001"
    tool_block.input = tool_input or {"message": "alert!"}

    tool_resp = MagicMock()
    tool_resp.stop_reason = "tool_use"
    tool_resp.content = [tool_block]
    tool_resp.usage.input_tokens = 200
    tool_resp.usage.output_tokens = 30

    end_block = MagicMock()
    end_block.type = "text"
    end_block.text = summary
    end_resp = MagicMock()
    end_resp.stop_reason = "end_turn"
    end_resp.content = [end_block]
    end_resp.usage.input_tokens = 250
    end_resp.usage.output_tokens = 40
    return [tool_resp, end_resp]


async def test_budget_exhausted_raises():
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(exhausted=True), config={})
    import pytest
    with pytest.raises(BudgetExhaustedError):
        await loop.run(_runbook(), {})


async def test_end_turn_returns_agent_result():
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(), config={})
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        client = mock_cls.return_value
        client.messages.create.return_value = _fake_end_turn_response("memory is healthy")
        result = await loop.run(_runbook(), {"entries": 100})
    assert isinstance(result, AgentResult)
    assert result.summary == "memory is healthy"
    assert result.tokens_used == 150


async def test_tool_call_dispatched_and_result_appended():
    from unittest.mock import AsyncMock
    tool_ctx = MagicMock()
    loop = AgentLoop(tool_ctx=tool_ctx, budget=_budget(), config={})
    responses = _fake_tool_then_end()
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        with patch("yeoman_overseer.agent.loop.dispatch", new_callable=AsyncMock) as mock_dispatch:
            mock_dispatch.return_value = "alert sent"
            client = mock_cls.return_value
            client.messages.create.side_effect = responses
            result = await loop.run(_runbook(), {})
    mock_dispatch.assert_called_once_with("send_alert", {"message": "alert!"}, tool_ctx)
    assert result.tool_calls_made == 1


async def test_budget_consumed_after_run():
    budget = _budget()
    loop = AgentLoop(tool_ctx=MagicMock(), budget=budget, config={})
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        client = mock_cls.return_value
        client.messages.create.return_value = _fake_end_turn_response()
        await loop.run(_runbook(), {})
    budget.consume.assert_called_once()
```

- [ ] **Run to verify failure**

```bash
pytest tests/overseer/test_agent_loop.py -v
```
Expected: FAIL

- [ ] **Implement `packages/overseer/yeoman_overseer/agent/loop.py`**

```python
"""Agent loop — invoke Anthropic API with tools, enforce limits, return AgentResult."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anthropic import Anthropic

from yeoman_overseer.agent.context import build_context
from yeoman_overseer.agent.tools import TOOL_DEFINITIONS, ToolContext, dispatch

if TYPE_CHECKING:
    from yeoman_overseer.agent.budget import BudgetTracker
    from yeoman_overseer.audit.logger import AuditLogger
    from yeoman_overseer.runbook.parser import Runbook
    from yeoman_overseer.runbook.schema import LLMBudget


class BudgetExhaustedError(Exception):
    pass


@dataclass
class AgentResult:
    runbook_name: str
    domain: str
    summary: str
    tool_calls_made: int
    tokens_used: int
    llm_profile: str


class AgentLoop:
    def __init__(
        self,
        tool_ctx: ToolContext,
        budget: BudgetTracker,
        config: dict[str, Any],  # models.profiles from config.json
    ) -> None:
        self._tool_ctx = tool_ctx
        self._budget = budget
        self._config = config

    async def run(self, runbook: Runbook, observations: dict[str, Any]) -> AgentResult:
        """Blocking agent loop — async because send_alert and future tools are async."""
        domain = runbook.meta.domain
        if not self._budget.can_call_llm(domain):
            raise BudgetExhaustedError(f"LLM budget exhausted for domain={domain}")

        llm_budget = runbook.meta.llm_budget
        from yeoman_overseer.runbook.schema import LLMBudget
        if llm_budget is None:
            llm_budget = LLMBudget()

        profile_name = llm_budget.llm_profile
        profile = self._config.get("models", {}).get("profiles", {}).get(profile_name, {})
        model = profile.get("model", "claude-haiku-4-5-20251001")

        context = build_context(runbook, observations, self._tool_ctx.audit)

        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": context.user_message}
        ]
        tool_calls_made = 0
        total_tokens = 0
        summary = ""

        while tool_calls_made <= llm_budget.max_tool_calls:
            remaining_tokens = llm_budget.max_tokens - total_tokens
            if remaining_tokens <= 0:
                break

            response = client.messages.create(
                model=model,
                max_tokens=min(4096, remaining_tokens),
                system=context.system_prompt,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        summary = block.text
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_calls_made += 1
                        try:
                            result = await dispatch(block.name, block.input, self._tool_ctx)
                        except Exception as exc:
                            result = f"ERROR: {exc}"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        self._budget.consume(total_tokens, 1)

        return AgentResult(
            runbook_name=runbook.meta.name,
            domain=domain,
            summary=summary,
            tool_calls_made=tool_calls_made,
            tokens_used=total_tokens,
            llm_profile=profile_name,
        )
```

- [ ] **Run to verify pass**

```bash
pytest tests/overseer/test_agent_loop.py -v
```
Expected: 4 PASSED

- [ ] **Commit**

```bash
git add packages/overseer/pyproject.toml packages/overseer/yeoman_overseer/agent/loop.py tests/overseer/test_agent_loop.py
git commit -m "feat(overseer): add agent loop with Anthropic tool-use, budget enforcement"
```

---

### Task 11: Wire agent into `service.py`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/service.py`

- [ ] **Implement**

Import and initialize in `OverseerService.init()`:

```python
from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.agent.loop import AgentLoop, AgentResult, BudgetExhaustedError
from yeoman_overseer.agent.tools import ToolContext
```

Add these two fields to the `OverseerService` dataclass (alongside existing `_git`, `_audit`, etc.):

```python
_comms: CascadingComms | None = None
_agent_loop: AgentLoop | None = None
```

Add the import at the top of `service.py`:
```python
from yeoman_overseer.comms.cascading import CascadingComms
```

In `init()`, after setting up `_audit` and `_state`, add:

```python
# Comms — no channels yet; local_log=True ensures alerts are never silently lost
self._comms = CascadingComms(channels=[], local_log=True)

# Load config for model profiles
import json
config_path = self.data_dir.parent / "config.json"
raw_config = json.loads(config_path.read_text()) if config_path.exists() else {}

tool_ctx = ToolContext(
    yeoman_home=self.data_dir.parent,
    source_dir=Path.home() / "Documents" / "yeoman",
    audit=self._audit,
    comms=self._comms,
    data_dir=self.data_dir,
)
budget = BudgetTracker(
    self._state,
    calls_per_day=self.config.llm_calls_per_day,
    tokens_per_day=self.config.llm_tokens_per_day,
)
self._agent_loop = AgentLoop(tool_ctx=tool_ctx, budget=budget, config=raw_config)
```

Update `_on_runbook_triggered()` to branch on `escalate_to_llm`. The Phase 1 path stays as-is (already logs an audit entry); Phase 2 adds the LLM branch above it:

```python
async def _on_runbook_triggered(self, runbook: Runbook, check_result: CheckResult) -> None:
    import time
    start = time.monotonic()
    logger.info("Runbook triggered: %s", runbook.meta.name)

    escalated = False
    result_str = "success"
    llm_tokens = llm_calls = reasoning = None

    if runbook.meta.escalate_to_llm and self._agent_loop:
        try:
            observations = {
                "check": check_result.value,
                "message": check_result.message,
            }
            agent_result = await self._agent_loop.run(runbook, observations)
            escalated = True
            llm_tokens = agent_result.tokens_used
            llm_calls = agent_result.tool_calls_made
            reasoning = agent_result.summary[:500] if agent_result.summary else None
        except BudgetExhaustedError as exc:
            result_str = f"budget_exhausted: {exc}"

    duration_ms = int((time.monotonic() - start) * 1000)
    if self._audit:
        self._audit.append(AuditEntry(
            runbook=runbook.meta.name,
            trigger=runbook.meta.trigger.kind,
            action="triggered",
            target=runbook.meta.trigger.condition.target if runbook.meta.trigger.condition else "",
            result=result_str,
            duration_ms=duration_ms,
            escalated_to_llm=escalated,
            domain=runbook.meta.domain,
            llm_tokens_used=llm_tokens,
            llm_tool_calls=llm_calls,
            reasoning_summary=reasoning,
        ))
```

**Note:** The Phase 1 deterministic executor path is not changed here. The existing `_on_runbook_triggered` in Phase 1 only logs an audit entry; the deterministic executor is invoked from `TriggerEvaluator` directly. Phase 2 only adds the LLM branch — when `escalate_to_llm=False`, the original flow (trigger evaluator → deterministic executor) runs unchanged below this async handler.

- [ ] **Run full overseer test suite**

```bash
pytest tests/overseer/ -v
```
Expected: all existing + new tests PASS

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/service.py
git commit -m "feat(overseer): wire BudgetTracker and AgentLoop into service._on_runbook_triggered"
```

---

### Task 12: Add `overseerDefault` config profile

**Files:**
- Modify: `~/.yeoman/config.json`

- [ ] **Add profile**

Open `~/.yeoman/config.json`. Under `models.profiles`, add:

```json
"overseerDefault": {
  "kind": "llm",
  "model": "claude-haiku-4-5-20251001",
  "provider": "anthropic",
  "maxTokens": 4096,
  "temperature": 0.2,
  "timeoutMs": 60000
}
```

Use a low temperature (0.2) — the overseer reasons about facts, not creative output.

- [ ] **Verify config loads**

```bash
python -c "import json; c = json.load(open('$HOME/.yeoman/config.json')); print(c['models']['profiles']['overseerDefault'])"
```
Expected: prints the profile dict.

- [ ] **Commit runtime config**

```bash
cd ~/.yeoman && git add config.json && git commit -m "feat: add overseerDefault model profile for LLM runbooks"
```

---

### Task 13: Starter LLM runbooks

**Files:**
- Create: `packages/overseer/yeoman_overseer/starter_runbooks/memory-hygiene.md`
- Create: `packages/overseer/yeoman_overseer/starter_runbooks/governance-policy-audit.md`
- Create: `packages/overseer/yeoman_overseer/starter_runbooks/quality-response-sample.md`

- [ ] **Create `memory-hygiene.md`**

```markdown
---
name: memory-hygiene
domain: memory
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 20
  max_tokens: 10000
trigger:
  kind: cron
  expr: "0 3 * * *"
safety:
  max_actions_per_hour: 2
  cooldown_s: 3600
---

## Memory Hygiene

Review the semantic memory database for stale or low-quality entries.

### Your task

1. Use `query_memory` to sample recent entries across different topics.
2. Use `query_db` to count entries by age and salience:
   ```sql
   SELECT COUNT(*), AVG(salience) FROM memory2_nodes WHERE created_at < date('now', '-90 days')
   ```
3. If more than 100 entries are older than 90 days with salience below 0.3, send an alert recommending a prune run.
4. Report a one-paragraph summary of memory health in your final response.

Do not delete anything. Observe and report only.
```

- [ ] **Create `governance-policy-audit.md`**

```markdown
---
name: governance-policy-audit
domain: governance
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 15
  max_tokens: 12000
trigger:
  kind: cron
  expr: "0 4 * * 0"
safety:
  max_actions_per_hour: 1
  cooldown_s: 86400
---

## Governance Policy Audit

Review the current policy configuration for anomalies or drift.

### Your task

1. Read `~/.yeoman/policy.example.json` to understand the expected schema.
2. Use `query_db` on the audit log to check for recent policy changes:
   ```sql
   SELECT runbook, action, result, ts FROM audit_entries WHERE domain = 'governance' ORDER BY ts DESC LIMIT 20
   ```
3. Check for newly detected chats by reading `~/.yeoman/data/seen_chats.json`.
4. If any chat IDs are present in seen_chats but absent from policy, flag them in an alert.
5. Summarize governance health in your final response.

Do not modify policy. Observe and report only.
```

- [ ] **Create `quality-response-sample.md`**

```markdown
---
name: quality-response-sample
domain: quality
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 15
  max_tokens: 15000
trigger:
  kind: cron
  expr: "0 5 * * 0"
safety:
  max_actions_per_hour: 1
  cooldown_s: 86400
---

## Response Quality Sampling

Sample recent message exchanges and assess response quality.

### Your task

1. Use `query_db` to find the most active chat IDs from the past week:
   ```sql
   SELECT to_chat, COUNT(*) as msgs FROM outbound_log
   WHERE sent_at > datetime('now', '-7 days')
   GROUP BY to_chat ORDER BY msgs DESC LIMIT 3
   ```
2. Read the inbound archive for one of those chats (e.g., `~/.yeoman/data/inbound/whatsapp_<chat_id>.jsonl`), last 20 lines.
3. Assess: Are responses on-topic? Is the tone consistent with the persona? Any factual errors?
4. If quality appears degraded, send an alert with specific examples.
5. Write a brief quality summary in your final response.

Do not contact users. Observe and report only.
```

- [ ] **Run integration smoke test**

```bash
pytest tests/overseer/ -v
```
Expected: all PASS

- [ ] **Commit**

```bash
git add packages/overseer/yeoman_overseer/starter_runbooks/
git commit -m "feat(overseer): add three starter LLM runbooks (memory-hygiene, governance-policy-audit, quality-response-sample)"
```

---

### Final: sync and verify

- [ ] **Sync dependencies**

```bash
cd ~/Documents/yeoman && uv sync
```

- [ ] **Run full test suite**

```bash
pytest tests/ -v --tb=short
```
Expected: all PASS (no regressions in gateway or shared tests).

- [ ] **Restart gateway to pick up changes**

```bash
yeoman gateway restart
```
