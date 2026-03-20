# Yeoman Overseer — Phase 3 Design: Write Tools + Bubblewrap

**Parent spec:** `docs/superpowers/specs/2026-03-18-overseer-design.md`
**Prerequisite:** Phase 2 LLM read-only tier running and tested.

---

## Goal

Extend the LLM tier from read-only to read-write. The agent can now write files, edit code, prune memory, run tests, and execute sandboxed shell commands. All mutations are audited and git-committed.

A `requires_tests` gate protects source code changes using the **CI/CD patch model**: the agent writes into an isolated `git worktree` in `/tmp/`, tests run against the worktree, and only on success does the overseer merge the patch into the live repo. The live working tree is never modified until tests pass.

The `shell` tool runs inside bubblewrap with no network access, sensitive paths masked, and a fresh tmpdir per individual invocation.

**Phasing note:** The parent spec groups write tools and self-evolution together as "Phase 3." This implementation splits them: write tools ship here (Phase 3), and self-evolution (`create_runbook`, `modify_runbook`, cryptographic signing, owner approval) ships in Phase 4. The split provides a stable mutation foundation before enabling self-modification.

---

## New Files

### `agent/tools/` additions

```
write_file.py          # Audited write, auto-committed; blocked from .git/, secrets/, runbooks/
edit_file.py           # Audited patch, auto-committed; same restrictions as write_file
prune_memory.py        # Delete memory.db entries by criteria, snapshot-first
run_tests.py           # Execute pytest inside sandbox, return structured pass/fail
git_revert.py          # Revert a previous overseer internal git commit
dry_run_runbook.py     # Evaluate a runbook without executing (validates parse + action plan)
shell.py               # Run command inside bubblewrap sandbox
```

### `agent/sandbox.py`

Bubblewrap wrapper. Constructs and executes the `bwrap` invocation. Lives at `yeoman_overseer/agent/sandbox.py`. Used by `shell` and `run_tests`.

### `agent/patcher.py`

CI/CD patch manager for the `requires_tests` gate. Lives at `yeoman_overseer/agent/patcher.py`. Manages isolated git worktrees in `/tmp/`.

---

## Bubblewrap Sandbox (`sandbox.py`)

Each tool call that uses the sandbox receives its **own unique UUID tmpdir**. A `shell` call and a subsequent `run_tests` call in the same agent invocation get different `/tmp/overseer-{uuid}/` directories — no state carries between them.

```
bwrap
  # System read-only (Python interpreter, stdlib, installed packages)
  --ro-bind  /usr             /usr
  --ro-bind  /lib             /lib
  --ro-bind  /lib64           /lib64        (if present)
  --ro-bind  /bin             /bin
  --ro-bind  /sbin            /sbin
  --ro-bind  /etc/ld.so.cache /etc/ld.so.cache

  # Application read-only
  --ro-bind  ~/.yeoman/            ~/.yeoman/
  --ro-bind  ~/Documents/yeoman/   ~/Documents/yeoman/

  # Sensitive path masking (hide contents within mounted tree)
  --tmpfs    ~/.yeoman/secrets/             (empty tmpfs hides secrets/)
  --ro-bind  /dev/null ~/.yeoman/.env       (zero-byte dummy hides .env)

  # Ephemeral writable tmp — unique UUID per invocation, not per agent loop
  --bind     /tmp/overseer-{uuid}  /tmp

  --proc     /proc
  --dev      /dev
  --unshare-net                     (no network — blocks socket attacks)
  --unshare-pid                     (isolated process namespace)
  --die-with-parent                 (kill sandbox children if parent is killed)
```

`sandbox.py` checks for `bwrap` via `shutil.which("bwrap")` on first use. Raises `SandboxUnavailableError` if not found. The tmpdir is created immediately before the `bwrap` call and deleted in a `finally` block.

**Sensitive path masking:** `--tmpfs` mounts an empty RAM disk over `secrets/`, making the directory appear empty inside the sandbox. `--ro-bind /dev/null ~/.yeoman/.env` replaces `.env` with a zero-byte read-only file. Both are belt-and-suspenders with the `write_file`/`read_file` deny-lists — if a future tool or refactor weakens the deny-list, the masking still holds.

