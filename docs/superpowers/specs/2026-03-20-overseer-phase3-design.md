# Yeoman Overseer — Phase 3 Design: Write Tools + Bubblewrap

**Parent spec:** `docs/superpowers/specs/2026-03-18-overseer-design.md`
**Prerequisite:** Phase 2 LLM read-only tier running and tested.

---

## Goal

Extend the LLM tier from read-only to read-write. The agent can now write files, edit code, prune memory, run tests, and execute sandboxed shell commands. All mutations are audited and git-committed. A `requires_tests` gate protects code changes: it creates a staging branch, runs pytest, and rolls back automatically on failure. The `shell` tool runs inside a bubblewrap sandbox with no network access and an ephemeral tmpdir per invocation.

**Phasing note:** The parent spec groups write tools and self-evolution together as "Phase 3." This implementation splits them: write tools ship here (Phase 3), and self-evolution (`create_runbook`, `modify_runbook`, cryptographic signing, owner approval) ships in Phase 4. The split provides a stable mutation foundation before enabling self-modification.

---

## New Files

### `agent/tools/` additions

```
write_file.py          # Audited write, auto-committed to internal git
edit_file.py           # Audited patch, auto-committed
prune_memory.py        # Delete memory.db entries by criteria, snapshot-first
run_tests.py           # Execute pytest inside sandbox, return structured pass/fail
git_revert.py          # Revert a previous overseer internal git commit
dry_run_runbook.py     # Evaluate a runbook without executing (validates parse + action plan)
shell.py               # Run command inside bubblewrap sandbox
```

### `agent/sandbox.py`

Bubblewrap wrapper. Constructs and executes the `bwrap` invocation for `shell` and `run_tests`. Lives at `yeoman_overseer/agent/sandbox.py`.

### `agent/staging.py`

Staging branch manager for the `requires_tests` gate. Lives at `yeoman_overseer/agent/staging.py`.

---

## Bubblewrap Sandbox (`sandbox.py`)

```
bwrap
  # System read-only (Python interpreter, stdlib, installed packages)
  --ro-bind  /usr           /usr
  --ro-bind  /lib           /lib
  --ro-bind  /lib64         /lib64      (if present)
  --ro-bind  /bin           /bin
  --ro-bind  /sbin          /sbin
  --ro-bind  /etc/ld.so.cache /etc/ld.so.cache

  # Application read-only
  --ro-bind  ~/.yeoman/            ~/.yeoman/
  --ro-bind  ~/Documents/yeoman/   ~/Documents/yeoman/

  # Ephemeral writable tmp (fresh per invocation)
  --bind     /tmp/overseer-{uuid}  /tmp

  --proc     /proc
  --dev      /dev
  --unshare-net                     (no network — blocks socket attacks)
  --unshare-pid                     (isolated process namespace)
  --die-with-parent                 (kill sandbox children if parent is killed)

  # Explicitly excluded: var/run/, secrets/, socket files
```

`sandbox.py` checks for `bwrap` on first use via `shutil.which("bwrap")`. Raises `SandboxUnavailableError` if not found. `bwrap` is declared as a system dependency in the overseer package.

A fresh tmpdir is created per invocation and deleted on completion or failure (via `finally` block).

**Escape prevention:** `write_file` commits to the real filesystem (audited). The `shell` sandbox mounts everything read-only except the ephemeral `/tmp`. The sandbox sees only the last *committed* state — uncommitted `write_file` changes are invisible inside it. This severs the write-then-execute escape vector.

**pytest environment:** `run_tests.py` passes `PYTEST_CACHE_DIR=/tmp/pytest-cache` and `--basetemp=/tmp/pytest-tmp` to the pytest invocation so all cache and temp output lands in the writable `/tmp`, not in the read-only source tree.

---

## Tool Behaviors

### `write_file`
- Writes to any path under `~/.yeoman/` or `~/Documents/yeoman/`
- Blocked from writing to `runbooks/` — `create_runbook` (Phase 4) handles runbook creation
- Auto-commits to the internal overseer git with a structured commit message
- Audit-logged with path, content hash, and commit SHA

### `edit_file`
- Applies a unified diff patch to an existing file
- Same path restrictions and audit behavior as `write_file`
- Blocked from editing files in `runbooks/`

### `prune_memory`
- Takes a snapshot of `memory.db` before any deletion
- Accepts criteria: age (days), salience threshold (float), domain (string)
- Audit-logged with criteria, rows deleted, and snapshot path

### `run_tests`
- Executes `pytest` inside the bubblewrap sandbox
- Passes `PYTEST_CACHE_DIR=/tmp/pytest-cache` and `--basetemp=/tmp/pytest-tmp`
- Returns `{"passed": bool, "total": int, "failed": int, "output": str}`
- Any runbook can call `run_tests` directly; the `requires_tests` gate also calls it internally

### `git_revert`
- Reverts a single commit in the internal overseer git by SHA
- Audit-logged with target SHA and result
- Restricted to the internal overseer git only — cannot revert commits in `~/Documents/yeoman/`
- Does not apply to runbook files (runbooks are modified via Phase 4 tools only)

