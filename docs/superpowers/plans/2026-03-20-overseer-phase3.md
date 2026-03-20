# Overseer Phase 3: Write Tools + Bubblewrap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the LLM agent from read-only to read-write — the agent can write files, edit code, prune memory, run tests, and execute sandboxed shell commands; source-code mutations are gated behind a CI/CD patch model that only merges when tests pass.

**Architecture:** A `Sandbox` wrapper (bubblewrap) gives each shell/test invocation its own fresh tmpdir with no network access and sensitive paths masked. A `Patcher` manages isolated `git worktree` branches in `/tmp/` so the live source tree is untouched until tests pass. New tools register alongside Phase 2's read-only set. `loop.py` gains a `requires_tests` gate that translates write paths to the worktree and calls `run_tests` at completion.

**Prerequisite:** Phase 2 (`agent/` module, `loop.py`, `ToolContext`, tool registry) must be complete and passing.

**Tech Stack:** Python 3.14, bubblewrap (`bwrap`), git worktrees, sqlite3, pytest, existing yeoman_overseer Phase 1+2 modules

**Spec:** `docs/superpowers/specs/2026-03-20-overseer-phase3-design.md`

---

## File Map

**Create:**
```
packages/overseer/yeoman_overseer/agent/sandbox.py
packages/overseer/yeoman_overseer/agent/patcher.py
packages/overseer/yeoman_overseer/agent/tools/write_file.py
packages/overseer/yeoman_overseer/agent/tools/edit_file.py
packages/overseer/yeoman_overseer/agent/tools/prune_memory.py
packages/overseer/yeoman_overseer/agent/tools/run_tests.py
packages/overseer/yeoman_overseer/agent/tools/git_revert.py
packages/overseer/yeoman_overseer/agent/tools/dry_run_runbook.py
packages/overseer/yeoman_overseer/agent/tools/shell.py
packages/overseer/yeoman_overseer/starter_runbooks/ops-memory-prune.md
packages/overseer/yeoman_overseer/starter_runbooks/ops-source-cleanup.md
tests/overseer/test_schema_phase3.py
tests/overseer/test_agent_sandbox.py
tests/overseer/test_agent_patcher.py
tests/overseer/test_tool_write_file.py
tests/overseer/test_tool_edit_file.py
tests/overseer/test_tool_prune_memory.py
tests/overseer/test_tool_run_tests.py
tests/overseer/test_tool_git_revert.py
tests/overseer/test_tool_dry_run_runbook.py
tests/overseer/test_tool_shell.py
tests/overseer/test_requires_tests_gate.py
tests/overseer/test_sandbox_isolation.py
```

**Modify:**
```
packages/overseer/yeoman_overseer/runbook/schema.py  — add shell_timeout_s to SafetyConfig
packages/overseer/yeoman_overseer/agent/tools/__init__.py  — add ToolContext.sandbox + shell_timeout_s; register 7 new tools
packages/overseer/yeoman_overseer/agent/loop.py  — add requires_tests gate, Patcher integration
```

---

### Task 1: Add `shell_timeout_s` to `SafetyConfig`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/runbook/schema.py`
- Test: `tests/overseer/test_schema_phase3.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_schema_phase3.py
from yeoman_overseer.runbook.schema import SafetyConfig, RunbookFrontmatter, TriggerConfig

def test_safety_config_shell_timeout_default():
    s = SafetyConfig()
    assert s.shell_timeout_s == 60

def test_safety_config_shell_timeout_custom():
    s = SafetyConfig(shell_timeout_s=120)
    assert s.shell_timeout_s == 120

def test_runbook_shell_timeout_parses():
    import yaml
    raw = yaml.safe_load("""
name: ops-cleanup
domain: ops
trigger:
  kind: cron
  expr: "0 2 * * 0"
safety:
  shell_timeout_s: 90
""")
    fm = RunbookFrontmatter(**raw)
    assert fm.safety.shell_timeout_s == 90
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_schema_phase3.py -v
```
Expected: `FAILED — AttributeError: 'SafetyConfig' object has no attribute 'shell_timeout_s'`

- [ ] **Add field to SafetyConfig**

In `packages/overseer/yeoman_overseer/runbook/schema.py`, add to `SafetyConfig`:

```python
class SafetyConfig(BaseModel):
    max_actions_per_hour: int = 10
    rollback: bool = True
    cooldown_s: int = 300
    requires_tests: bool = False
    on_lock_conflict: Literal["queue", "skip"] = "skip"
    shell_timeout_s: int = 60   # ← add this line
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_schema_phase3.py -v
```
Expected: 3 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/runbook/schema.py tests/overseer/test_schema_phase3.py
git commit -m "feat(overseer): add shell_timeout_s to SafetyConfig"
```

---

### Task 2: Bubblewrap sandbox wrapper (`sandbox.py`)

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/sandbox.py`
- Test: `tests/overseer/test_agent_sandbox.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_agent_sandbox.py
from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from yeoman_overseer.agent.sandbox import Sandbox, SandboxUnavailableError


def test_raises_when_bwrap_not_found():
    with patch("shutil.which", return_value=None):
        Sandbox._bwrap = None
        with pytest.raises(SandboxUnavailableError, match="bwrap not found"):
            Sandbox().run(["echo", "hi"])


def test_run_returns_structured_result(tmp_path):
    mock_result = MagicMock()
    mock_result.stdout = "hello\n"
    mock_result.stderr = ""
    mock_result.returncode = 0

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result) as mock_run:
        Sandbox._bwrap = None
        result = Sandbox().run(["echo", "hello"])

    assert result == {"stdout": "hello\n", "stderr": "", "exit_code": 0}


def test_bwrap_args_include_required_mounts(tmp_path):
    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return mock_result

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", side_effect=fake_run):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    joined = " ".join(captured_args)
    assert "--unshare-net" in joined
    assert "--unshare-pid" in joined
    assert "--die-with-parent" in joined
    assert "--ro-bind" in joined
    assert "--tmpfs" in joined  # secrets/ masking


def test_sensitive_path_masking_in_args():
    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(str(a) for a in args)
        return mock_result

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", side_effect=fake_run):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    joined = " ".join(captured_args)
    assert "secrets" in joined    # --tmpfs over secrets/
    assert ".env" in joined       # --ro-bind /dev/null over .env


def test_tmpdir_cleaned_up_on_success(tmp_path):
    """After a successful run, the per-call tmpdir must not exist."""
    created_dirs: list[Path] = []

    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created_dirs.append(self)
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)
    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    # All created tmpdirs should be gone
    for d in created_dirs:
        assert not d.exists()
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_agent_sandbox.py -v
```
Expected: `FAILED — ModuleNotFoundError: No module named 'yeoman_overseer.agent.sandbox'`