**Per-invocation isolation:** Each `bwrap` call (each `shell` call, each `run_tests` call) gets a fresh UUID tmpdir. A malicious `shell` command cannot drop a poisoned `conftest.py` that a subsequent `run_tests` call would execute, because they map to different `/tmp/overseer-{uuid}/` directories.

**pytest environment:** `run_tests.py` passes `PYTEST_CACHE_DIR=/tmp/pytest-cache` and `--basetemp=/tmp/pytest-tmp` so all cache and temp output lands in the writable per-invocation `/tmp`.

---

## Tool Behaviors

### `write_file`

- Target path must resolve under `~/.yeoman/` or `~/Documents/yeoman/`
- **Deny-list** (applies within the allowed roots): `.git/`, `.env`, `secrets/`, `systemd/`, `runbooks/`
- When `requires_tests: true`: writes go to the active `PatchContext` worktree path (see Patcher below), not to the live filesystem. Live files unchanged until tests pass.
- When `requires_tests: false`: writes directly to the live filesystem, auto-committed to internal git
- Audit-logged with path, content hash, and commit SHA

### `edit_file`

- Applies a unified diff patch to an existing file
- Same path restrictions and deny-list as `write_file`
- Same `requires_tests` routing behavior as `write_file`

### `prune_memory`

- Takes a snapshot of `memory.db` before any deletion
- Accepts criteria: age (days), salience threshold (float), domain (string)
- Audit-logged with criteria, rows deleted, and snapshot path

### `run_tests`

- Executes `pytest` inside the bubblewrap sandbox
- Accepts an optional `source_root: Path` argument — if provided (e.g., a worktree path), the sandbox mounts it instead of `~/Documents/yeoman/`
- Returns `{"passed": bool, "total": int, "failed": int, "output": str}`
- Any runbook can call `run_tests` directly; the `requires_tests` gate also calls it internally with the worktree path

### `git_revert`

- Reverts a single commit in the internal overseer git by SHA
- Audit-logged with target SHA and result
- Restricted to the internal overseer git only — cannot revert commits in `~/Documents/yeoman/`
- Does not apply to runbook files (runbooks are modified via Phase 4 tools only)

### `dry_run_runbook`

- Evaluates a runbook against current system state without executing
- Validates: frontmatter parses, trigger condition is well-formed, action vocabulary is recognised
- Returns `{"valid": bool, "trigger_would_fire": bool, "action_plan": list, "issues": list}`
- In Phase 3, this tool is available for direct LLM use but the `requires_tests` gate does not invoke it — `write_file`/`edit_file` are blocked from `runbooks/`, so there is no runbook to dry-run. It becomes load-bearing in Phase 4 when runbook files can be modified.

### `shell`

- Runs a command string inside the bubblewrap sandbox (new UUID tmpdir per call)
- Returns `{"stdout": str, "stderr": str, "exit_code": int}`
- Timeout: `runbook.meta.safety.shell_timeout_s` seconds (default 60, configurable per runbook)
- A single `shell_timeout_s` value caps every `shell` call in the runbook invocation

---

## CI/CD Patch Model (`patcher.py`)

The `Patcher` replaces the `StagingManager` pattern. Rather than creating git branches in the live repo, it uses `git worktree` to spin up an isolated copy in `/tmp/`. The live working tree is never touched until tests pass.

### `PatchContext`

```python
@dataclass
class PatchContext:
    worktree_path: Path   # /tmp/overseer-wt-{run_id}/
    branch: str           # overseer-patch-{run_id}
    live_repo: Path       # ~/Documents/yeoman/
```

### `Patcher` API

- `create_worktree(live_repo: Path, run_id: str) -> PatchContext`
  Runs `git worktree add /tmp/overseer-wt-{run_id} -b overseer-patch-{run_id}`. Created lazily on the first write call that needs it.

- `translate_path(ctx: PatchContext, original: Path) -> Path`
  Maps `~/Documents/yeoman/foo/bar.py` → `/tmp/overseer-wt-{uuid}/foo/bar.py`.

- `apply(ctx: PatchContext) -> None`
  Merges the worktree branch into the live repo's main branch (`git merge`), then removes the worktree (`git worktree remove`). Audit-committed.

- `discard(ctx: PatchContext) -> None`
  Removes the worktree and deletes the branch without merging. Called on test failure or agent abort.

