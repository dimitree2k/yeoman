# Yeoman Overseer — Phase 2 Design: LLM Tier (Read-Only)

**Parent spec:** `docs/superpowers/specs/2026-03-18-overseer-design.md`
**Prerequisite:** Phase 1 deterministic foundation running and tested.

---

## Goal

Add an LLM tier to the overseer. When a runbook sets `escalate_to_llm: true`, the trigger evaluator routes it to a new `agent/` module instead of the deterministic executor. The agent can reason about observations and query the system — but cannot write or modify anything. Phase 2 establishes the agent loop, context assembly, budget tracking, and a read-only tool set.

---

## New Module: `agent/`

```
packages/overseer/yeoman_overseer/agent/
├── __init__.py
├── context.py        # Assembles per-invocation LLM context
├── loop.py           # Agent loop: invoke model, dispatch tools, enforce limits
├── budget.py         # Daily token + call tracking, 80%/100% thresholds
└── tools/
    ├── __init__.py   # Tool registry and dispatch
    ├── read_file.py
    ├── query_db.py
    ├── query_memory.py
    ├── check_health.py
    ├── git_log.py
    └── send_alert.py
```

---

## Context Assembly (`context.py`)

Each invocation assembles a compact context from four sources:

```
System:       "You are the yeoman overseer. You maintain system health,
               governance, and evolution. You have no user contact.
               You report to the owner via digest, not conversation."

Runbook:      Full runbook Markdown (the active runbook)
Observations: Structured data from the trigger check that fired
              (~500-byte JSON: gateway status, uptime, disk, error counts)
Audit log:    Last 20 entries filtered to the runbook's domain
              (via AuditLogger.read_recent(limit=20, domain=runbook.meta.domain))
Tombstones:   Recently retired features in this domain
              (via AuditLogger.query_tombstones(domain=runbook.meta.domain))
```

The trigger evaluator pre-formats observations as structured data before passing them in. The agent never receives raw log output. This keeps context compact and prevents attention dilution.

**Note:** `query_tombstones()` in Phase 1 only filters by `name`. Phase 2 adds a `domain` keyword argument to `query_tombstones()` in `audit/logger.py` to support domain-filtered tombstone injection.

---

## Agent Loop (`loop.py`)

1. Check budget — abort if at 100% daily ceiling
2. Assemble context via `context.py`
3. Call the LLM with the tool definitions
4. Dispatch tool calls: validate args → execute → log to audit → append result
5. Enforce `llm_budget.max_tool_calls` (default 100) and `llm_budget.max_tokens` (default 30,000) hard limits — terminate if exceeded
6. On completion: return a structured `AgentResult` (summary, tool calls made, tokens used, domain)
7. `service.py` buffers `AgentResult` into the daily digest

The loop is blocking and non-streaming. The overseer runs background tasks; latency is not a concern.

**Model routing:** `loop.py` reads `llm_profile` from the runbook's `llm_budget` object and looks it up in `config.json` `models.profiles`. Falls back to a new `overseerDefault` profile. The owner controls cost and capability per runbook independently of Arvid's routes.

---

## Budget System (`budget.py`)

Tracks daily token consumption and API call count across all LLM runbooks. `BudgetTracker` reads and writes the `budget` key in `state.json`, which Phase 2 extends with two new sub-keys:

```json
"budget": {
  "actions_hour": 0,
  "llm_daily": 0,
  "tokens_daily": 0,
  "budget_reset_date": "2026-03-20"
}
```

`BudgetTracker.consume(tokens, calls)` checks whether `budget_reset_date` matches today's date. If not, it resets `tokens_daily`, `llm_daily`, and `budget_reset_date` before recording. This reset survives service restarts — the date comparison is the sole mechanism.

| Threshold | Behavior |
|-----------|----------|
| < 80% | Normal operation |
| 80% | Only `domain: health` runbooks may escalate to LLM |
| 100% | No LLM calls until midnight; deterministic-only mode |

`service.py` initializes `BudgetTracker` on boot and passes it to `_on_runbook_triggered` for budget checks before agent dispatch.

---

## Tool Set (Read-Only / Safe)

### Summary

| Tool | Safety | Behavior |
|------|--------|----------|
| `read_file` | read-only | Read files under `~/.yeoman/` or `~/Documents/yeoman/`. Explicit deny-list blocks secrets. |
| `query_db` | read-only | Run SELECT on any SQLite DB via read-only URI connection. |
| `query_memory` | read-only | Semantic search on `memory.db`. Returns formatted results. |
| `check_health` | read-only | Delegates to `trigger/checks.py`. Returns structured check result. |
| `git_log` | read-only | Inspect git history of source repo or internal overseer git. |
| `send_alert` | safe | Send via `comms/cascading.py`. Audit-logged. Dispatched sequentially within a single agent invocation. |

### `read_file` — security detail

Path validation enforces two layers:

1. **Root allowlist:** path must resolve under `~/.yeoman/` or `~/Documents/yeoman/`. Symlink traversal and `..` sequences are resolved before checking.
2. **Explicit deny-list:** even within the allowed roots, the following are rejected:
   - Any path containing `.env` as a path component (e.g., `~/.yeoman/.env`)
   - Any path under `secrets/` (e.g., `~/.yeoman/secrets/`)
   - Any path under `.git/` (prevents reading credential files or hooks)