- [ ] **Implement `sandbox.py`**

```python
# packages/overseer/yeoman_overseer/agent/sandbox.py
"""Bubblewrap sandbox wrapper — per-call UUID tmpdir, no network, sensitive paths masked."""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    """Raised when bwrap is not available on PATH."""


class Sandbox:
    _bwrap: str | None = None

    @classmethod
    def _find_bwrap(cls) -> str:
        if cls._bwrap is None:
            found = shutil.which("bwrap")
            if not found:
                raise SandboxUnavailableError("bwrap not found on PATH")
            cls._bwrap = found
        return cls._bwrap

    def run(
        self,
        cmd: list[str],
        *,
        timeout: int = 60,
        source_root: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Run cmd inside bubblewrap. Returns {stdout, stderr, exit_code}."""
        bwrap = self._find_bwrap()

        yeoman_home = Path.home() / ".yeoman"
        source_dir = source_root or (Path.home() / "Documents" / "yeoman")

        tmpdir = Path(f"/tmp/overseer-{uuid.uuid4().hex}")
        tmpdir.mkdir(mode=0o700)

        bwrap_args: list[str] = [
            bwrap,
            # System read-only
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache",
            # Application read-only
            "--ro-bind", str(yeoman_home), str(yeoman_home),
            "--ro-bind", str(source_dir), str(source_dir),
            # Sensitive path masking
            "--tmpfs", str(yeoman_home / "secrets"),
            "--ro-bind", "/dev/null", str(yeoman_home / ".env"),
            # Ephemeral writable tmp — unique per call
            "--bind", str(tmpdir), "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-net",
            "--unshare-pid",
            "--die-with-parent",
            "--",
            *cmd,
        ]

        kwargs: dict = dict(capture_output=True, text=True, timeout=timeout)
        if env is not None:
            import os
            kwargs["env"] = {**os.environ, **env}

        try:
            result = subprocess.run(bwrap_args, **kwargs)
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_agent_sandbox.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/sandbox.py tests/overseer/test_agent_sandbox.py
git commit -m "feat(overseer): add bubblewrap sandbox wrapper"
```

---

### Task 3: CI/CD Patcher (`patcher.py`)

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/patcher.py`
- Test: `tests/overseer/test_agent_patcher.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_agent_patcher.py
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest
from yeoman_overseer.agent.patcher import Patcher, PatchContext


def _init_git_repo(path: Path) -> None:
    """Create a real git repo with a root commit so worktrees work."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_create_worktree(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="abc123")
    try:
        assert ctx.worktree_path.is_dir()
        assert ctx.branch == "overseer-patch-abc123"
        assert ctx.live_repo == live_repo
        # Verify it's a real git worktree
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=live_repo, capture_output=True, text=True
        )
        assert "abc123" in result.stdout
    finally:
        patcher.discard(ctx)


def test_translate_path(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="xyz")
    try:
        original = live_repo / "src" / "foo.py"
        translated = patcher.translate_path(ctx, original)
        assert translated == ctx.worktree_path / "src" / "foo.py"
    finally:
        patcher.discard(ctx)


def test_apply_merges_into_live_repo(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="test-apply")
    try:
        # Write a file in the worktree
        new_file = ctx.worktree_path / "hello.txt"
        new_file.write_text("world")
        patcher.apply(ctx)
        # File should now exist in live repo
        assert (live_repo / "hello.txt").exists()
        # Worktree should be gone
        assert not ctx.worktree_path.exists()
    except Exception:
        patcher.discard(ctx)
        raise


def test_discard_removes_worktree(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="test-discard")
    patcher.discard(ctx)
    assert not ctx.worktree_path.exists()
    # Branch should be gone
    result = subprocess.run(
        ["git", "branch"],
        cwd=live_repo, capture_output=True, text=True
    )
    assert "overseer-patch-test-discard" not in result.stdout


def test_apply_raises_if_tests_leave_worktree_dirty(tmp_path):
    """apply() handles repos with no changes gracefully (--allow-empty commit)."""
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="empty")
    try:
        # Apply with no changes — should not raise
        patcher.apply(ctx)
        assert not ctx.worktree_path.exists()
    except Exception:
        patcher.discard(ctx)
        raise
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_agent_patcher.py -v
```
Expected: `FAILED — ModuleNotFoundError: No module named 'yeoman_overseer.agent.patcher'`

- [ ] **Implement `patcher.py`**

```python
# packages/overseer/yeoman_overseer/agent/patcher.py
"""CI/CD patch model — isolated git worktrees in /tmp/, merge only on test pass."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PatchContext:
    worktree_path: Path
    branch: str
    live_repo: Path


class Patcher:
    """Manages git worktrees for isolated source code mutation."""

    def create_worktree(self, live_repo: Path, run_id: str) -> PatchContext:
        wt_path = Path(f"/tmp/overseer-wt-{run_id}")
        branch = f"overseer-patch-{run_id}"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", branch],
            cwd=live_repo,
            check=True,
            capture_output=True,
        )
        return PatchContext(worktree_path=wt_path, branch=branch, live_repo=live_repo)

    def translate_path(self, ctx: PatchContext, original: Path) -> Path:
        """Map a live-repo path to its equivalent in the worktree."""
        rel = original.resolve().relative_to(ctx.live_repo.resolve())
        return ctx.worktree_path / rel

    def apply(self, ctx: PatchContext) -> None:
        """Commit worktree changes, merge into live repo, remove worktree."""
        # Stage and commit everything in the worktree
        subprocess.run(
            ["git", "add", "-A"],
            cwd=ctx.worktree_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"overseer: patch {ctx.branch}"],
            cwd=ctx.worktree_path,
            check=True,
            capture_output=True,
        )
        # Merge worktree branch into live repo
        subprocess.run(
            ["git", "merge", ctx.branch, "--no-ff", "-m", f"overseer: merge {ctx.branch}"],
            cwd=ctx.live_repo,
            check=True,
            capture_output=True,
        )
        self._cleanup(ctx)

    def discard(self, ctx: PatchContext) -> None:
        """Remove worktree and delete branch without merging."""
        self._cleanup(ctx)

    def _cleanup(self, ctx: PatchContext) -> None:
        subprocess.run(
            ["git", "worktree", "remove", str(ctx.worktree_path), "--force"],
            cwd=ctx.live_repo,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", ctx.branch],
            cwd=ctx.live_repo,
            capture_output=True,
        )
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_agent_patcher.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/patcher.py tests/overseer/test_agent_patcher.py
git commit -m "feat(overseer): add CI/CD patcher with git worktree support"
```

---

### Task 4: `write_file` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/write_file.py`
- Test: `tests/overseer/test_tool_write_file.py`