### `requires_tests` Gate Flow

`requires_tests` already lives in `SafetyConfig` (`safety.requires_tests: bool = False`). No schema change needed for the field itself. Phase 3 adds `shell_timeout_s` to `SafetyConfig`:

```python
class SafetyConfig(BaseModel):
    ...                          # existing fields unchanged
    shell_timeout_s: int = 60
```

The gate is enforced in `loop.py`. When `runbook.meta.safety.requires_tests` is `True`, `write_file`/`edit_file` calls on source code follow this flow:

```
write_file / edit_file on source code (~/Documents/yeoman/**/*.py)
    └── requires_tests: true?
            ├── no  → write directly to live path, audit, commit
            └── yes → Patcher.create_worktree() (lazy, once per invocation)
                        → Patcher.translate_path(original) → worktree path
                        → write to worktree path
                        (all writes accumulate in worktree during agent loop)
                        → run_tests(source_root=worktree_path) via sandbox
                                ├── fail → Patcher.discard(), log, alert owner, abort
                                └── pass → Patcher.apply(), audit commit
```

`dry_run_runbook` is not invoked in the Phase 3 gate. It is added in Phase 4.

---

## Phase 3 Integration Points

| File | Change |
|------|--------|
| `runbook/schema.py` | Add `shell_timeout_s: int = 60` to `SafetyConfig` |
| `agent/loop.py` | Add `Patcher` integration: detect `requires_tests`, route writes through worktree, call `run_tests(source_root=worktree_path)` |
| `audit/logger.py` | No changes — write tools reuse the `AuditEntry` LLM fields added in Phase 2 |

---

## Starter Runbooks

Shipped with Phase 3 into `packages/overseer/yeoman_overseer/starter_runbooks/`:

| Runbook | Trigger | Domain | Tools used | `requires_tests` |
|---------|---------|--------|-----------|-----------------|
| `ops-memory-prune.md` | cron weekly | memory | `prune_memory`, `send_alert` | false (no source code touched) |
| `ops-source-cleanup.md` | cron weekly | ops | `shell`, `send_alert` | false (deletes temp files, not source) |

`ops-source-cleanup.md` uses `shell` to delete stale files under `~/.yeoman/var/cache/` and `~/.yeoman/var/media/`. It does not write to source directories.

---

## Tests

| Test file | Coverage |
|-----------|----------|
| `test_tool_write_file.py` | Path allowlist, deny-list (`.git/`, `.env`, `secrets/`, `systemd/`, `runbooks/`), audit log, git commit |
| `test_tool_edit_file.py` | Patch application, deny-list, audit log |
| `test_tool_prune_memory.py` | Snapshot-first enforcement, criteria validation, audit entry |
| `test_tool_run_tests.py` | pytest in sandbox, `source_root` override, structured pass/fail, `/tmp` env vars |
| `test_tool_git_revert.py` | Revert by SHA, audit entry, source repo rejection |
| `test_tool_dry_run_runbook.py` | Validate without executing, issue list output |
| `test_tool_shell.py` | Network blocked, per-call UUID tmpdir, `--die-with-parent`, timeout, cleanup on failure |
| `test_agent_sandbox.py` | Bubblewrap wrapper: mount rules, sensitive path masking (`--tmpfs secrets/`, `/dev/null .env`), `SandboxUnavailableError` |
| `test_agent_patcher.py` | Worktree creation, `translate_path`, apply (merge), discard, lazy creation |
| `test_requires_tests_gate.py` | Gate in `loop.py`: worktree write accumulation, pass→apply, fail→discard+alert |
| `test_sandbox_isolation.py` | Cross-invocation isolation: `shell` tmpdir and `run_tests` tmpdir are distinct UUIDs |

---

## What Phase 3 Does Not Include

- `create_runbook` / `modify_runbook` — Phase 4
- Cryptographic runbook signing (ed25519) — Phase 4
- Owner signature workflow for staging runbooks — Phase 4
- Tombstone system extensions — Phase 4
- Event bus triggers — Phase 4

See `docs/superpowers/specs/future-considerations.md` for deferred architectural items (Workspace Pattern, typed DB APIs, ephemeral DB replica, A2A, container graduation).