### `dry_run_runbook`
- Evaluates a runbook against current system state without executing
- Validates: frontmatter parses, trigger condition is well-formed, action vocabulary is recognised
- Returns `{"valid": bool, "trigger_would_fire": bool, "action_plan": list, "issues": list}`
- In Phase 3, this tool is available for LLM use but the `requires_tests` gate does not invoke it (since `write_file`/`edit_file` are blocked from `runbooks/`). It becomes load-bearing in Phase 4 when runbook files can be modified.

### `shell`
- Runs a command string inside the bubblewrap sandbox
- Returns `{"stdout": str, "stderr": str, "exit_code": int}`
- Timeout: `runbook.meta.safety.shell_timeout_s` seconds (default 60, configurable per runbook)
- A single `shell_timeout_s` value applies to every `shell` call in the runbook invocation

---

## `requires_tests` Gate

`requires_tests` already lives in `SafetyConfig` (`safety.requires_tests: bool = False`). No schema change needed. Phase 3 adds `shell_timeout_s` to `SafetyConfig`:

```python
class SafetyConfig(BaseModel):
    ...                          # existing fields unchanged
    shell_timeout_s: int = 60
```

The gate is enforced in `loop.py` via `StagingManager` (`agent/staging.py`). When `runbook.meta.safety.requires_tests` is `True`, any `write_file` or `edit_file` call on a code file follows this flow:

```
write_file / edit_file on a code file
    └── requires_tests: true?
            ├── no  → write directly, audit, commit to internal git
            └── yes → StagingManager.create_staging_branch()
                        └── write to staging branch
                              └── run_tests (inside sandbox)
                                      ├── fail → StagingManager.rollback(), log, alert owner, abort
                                      └── pass → StagingManager.merge_to_main(), audit commit
```

**`StagingManager` (`agent/staging.py`):**
- `create_staging_branch(run_id: str) -> str` — creates `overseer-staging-{run_id}` in the internal git
- `write_on_branch(branch: str, path: Path, content: str)` — commits file change to the staging branch
- `merge_to_main(branch: str)` — fast-forward merges staging branch to main, deletes branch
- `rollback(branch: str)` — deletes the staging branch without merging

`dry_run_runbook` is not invoked in the Phase 3 gate — `write_file`/`edit_file` cannot write to `runbooks/`, so there is no runbook to dry-run. The gate runs `run_tests` only. `dry_run_runbook` is added to the gate in Phase 4.

---

## Phase 3 Integration Points

| File | Change |
|------|--------|
| `runbook/schema.py` | Add `shell_timeout_s: int = 60` to `SafetyConfig` |
| `agent/loop.py` | Add staging branch logic: detect `requires_tests`, call `StagingManager` around write tool calls |
| `audit/logger.py` | No changes — write tools reuse the existing `AuditEntry` with `llm_tokens_used` / `llm_tool_calls` fields added in Phase 2 |

---

## Starter Runbooks

Shipped with Phase 3 into `packages/overseer/yeoman_overseer/starter_runbooks/`:

| Runbook | Trigger | Domain | Tools used | `requires_tests` |
|---------|---------|--------|-----------|-----------------|
| `ops-memory-prune.md` | cron weekly | memory | `prune_memory`, `send_alert` | false (config-only) |
| `ops-source-cleanup.md` | cron weekly | ops | `shell`, `send_alert` | false (temp file deletion, not source edits) |

`ops-source-cleanup.md` uses `shell` to delete stale temp files under `~/.yeoman/var/cache/` and `~/.yeoman/var/media/`. It does not write to source directories and does not need `requires_tests: true`.

---

## Tests

| Test file | Coverage |
|-----------|----------|
| `test_tool_write_file.py` | Path restrictions, audit log, git commit, `runbooks/` block |
| `test_tool_edit_file.py` | Patch application, audit log, `runbooks/` block |
| `test_tool_prune_memory.py` | Snapshot-first enforcement, criteria validation, audit entry |
| `test_tool_run_tests.py` | pytest execution inside sandbox, structured pass/fail output, `/tmp` env vars |
| `test_tool_git_revert.py` | Revert by SHA, audit entry, source repo rejection |
| `test_tool_dry_run_runbook.py` | Validate without executing, issue list output |
| `test_tool_shell.py` | Network blocked, tmpdir isolation, `--die-with-parent`, timeout, cleanup on failure |
| `test_agent_sandbox.py` | Bubblewrap wrapper: mount rules, fresh tmpdir, `SandboxUnavailableError` |
| `test_agent_staging.py` | Branch creation, merge, rollback, conflict handling |
| `test_requires_tests_gate.py` | Gate in `loop.py`: write on staging branch, pass→merge, fail→rollback+alert |

---

## What Phase 3 Does Not Include

- `create_runbook` / `modify_runbook` — Phase 4
- Cryptographic runbook signing (ed25519) — Phase 4
- Owner signature workflow for staging runbooks — Phase 4
- Tombstone system extensions — Phase 4
- Event bus triggers — Phase 4
- A2A protocol integration — future consideration