**Dependency:** Phase 2's `agent/tools/__init__.py` must exist with `ToolContext` defined. `ToolContext` must expose `yeoman_home: Path`, `source_dir: Path`, `data_dir: Path`, `git: InternalGit`, `audit: AuditLogger`, `runbook_name: str`, `domain: str`.

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_write_file.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from yeoman_overseer.agent.tools.write_file import write_file, _is_allowed


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.yeoman_home = tmp_path / ".yeoman"
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / ".yeoman" / "data"
    ctx.runbook_name = "test-runbook"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    ctx.yeoman_home.mkdir(parents=True)
    ctx.source_dir.mkdir(parents=True)
    return ctx


def test_write_to_allowed_path(tmp_path):
    ctx = _ctx(tmp_path)
    target = str(ctx.yeoman_home / "config.json")
    result = write_file(target, '{"key": "value"}', ctx)
    assert result["ok"] is True
    assert Path(target).read_text() == '{"key": "value"}'


def test_audit_logged_on_write(tmp_path):
    ctx = _ctx(tmp_path)
    target = str(ctx.yeoman_home / "notes.txt")
    write_file(target, "hello", ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "write_file"
    assert entry.target == target


def test_deny_list_dot_env(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / ".env"), "SECRET=x", ctx)
    assert result["ok"] is False
    assert "denied" in result["error"]


def test_deny_list_secrets_dir(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "secrets" / "key.pem"), "data", ctx)
    assert result["ok"] is False


def test_deny_list_dot_git(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.source_dir / ".git" / "hooks" / "pre-commit"), "evil", ctx)
    assert result["ok"] is False


def test_deny_list_runbooks(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "runbooks" / "new.md"), "---\nname: x", ctx)
    assert result["ok"] is False


def test_deny_list_systemd(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "systemd" / "unit.service"), "[Unit]", ctx)
    assert result["ok"] is False