Rationale: a poisoned observation could instruct the LLM to read `.env` and exfiltrate secrets via `send_alert` or the audit log. The deny-list makes this architecturally impossible regardless of the LLM's instructions.

### `query_db` — security detail

Connects using SQLite's read-only URI mode:

```python
sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

This enforces read-only access at the SQLite engine level — the file descriptor is opened with `O_RDONLY`. No application-level SQL parsing or `SELECT`-prefix checking is needed or used. Attempts to write, modify the schema, or run `PRAGMA writable_schema=ON` fail at the engine before any query executes. CTE-wrapped mutations and comment-obfuscated statements are equally blocked.

---

## Schema Extensions

### `runbook/schema.py`

`RunbookFrontmatter` already has `escalate_to_llm: bool`, `llm_budget: LLMBudget | None`, and `domain: str`. Phase 2 makes two targeted changes:

1. **Extend `LLMBudget`** with a `llm_profile` field and updated defaults:

```python
class LLMBudget(BaseModel):
    max_tokens: int = 30_000       # raised from 4096
    max_tool_calls: int = 100      # raised from 10
    llm_profile: str = "overseerDefault"
```

2. **No other `RunbookFrontmatter` changes.** All required fields (`escalate_to_llm`, `domain`) already exist.

### `audit/logger.py`

Add optional LLM fields to `AuditEntry`:

```python
@dataclass
class AuditEntry:
    ...                                          # existing fields unchanged
    llm_tokens_used: int | None = None
    llm_tool_calls: int | None = None
    llm_profile: str | None = None
    reasoning_summary: str | None = None
```

Add `domain` keyword argument to `query_tombstones()`:

```python
def query_tombstones(self, *, name: str | None = None, domain: str | None = None) -> list[dict]:
    # filter by domain if provided
```

---

## Phase 1 Integration Points

| File | Change |
|------|--------|
| `runbook/schema.py` | Extend `LLMBudget`: add `llm_profile`, raise `max_tokens`/`max_tool_calls` defaults |
| `audit/logger.py` | Add optional LLM fields to `AuditEntry`; add `domain` filter to `query_tombstones()` |
| `state.py` | Add `tokens_daily: int = 0` and `budget_reset_date: str = ""` to the `budget` dict (backward-compatible — existing keys preserved). Widen `budget` type annotation from `dict[str, int]` to `dict[str, Any]` to accommodate the string date value. |
| `service.py` | Initialize `BudgetTracker` in `init()`; in `_on_runbook_triggered`, branch on `runbook.meta.escalate_to_llm`: False → deterministic executor, True → `agent/loop.py` |
| `config.json` | Add `overseerDefault` model profile (e.g., `claude-haiku-4-5` for cost-efficiency; owner adjusts as needed) |

---

## Starter LLM Runbooks

Shipped with Phase 2 into `packages/overseer/yeoman_overseer/starter_runbooks/` (copied to `data_dir/runbooks/` on first boot by `service.py`):

| Runbook | Trigger | Domain | Tools used |
|---------|---------|--------|-----------|
| `memory-hygiene.md` | cron daily | memory | `query_memory`, `query_db`, `send_alert` |
| `governance-policy-audit.md` | cron weekly | governance | `read_file`, `query_db`, `send_alert` |
| `quality-response-sample.md` | cron weekly | quality | `read_file`, `query_db`, `send_alert` |

---

## Tests

| Test file | Coverage |
|-----------|----------|
| `test_agent_context.py` | Audit log slicing, domain-filtered tombstone injection, system state formatting |
| `test_agent_loop.py` | Tool dispatch, hard limit enforcement, budget abort, `AgentResult` structure |
| `test_agent_budget.py` | 80%/100% thresholds, date-based midnight reset, `state.json` persistence |
| `test_tool_read_file.py` | Path traversal rejection, root allowlist, deny-list (`.env`, `secrets/`, `.git/`) |
| `test_tool_query_db.py` | Read-only URI connection, write attempt rejected at engine level |
| `test_tool_query_memory.py` | Semantic search result formatting |
| `test_tool_check_health.py` | Correct delegation to `trigger/checks.py` |
| `test_tool_git_log.py` | Log parsing, empty repo handling |
| `test_tool_send_alert.py` | Delegation to cascading comms, audit entry |
| `test_runbook_schema_llm.py` | Updated `LLMBudget` defaults and `llm_profile` field |
| `test_audit_entry_llm.py` | Optional LLM fields serialise and round-trip correctly |

---

## What Phase 2 Does Not Include

- Write tools (`write_file`, `edit_file`) — Phase 3
- `shell` via bubblewrap — Phase 3
- `requires_tests` gate — Phase 3
- `create_runbook` / `modify_runbook` — Phase 4
- Cryptographic runbook signing — Phase 4
- Owner signature workflow — Phase 4
- Event bus triggers — Phase 4

See `docs/superpowers/specs/future-considerations.md` for deferred architectural items (Workspace Pattern, typed DB APIs, ephemeral DB replica, A2A).