def test_path_outside_roots_denied(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file("/etc/passwd", "root:x:0:0", ctx)
    assert result["ok"] is False


def test_symlink_traversal_blocked(tmp_path):
    ctx = _ctx(tmp_path)
    # Symlink pointing outside allowed root
    link = ctx.yeoman_home / "escape"
    link.symlink_to("/etc")
    result = write_file(str(link / "passwd"), "evil", ctx)
    assert result["ok"] is False


def test_is_allowed_helper(tmp_path):
    ctx = _ctx(tmp_path)
    assert _is_allowed(ctx.yeoman_home / "config.json", ctx) is True
    assert _is_allowed(ctx.yeoman_home / ".env", ctx) is False
    assert _is_allowed(Path("/tmp/outside"), ctx) is False
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_write_file.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `write_file.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/write_file.py
"""Audited file write — path allowlist + deny-list, auto-committed."""
from __future__ import annotations

from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry

# Parts that must not appear anywhere in the resolved path
_DENY_PARTS: frozenset[str] = frozenset({".env", "secrets", ".git", "systemd", "runbooks"})


def _is_allowed(path: Path, ctx: object) -> bool:
    """Return True iff path resolves within an allowed root and passes the deny-list."""
    try:
        resolved = path.resolve()
    except OSError:
        return False

    roots = [ctx.yeoman_home.resolve(), ctx.source_dir.resolve()]
    in_root = any(resolved == r or resolved.is_relative_to(r) for r in roots)
    if not in_root:
        return False

    for part in resolved.parts:
        if part in _DENY_PARTS:
            return False
    return True


def write_file(path: str, content: str, ctx: object) -> dict:
    """Write content to path. Returns {ok, path} or {ok: False, error}."""
    target = Path(path)

    if not _is_allowed(target, ctx):
        return {"ok": False, "error": f"path denied: {path}"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="write_file",
        target=str(target),
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    # Commit to internal git if the path is within data_dir
    if ctx.git is not None:
        try:
            rel = str(target.resolve().relative_to(ctx.data_dir.resolve()))
            ctx.git.commit(files=[rel], message=f"overseer: write {target.name}")
        except ValueError:
            pass  # Not in data_dir — Patcher or no-op handles commit

    return {"ok": True, "path": str(target)}
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_write_file.py -v
```
Expected: 10 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/write_file.py tests/overseer/test_tool_write_file.py
git commit -m "feat(overseer): add write_file tool with path allowlist and deny-list"
```

---

### Task 5: `edit_file` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/edit_file.py`
- Test: `tests/overseer/test_tool_edit_file.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_edit_file.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.edit_file import edit_file


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.yeoman_home = tmp_path / ".yeoman"
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / ".yeoman" / "data"
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    ctx.yeoman_home.mkdir(parents=True)
    ctx.source_dir.mkdir(parents=True)
    return ctx


def test_edit_replaces_old_with_new(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("hello world\nfoo bar\n")
    result = edit_file(str(target), "hello world", "hello everyone", ctx)
    assert result["ok"] is True
    assert target.read_text() == "hello everyone\nfoo bar\n"


def test_edit_fails_if_old_not_found(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("something else\n")
    result = edit_file(str(target), "hello world", "replacement", ctx)
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_edit_fails_on_nonexistent_file(tmp_path):
    ctx = _ctx(tmp_path)
    result = edit_file(str(ctx.yeoman_home / "missing.txt"), "old", "new", ctx)
    assert result["ok"] is False


def test_edit_deny_list_env(tmp_path):
    ctx = _ctx(tmp_path)
    result = edit_file(str(ctx.yeoman_home / ".env"), "old", "new", ctx)
    assert result["ok"] is False
    assert "denied" in result["error"]


def test_audit_logged_on_edit(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("old content")
    edit_file(str(target), "old content", "new content", ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "edit_file"
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_edit_file.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `edit_file.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/edit_file.py
"""Audited file edit — exact string replacement with path restrictions."""
from __future__ import annotations

from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry
from yeoman_overseer.agent.tools.write_file import _is_allowed


def edit_file(path: str, old_string: str, new_string: str, ctx: object) -> dict:
    """Replace old_string with new_string in path. Returns {ok, path} or {ok: False, error}."""
    target = Path(path)

    if not _is_allowed(target, ctx):
        return {"ok": False, "error": f"path denied: {path}"}

    if not target.exists():
        return {"ok": False, "error": f"file not found: {path}"}

    content = target.read_text(encoding="utf-8")
    if old_string not in content:
        return {"ok": False, "error": f"old_string not found in {path}"}

    new_content = content.replace(old_string, new_string, 1)
    target.write_text(new_content, encoding="utf-8")

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="edit_file",
        target=str(target),
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    if ctx.git is not None:
        try:
            rel = str(target.resolve().relative_to(ctx.data_dir.resolve()))
            ctx.git.commit(files=[rel], message=f"overseer: edit {target.name}")
        except ValueError:
            pass

    return {"ok": True, "path": str(target)}
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_edit_file.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/edit_file.py tests/overseer/test_tool_edit_file.py
git commit -m "feat(overseer): add edit_file tool"
```

---

### Task 6: `prune_memory` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/prune_memory.py`
- Test: `tests/overseer/test_tool_prune_memory.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_prune_memory.py
from __future__ import annotations
import sqlite3
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.prune_memory import prune_memory


def _ctx(tmp_path: Path, db_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.memory_db = db_path
    ctx.runbook_name = "memory-prune"
    ctx.domain = "memory"
    ctx.audit = MagicMock()
    return ctx


def _make_db(path: Path) -> None:
    """Create a minimal memory.db with some nodes."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory2_nodes "
        "(id INTEGER PRIMARY KEY, content TEXT, salience REAL, created_at REAL, domain TEXT)"
    )
    import time
    now = time.time()
    # Old low-salience node
    conn.execute("INSERT INTO memory2_nodes VALUES (1, 'old low', 0.1, ?, 'general')", (now - 40 * 86400,))
    # Recent high-salience node
    conn.execute("INSERT INTO memory2_nodes VALUES (2, 'recent high', 0.9, ?, 'general')", (now,))
    conn.commit()
    conn.close()


def test_snapshot_created_before_deletion(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)

    result = prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    assert result["ok"] is True
    # Snapshot should exist
    snapshots = list(tmp_path.glob("memory.db.snapshot-*"))
    assert len(snapshots) == 1


def test_deletes_old_low_salience_rows(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)

    result = prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    assert result["rows_deleted"] == 1

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id FROM memory2_nodes").fetchall()
    conn.close()
    assert rows == [(2,)]  # Only the recent high-salience row remains


def test_domain_filter(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory2_nodes "
        "(id INTEGER PRIMARY KEY, content TEXT, salience REAL, created_at REAL, domain TEXT)"
    )
    import time
    now = time.time()
    conn.execute("INSERT INTO memory2_nodes VALUES (1, 'old', 0.1, ?, 'health')", (now - 40 * 86400,))
    conn.execute("INSERT INTO memory2_nodes VALUES (2, 'old', 0.1, ?, 'memory')", (now - 40 * 86400,))
    conn.commit()
    conn.close()
    ctx = _ctx(tmp_path, db)

    result = prune_memory(age_days=30, salience_below=0.5, domain="health", ctx=ctx)
    assert result["rows_deleted"] == 1
    conn = sqlite3.connect(db)
    remaining = conn.execute("SELECT id FROM memory2_nodes").fetchall()
    conn.close()
    assert (2,) in remaining  # memory domain row preserved


def test_audit_logged(tmp_path):
    db = tmp_path / "memory.db"
    _make_db(db)
    ctx = _ctx(tmp_path, db)

    prune_memory(age_days=30, salience_below=0.5, ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "prune_memory"
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_prune_memory.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `prune_memory.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/prune_memory.py
"""Prune memory.db entries by age/salience — snapshot-first."""
from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from yeoman_overseer.audit.logger import AuditEntry


def prune_memory(
    *,
    age_days: int | None = None,
    salience_below: float | None = None,
    domain: str | None = None,
    ctx: object,
) -> dict:
    """Delete memory nodes matching criteria after snapshotting the DB first."""
    db_path: Path = ctx.memory_db

    # Snapshot before any mutation
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot = db_path.with_name(f"{db_path.name}.snapshot-{ts}")
    shutil.copy2(db_path, snapshot)

    cutoff_ts = time.time() - (age_days * 86400) if age_days is not None else None

    clauses: list[str] = []
    params: list = []

    if cutoff_ts is not None:
        clauses.append("created_at < ?")
        params.append(cutoff_ts)
    if salience_below is not None:
        clauses.append("salience < ?")
        params.append(salience_below)
    if domain is not None:
        clauses.append("domain = ?")
        params.append(domain)

    if not clauses:
        return {"ok": False, "error": "no criteria provided"}

    where = " AND ".join(clauses)
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(f"DELETE FROM memory2_nodes WHERE {where}", params)
        rows_deleted = cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="prune_memory",
        target=str(db_path),
        result=f"deleted {rows_deleted} rows",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    return {"ok": True, "rows_deleted": rows_deleted, "snapshot": str(snapshot)}
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_prune_memory.py -v
```
Expected: 4 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/prune_memory.py tests/overseer/test_tool_prune_memory.py
git commit -m "feat(overseer): add prune_memory tool with snapshot-first guarantee"
```

---

### Task 7: `run_tests` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/run_tests.py`
- Test: `tests/overseer/test_tool_run_tests.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_run_tests.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.run_tests import run_tests


def _ctx(source_dir: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.source_dir = source_dir
    ctx.sandbox = MagicMock()
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.audit = MagicMock()
    return ctx


def test_passes_with_zero_failures(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "5 passed in 0.12s",
        "stderr": "",
        "exit_code": 0,
    }
    result = run_tests(ctx=ctx)
    assert result["passed"] is True
    assert result["exit_code"] == 0


def test_fails_with_nonzero_exit(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "2 failed, 3 passed",
        "stderr": "",
        "exit_code": 1,
    }
    result = run_tests(ctx=ctx)
    assert result["passed"] is False


def test_source_root_override(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {"stdout": "1 passed", "stderr": "", "exit_code": 0}
    worktree = tmp_path / "worktree"
    run_tests(source_root=worktree, ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args
    # source_root should be forwarded to sandbox
    assert call_kwargs[1].get("source_root") == worktree or worktree in call_kwargs[0]


def test_pytest_env_vars_set(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {"stdout": "1 passed", "stderr": "", "exit_code": 0}
    run_tests(ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args
    # env should include PYTEST_CACHE_DIR
    env = call_kwargs[1].get("env", {})
    assert "PYTEST_CACHE_DIR" in env


def test_output_included_in_result(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "collected 3 items\n3 passed",
        "stderr": "some warning",
        "exit_code": 0,
    }
    result = run_tests(ctx=ctx)
    assert "3 passed" in result["output"]
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_run_tests.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `run_tests.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/run_tests.py
"""Run pytest inside the bubblewrap sandbox."""
from __future__ import annotations

from pathlib import Path


def run_tests(*, source_root: Path | None = None, ctx: object) -> dict:
    """Execute pytest in sandbox. Returns {passed, exit_code, output}."""
    root = source_root or ctx.source_dir

    cmd = [
        "python", "-m", "pytest",
        "--tb=short",
        "--basetemp=/tmp/pytest-tmp",
        "-q",
    ]

    sandbox_result = ctx.sandbox.run(
        cmd,
        source_root=root,
        env={
            "PYTEST_CACHE_DIR": "/tmp/pytest-cache",
            "PYTHONPATH": str(root),
        },
    )

    output = sandbox_result["stdout"] + sandbox_result["stderr"]
    return {
        "passed": sandbox_result["exit_code"] == 0,
        "exit_code": sandbox_result["exit_code"],
        "output": output,
    }
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_run_tests.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/run_tests.py tests/overseer/test_tool_run_tests.py
git commit -m "feat(overseer): add run_tests tool with sandbox and source_root support"
```

---

### Task 8: `git_revert` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/git_revert.py`
- Test: `tests/overseer/test_tool_git_revert.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_git_revert.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.git_revert import git_revert


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / "data"
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    return ctx


def test_revert_calls_internal_git(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.git.revert.return_value = None
    result = git_revert("abc123", ctx=ctx)
    assert result["ok"] is True
    ctx.git.revert.assert_called_once_with("abc123")


def test_revert_audit_logged(tmp_path):
    ctx = _ctx(tmp_path)
    result = git_revert("abc123", ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "git_revert"
    assert "abc123" in entry.target


def test_revert_propagates_git_error(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.git.revert.side_effect = Exception("nothing to revert")
    result = git_revert("deadbeef", ctx=ctx)
    assert result["ok"] is False
    assert "nothing to revert" in result["error"]


def test_sha_validation_rejects_non_hex(tmp_path):
    ctx = _ctx(tmp_path)
    result = git_revert("not_a_sha!", ctx=ctx)
    assert result["ok"] is False
    assert "invalid sha" in result["error"].lower()
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_git_revert.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `git_revert.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/git_revert.py
"""Revert an internal overseer git commit by SHA."""
from __future__ import annotations

import re

from yeoman_overseer.audit.logger import AuditEntry

_SHA_RE = re.compile(r"^[0-9a-f]{6,40}$")


def git_revert(sha: str, *, ctx: object) -> dict:
    """Revert a single commit in the internal overseer git by SHA."""
    if not _SHA_RE.match(sha):
        return {"ok": False, "error": f"invalid sha: {sha!r}"}

    try:
        ctx.git.revert(sha)
    except Exception as exc:
        ctx.audit.append(AuditEntry(
            runbook=ctx.runbook_name,
            trigger="llm",
            action="git_revert",
            target=sha,
            result=f"error: {exc}",
            duration_ms=0,
            escalated_to_llm=True,
            domain=ctx.domain,
        ))
        return {"ok": False, "error": str(exc)}

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="git_revert",
        target=sha,
        result="success",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))
    return {"ok": True, "reverted": sha}
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_git_revert.py -v
```
Expected: 4 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/git_revert.py tests/overseer/test_tool_git_revert.py
git commit -m "feat(overseer): add git_revert tool"
```

---

### Task 9: `dry_run_runbook` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/dry_run_runbook.py`
- Test: `tests/overseer/test_tool_dry_run_runbook.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_dry_run_runbook.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.dry_run_runbook import dry_run_runbook

_VALID_RUNBOOK = """\
---
name: health-check
domain: health
trigger:
  kind: cron
  expr: "0 * * * *"
escalate_to_llm: false
---
## Actions
- action: noop
  target: system
"""

_INVALID_FRONTMATTER = """\
---
domain: health
# missing required 'name' field
trigger:
  kind: cron
---
"""

_UNKNOWN_TRIGGER = """\
---
name: bad-trigger
domain: health
trigger:
  kind: webhook
---
"""


def _ctx() -> MagicMock:
    ctx = MagicMock()
    return ctx


def test_valid_runbook_returns_valid_true(tmp_path):
    rb_path = tmp_path / "health-check.md"
    rb_path.write_text(_VALID_RUNBOOK)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is True
    assert result["issues"] == []


def test_invalid_frontmatter_reports_issues(tmp_path):
    rb_path = tmp_path / "bad.md"
    rb_path.write_text(_INVALID_FRONTMATTER)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is False
    assert len(result["issues"]) > 0


def test_missing_file_reports_issue(tmp_path):
    result = dry_run_runbook(str(tmp_path / "missing.md"), ctx=_ctx())
    assert result["valid"] is False
    assert any("not found" in i.lower() for i in result["issues"])


def test_action_plan_extracted(tmp_path):
    rb_path = tmp_path / "rb.md"
    rb_path.write_text(_VALID_RUNBOOK)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert isinstance(result["action_plan"], list)


def test_unknown_trigger_kind_reports_issue(tmp_path):
    rb_path = tmp_path / "webhook.md"
    rb_path.write_text(_UNKNOWN_TRIGGER)
    result = dry_run_runbook(str(rb_path), ctx=_ctx())
    assert result["valid"] is False
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_dry_run_runbook.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `dry_run_runbook.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/dry_run_runbook.py
"""Validate a runbook without executing it."""
from __future__ import annotations

import re
from pathlib import Path

from yeoman_overseer.runbook.parser import parse_runbook

_KNOWN_TRIGGER_KINDS = {"poll", "cron", "event"}
_ACTION_RE = re.compile(r"^[-*]\s+action:\s+(\w+)", re.MULTILINE)


def dry_run_runbook(path: str, *, ctx: object) -> dict:
    """Parse and validate a runbook file without executing any actions."""
    rb_path = Path(path)
    issues: list[str] = []

    if not rb_path.exists():
        return {
            "valid": False,
            "trigger_would_fire": False,
            "action_plan": [],
            "issues": [f"file not found: {path}"],
        }

    try:
        runbook = parse_runbook(rb_path)
    except Exception as exc:
        return {
            "valid": False,
            "trigger_would_fire": False,
            "action_plan": [],
            "issues": [f"parse error: {exc}"],
        }

    if runbook.meta.trigger.kind not in _KNOWN_TRIGGER_KINDS:
        issues.append(f"unknown trigger kind: {runbook.meta.trigger.kind!r}")

    # Extract action plan from the runbook body
    action_plan = _ACTION_RE.findall(runbook.body or "")

    return {
        "valid": len(issues) == 0,
        "trigger_would_fire": False,  # Evaluation requires live system state
        "action_plan": action_plan,
        "issues": issues,
    }
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_dry_run_runbook.py -v
```
Expected: 5 PASSED

- [ ] **Check `parse_runbook` function signature**

```bash
cd ~/Documents/yeoman && grep -n "def parse_runbook" packages/overseer/yeoman_overseer/runbook/parser.py
```

If `parse_runbook` takes a `Path` and returns an object with `.meta` and `.body`, the implementation above is correct. If the API differs, adjust accordingly.

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/dry_run_runbook.py tests/overseer/test_tool_dry_run_runbook.py
git commit -m "feat(overseer): add dry_run_runbook validation tool"
```

---

### Task 10: `shell` tool

**Files:**
- Create: `packages/overseer/yeoman_overseer/agent/tools/shell.py`
- Test: `tests/overseer/test_tool_shell.py`

- [ ] **Write failing test**

```python
# tests/overseer/test_tool_shell.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.shell import shell


def _ctx(timeout: int = 60) -> MagicMock:
    ctx = MagicMock()
    ctx.shell_timeout_s = timeout
    ctx.sandbox = MagicMock()
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.audit = MagicMock()
    return ctx


def test_shell_returns_structured_result():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {
        "stdout": "hello\n",
        "stderr": "",
        "exit_code": 0,
    }
    result = shell("echo hello", ctx=ctx)
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0


def test_shell_passes_timeout_from_context():
    ctx = _ctx(timeout=30)
    ctx.sandbox.run.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
    shell("true", ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args[1]
    assert call_kwargs.get("timeout") == 30


def test_shell_audit_logged():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {"stdout": "done", "stderr": "", "exit_code": 0}
    shell("ls /tmp", ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "shell"


def test_shell_command_split_correctly():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
    shell("echo foo bar", ctx=ctx)
    cmd = ctx.sandbox.run.call_args[0][0]
    assert cmd == ["echo", "foo", "bar"]


def test_shell_propagates_sandbox_exception():
    ctx = _ctx()
    ctx.sandbox.run.side_effect = TimeoutError("timed out")
    result = shell("sleep 999", ctx=ctx)
    assert result["exit_code"] == -1
    assert "timed out" in result["stderr"]
```

- [ ] **Run to verify failure**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_shell.py -v
```
Expected: `FAILED — ModuleNotFoundError`

- [ ] **Implement `shell.py`**

```python
# packages/overseer/yeoman_overseer/agent/tools/shell.py
"""Execute a shell command inside the bubblewrap sandbox."""
from __future__ import annotations

import shlex

from yeoman_overseer.audit.logger import AuditEntry


def shell(command: str, *, ctx: object) -> dict:
    """Run command string in sandbox. Returns {stdout, stderr, exit_code}."""
    cmd = shlex.split(command)

    try:
        result = ctx.sandbox.run(cmd, timeout=ctx.shell_timeout_s)
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1}

    ctx.audit.append(AuditEntry(
        runbook=ctx.runbook_name,
        trigger="llm",
        action="shell",
        target=command[:200],
        result=f"exit={result['exit_code']}",
        duration_ms=0,
        escalated_to_llm=True,
        domain=ctx.domain,
    ))

    return result
```

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_tool_shell.py -v
```
Expected: 5 PASSED

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/shell.py tests/overseer/test_tool_shell.py
git commit -m "feat(overseer): add shell tool with bubblewrap and per-runbook timeout"
```

---

### Task 11: Register new tools + extend `ToolContext`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/agent/tools/__init__.py`

**Note:** Phase 2 created this file. This task adds Phase 3 fields to `ToolContext` and registers the 7 new tools.

- [ ] **Read the current file**

```bash
cat packages/overseer/yeoman_overseer/agent/tools/__init__.py
```

- [ ] **Add `sandbox` and `shell_timeout_s` to ToolContext**

Add to the `ToolContext` dataclass:
```python
from yeoman_overseer.agent.sandbox import Sandbox

@dataclass
class ToolContext:
    # ... existing Phase 2 fields ...
    sandbox: Sandbox = field(default_factory=Sandbox)
    shell_timeout_s: int = 60
    memory_db: Path | None = None   # used by prune_memory
```

- [ ] **Register the 7 new tools in TOOL_DEFINITIONS**

Add entries for `write_file`, `edit_file`, `prune_memory`, `run_tests`, `git_revert`, `dry_run_runbook`, `shell` following the same structure as the Phase 2 tools (name, description, input_schema, handler callable).

- [ ] **Run full overseer test suite to verify no regressions**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/ -v
```
Expected: all existing tests still PASS

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/tools/__init__.py
git commit -m "feat(overseer): register Phase 3 tools in tool registry; extend ToolContext"
```

---

### Task 12: `requires_tests` gate in `loop.py`

**Files:**
- Modify: `packages/overseer/yeoman_overseer/agent/loop.py`
- Test: `tests/overseer/test_requires_tests_gate.py`
- Test: `tests/overseer/test_sandbox_isolation.py`

**Note:** Phase 2 created `loop.py`. This task adds the `requires_tests` gate and Patcher integration.

- [ ] **Write failing test for the gate**

```python
# tests/overseer/test_requires_tests_gate.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest
from yeoman_overseer.agent.patcher import Patcher, PatchContext
from yeoman_overseer.runbook.schema import SafetyConfig


# ---- Helpers ----

def _make_runbook(requires_tests: bool = True) -> MagicMock:
    rb = MagicMock()
    rb.meta.safety.requires_tests = requires_tests
    rb.meta.safety.shell_timeout_s = 60
    rb.meta.name = "test-runbook"
    rb.meta.domain = "ops"
    return rb


def _make_patch_ctx(worktree: Path, live_repo: Path) -> PatchContext:
    return PatchContext(
        worktree_path=worktree,
        branch="overseer-patch-run1",
        live_repo=live_repo,
    )


# ---- Tests ----

def test_no_gate_when_requires_tests_false(tmp_path):
    """When requires_tests=False, write_file goes directly to live path."""
    from yeoman_overseer.agent.loop import _route_write_path

    runbook = _make_runbook(requires_tests=False)
    original = tmp_path / "yeoman" / "src" / "foo.py"
    source_dir = tmp_path / "yeoman"

    result = _route_write_path(original, source_dir, patch_ctx=None, requires_tests=False)
    assert result == original


def test_gate_translates_path_when_requires_tests_true(tmp_path):
    """When requires_tests=True and a PatchContext exists, path is translated."""
    from yeoman_overseer.agent.loop import _route_write_path

    live_repo = tmp_path / "yeoman"
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)

    ctx = _make_patch_ctx(worktree, live_repo)
    original = live_repo / "src" / "foo.py"
    live_repo.mkdir(parents=True)
    (live_repo / "src").mkdir(parents=True)

    translated = _route_write_path(original, live_repo, patch_ctx=ctx, requires_tests=True)
    assert translated == worktree / "src" / "foo.py"


def test_gate_passes_on_test_success(tmp_path):
    """On test pass, Patcher.apply() is called and AgentResult marks patch as applied."""
    from yeoman_overseer.agent.loop import _finalize_patch

    patcher = MagicMock(spec=Patcher)
    run_tests_result = {"passed": True, "exit_code": 0, "output": "2 passed"}
    run_tests_fn = MagicMock(return_value=run_tests_result)
    ctx = MagicMock()
    ctx.sandbox = MagicMock()
    worktree = tmp_path / "wt"
    live_repo = tmp_path / "repo"
    patch_ctx = _make_patch_ctx(worktree, live_repo)

    result = _finalize_patch(patch_ctx, patcher, run_tests_fn, ctx)

    patcher.apply.assert_called_once_with(patch_ctx)
    patcher.discard.assert_not_called()
    assert result["patch_applied"] is True


def test_gate_discards_on_test_failure(tmp_path):
    """On test fail, Patcher.discard() is called and patch_applied is False."""
    from yeoman_overseer.agent.loop import _finalize_patch

    patcher = MagicMock(spec=Patcher)
    run_tests_result = {"passed": False, "exit_code": 1, "output": "1 failed"}
    run_tests_fn = MagicMock(return_value=run_tests_result)
    ctx = MagicMock()
    worktree = tmp_path / "wt"
    live_repo = tmp_path / "repo"
    patch_ctx = _make_patch_ctx(worktree, live_repo)

    result = _finalize_patch(patch_ctx, patcher, run_tests_fn, ctx)

    patcher.discard.assert_called_once_with(patch_ctx)
    patcher.apply.assert_not_called()
    assert result["patch_applied"] is False
```

- [ ] **Write failing test for sandbox isolation**

```python
# tests/overseer/test_sandbox_isolation.py
from __future__ import annotations
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from yeoman_overseer.agent.sandbox import Sandbox


def test_each_sandbox_run_gets_unique_tmpdir():
    """Two Sandbox.run() calls must use different /tmp/overseer-{uuid} directories."""
    created: list[str] = []
    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created.append(str(self))
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        sb = Sandbox()
        sb.run(["echo", "a"])
        sb.run(["echo", "b"])

    assert len(created) == 2
    assert created[0] != created[1], "Two sandbox calls must use distinct tmpdirs"


def test_tmpdir_names_contain_hex_uuid():
    """Tmpdir must be /tmp/overseer-{32-hex-char-uuid}."""
    created: list[str] = []
    original_mkdir = Path.mkdir

    def tracking_mkdir(self, *args, **kwargs):
        if "overseer-" in str(self):
            created.append(Path(self).name)
        original_mkdir(self, *args, **kwargs)

    mock_result = MagicMock(stdout="", stderr="", returncode=0)

    with patch("shutil.which", return_value="/usr/bin/bwrap"), \
         patch("subprocess.run", return_value=mock_result), \
         patch.object(Path, "mkdir", tracking_mkdir):
        Sandbox._bwrap = None
        Sandbox().run(["true"])

    assert len(created) == 1
    name = created[0]
    prefix, _, hex_part = name.partition("-")
    assert prefix == "overseer"
    assert len(hex_part) == 32
    int(hex_part, 16)  # Must be valid hex
```

- [ ] **Run to verify failures**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_requires_tests_gate.py tests/overseer/test_sandbox_isolation.py -v
```
Expected: both fail — `_route_write_path` and `_finalize_patch` don't exist yet

- [ ] **Add gate helpers to `loop.py`**

In `packages/overseer/yeoman_overseer/agent/loop.py`, add these two functions and integrate the gate into the agent run method:

```python
# Add at module level
from yeoman_overseer.agent.patcher import Patcher, PatchContext

def _route_write_path(
    original: Path,
    source_dir: Path,
    *,
    patch_ctx: PatchContext | None,
    requires_tests: bool,
) -> Path:
    """Return worktree-translated path if requires_tests gate is active, else original."""
    if not requires_tests or patch_ctx is None:
        return original
    try:
        original.resolve().relative_to(source_dir.resolve())
    except ValueError:
        return original  # Not under source_dir — no translation needed
    patcher = Patcher()
    return patcher.translate_path(patch_ctx, original)


def _finalize_patch(
    patch_ctx: PatchContext,
    patcher: Patcher,
    run_tests_fn,
    ctx: object,
) -> dict:
    """Run tests against worktree; apply on pass, discard on fail."""
    test_result = run_tests_fn(source_root=patch_ctx.worktree_path, ctx=ctx)
    if test_result["passed"]:
        patcher.apply(patch_ctx)
        return {"patch_applied": True, "test_output": test_result["output"]}
    else:
        patcher.discard(patch_ctx)
        return {"patch_applied": False, "test_output": test_result["output"]}
```

In the agent `run()` method, add gate logic around write_file/edit_file dispatch:
- Before dispatching write_file or edit_file, if `runbook.meta.safety.requires_tests` is True:
  - Lazily create `_patch_ctx` via `Patcher().create_worktree(ctx.source_dir, run_id)` on the first write call
  - Translate the `path` argument via `_route_write_path()`
- After the agent loop completes, if `_patch_ctx is not None`:
  - Call `_finalize_patch(_patch_ctx, patcher, run_tests_fn, ctx)`
  - Include the patch result in `AgentResult`

- [ ] **Run to verify pass**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/test_requires_tests_gate.py tests/overseer/test_sandbox_isolation.py -v
```
Expected: all PASSED

- [ ] **Run full overseer test suite**

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/ -v
```
Expected: all PASSED (no regressions)

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/agent/loop.py \
        tests/overseer/test_requires_tests_gate.py \
        tests/overseer/test_sandbox_isolation.py
git commit -m "feat(overseer): add requires_tests gate with Patcher integration to loop.py"
```

---

### Task 13: Starter runbooks

**Files:**
- Create: `packages/overseer/yeoman_overseer/starter_runbooks/ops-memory-prune.md`
- Create: `packages/overseer/yeoman_overseer/starter_runbooks/ops-source-cleanup.md`

- [ ] **Create `ops-memory-prune.md`**

```markdown
---
name: ops-memory-prune
domain: memory
enabled: true
version: 1
origin: manual
trigger:
  kind: cron
  expr: "0 3 * * 0"  # weekly, Sunday 3am
escalate_to_llm: true
llm_budget:
  max_tokens: 8000
  max_tool_calls: 10
  llm_profile: overseerDefault
safety:
  max_actions_per_hour: 2
  requires_tests: false
---

## Purpose

Prune stale low-salience memory entries weekly to keep `memory.db` compact and
retrieval performance high.

## Procedure

1. Query current memory stats via `query_memory` to understand volume and domain
   distribution.
2. Prune entries older than 60 days with salience below 0.3.
3. Send a summary alert with rows deleted and snapshot path.

## Safety

- `requires_tests: false` — no source code is touched.
- A snapshot is taken before any deletion. Recovery is always possible.
- Do not delete entries with salience > 0.5 regardless of age.
```

- [ ] **Create `ops-source-cleanup.md`**

```markdown
---
name: ops-source-cleanup
domain: ops
enabled: true
version: 1
origin: manual
trigger:
  kind: cron
  expr: "0 2 * * 0"  # weekly, Sunday 2am
escalate_to_llm: true
llm_budget:
  max_tokens: 8000
  max_tool_calls: 15
  llm_profile: overseerDefault
safety:
  max_actions_per_hour: 5
  requires_tests: false
  shell_timeout_s: 60
---

## Purpose

Remove stale files from `~/.yeoman/var/cache/` and `~/.yeoman/var/media/` weekly
to prevent unbounded disk growth.

## Procedure

1. Check disk usage via `check_health`.
2. Use `shell` to list files older than 14 days in `~/.yeoman/var/cache/` and
   `~/.yeoman/var/media/incoming/`.
3. Use `shell` to delete them (`find ... -mtime +14 -delete`).
4. Re-check disk usage and send a summary alert.

## Safety

- `requires_tests: false` — no source code is touched.
- Only touches `~/.yeoman/var/` subdirectories (cache and media).
- Shell commands run inside bubblewrap — no network, no source code access.
```

- [ ] **Verify files are present**

```bash
ls packages/overseer/yeoman_overseer/starter_runbooks/
```
Expected: `ops-memory-prune.md`, `ops-source-cleanup.md` (plus the Phase 2 runbooks)

- [ ] **Commit**

```bash
cd ~/Documents/yeoman
git add packages/overseer/yeoman_overseer/starter_runbooks/ops-memory-prune.md \
        packages/overseer/yeoman_overseer/starter_runbooks/ops-source-cleanup.md
git commit -m "feat(overseer): add Phase 3 starter runbooks (ops-memory-prune, ops-source-cleanup)"
```

---

## Completion Checklist

After all tasks, run the full suite one final time:

```bash
cd ~/Documents/yeoman && python -m pytest tests/overseer/ -v --tb=short
```

All tests must pass. Confirm:
- [ ] `test_schema_phase3.py` — SafetyConfig.shell_timeout_s
- [ ] `test_agent_sandbox.py` — bubblewrap wrapper, masking, cleanup
- [ ] `test_agent_patcher.py` — create_worktree, translate_path, apply, discard
- [ ] `test_tool_write_file.py` — path allowlist, deny-list (5 deny-list cases)
- [ ] `test_tool_edit_file.py` — exact replacement, deny-list, audit
- [ ] `test_tool_prune_memory.py` — snapshot-first, criteria, domain filter
- [ ] `test_tool_run_tests.py` — sandbox delegation, source_root, env vars
- [ ] `test_tool_git_revert.py` — SHA validation, audit, error propagation
- [ ] `test_tool_dry_run_runbook.py` — valid/invalid parsing, missing file
- [ ] `test_tool_shell.py` — sandbox call, timeout, audit, exception handling
- [ ] `test_requires_tests_gate.py` — path routing, apply on pass, discard on fail
- [ ] `test_sandbox_isolation.py` — distinct UUIDs per call
