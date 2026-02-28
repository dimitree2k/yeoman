# Rename nanobot to yeoman — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename the project from nanobot/nanobot-stack to yeoman across all source, config, docs, and assets.

**Architecture:** Mechanical bulk rename of the Python package directory and all imports, followed by targeted updates to path resolvers, config defaults, build metadata, bridge code, docs, and assets. A runtime migration helper auto-moves ~/.nanobot to ~/.yeoman on first run.

**Tech Stack:** Python, TypeScript (bridge), shell scripts, pyproject.toml (hatch build system)

---

### Task 1: Rename the Python package directory

**Files:**
- Rename: `nanobot/` → `yeoman/`

**Step 1: Rename the directory**

```bash
git mv nanobot yeoman
```

**Step 2: Verify the directory was renamed**

Run: `ls yeoman/__init__.py`
Expected: file exists

**Step 3: Commit**

```bash
git add -A && git commit -m "refactor: rename nanobot/ package directory to yeoman/"
```

---

### Task 2: Bulk-replace all Python imports

**Files:**
- Modify: all 136 `.py` files under `yeoman/`
- Modify: all 23 test files under `tests/`

**Step 1: Replace all `nanobot.` imports in source and tests**

```bash
find yeoman tests -name "*.py" -exec sed -i 's/\bfrom nanobot\b/from yeoman/g; s/\bimport nanobot\b/import yeoman/g; s/"nanobot\./"yeoman./g; s/'\''nanobot\./'\''yeoman./g' {} +
```

**Step 2: Verify no `nanobot` imports remain**

Run: `grep -r "from nanobot\.\|import nanobot\." yeoman/ tests/`
Expected: no output

**Step 3: Check for string references to `nanobot.` module paths (e.g. in mypy overrides, logging)**

Run: `grep -rn "nanobot\." yeoman/ tests/ --include="*.py" | grep -v __pycache__`
Fix any remaining references manually.

**Step 4: Commit**

```bash
git add -A && git commit -m "refactor: update all Python imports from nanobot to yeoman"
```

---

### Task 3: Update pyproject.toml

**Files:**
- Modify: `pyproject.toml`

**Step 1: Update package metadata and build config**

Apply these changes to `pyproject.toml`:

```
Line 2:  name = "nanobot-stack"          → name = "yeoman"
Line 4:  description = "...(fork of...)" → description = "Policy-first personal AI assistant runtime"
Line 8:  {name = "nanobot contributors"} → {name = "yeoman contributors"}
Line 44: nanobot = "nanobot.cli..."       → yeoman = "yeoman.cli.commands:app"
Line 45: nanobot-stack = "nanobot.cli..." → DELETE this line
Line 52: packages = ["nanobot"]           → packages = ["yeoman"]
Line 55: "nanobot" = "nanobot"            → "yeoman" = "yeoman"
Line 60: "nanobot/**/*.py"               → "yeoman/**/*.py"
Line 61: "nanobot/skills/**/*.md"        → "yeoman/skills/**/*.md"
Line 62: "nanobot/skills/**/*.sh"        → "yeoman/skills/**/*.sh"
Line 67: "nanobot/"                      → "yeoman/"
Line 74: "nanobot/bridge"               → "yeoman/bridge"
Line 91: ["nanobot.core.*", ...]         → ["yeoman.core.*", "yeoman.adapters.*", "yeoman.app.*"]
```

**Step 2: Verify toml is valid**

Run: `python -c "import tomllib; tomllib.load(open('pyproject.toml','rb')); print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add pyproject.toml && git commit -m "refactor: update pyproject.toml for yeoman package name"
```

---

### Task 4: Update package identity files

**Files:**
- Modify: `yeoman/__init__.py`
- Modify: `yeoman/__main__.py`

**Step 1: Update `yeoman/__init__.py`**

Replace the docstring:
```python
"""yeoman - Policy-first personal AI assistant runtime"""

__version__ = "0.2.0"
__logo__ = "🐈"
```

Note: bump version to 0.2.0 to mark the rename.

**Step 2: Update `yeoman/__main__.py`**

Change line 9:
```python
# Old:
load_dotenv(os.path.expanduser("~/.nanobot/.env"), override=False)
# New:
load_dotenv(os.path.expanduser("~/.yeoman/.env"), override=False)
```

**Step 3: Commit**

```bash
git add yeoman/__init__.py yeoman/__main__.py && git commit -m "refactor: update package identity to yeoman v0.2.0"
```

---

### Task 5: Update path resolver with migration logic

**Files:**
- Modify: `yeoman/utils/helpers.py`

**Step 1: Update `get_data_path()` with migration logic**

Replace the current `get_data_path()` function (lines 14-22) with:

```python
def get_data_path() -> Path:
    """Get the yeoman data directory.

    Respects YEOMAN_HOME (or legacy NANOBOT_HOME) environment variable;
    falls back to ~/.yeoman. Migrates ~/.nanobot → ~/.yeoman on first run.
    """
    yeoman_home = os.environ.get("YEOMAN_HOME", "").strip()
    if yeoman_home:
        return ensure_dir(Path(yeoman_home))

    # Legacy fallback
    nanobot_home = os.environ.get("NANOBOT_HOME", "").strip()
    if nanobot_home:
        return ensure_dir(Path(nanobot_home))

    new_dir = Path.home() / ".yeoman"
    old_dir = Path.home() / ".nanobot"

    if not new_dir.exists() and old_dir.exists():
        import shutil
        print(f"Migrating runtime directory: {old_dir} → {new_dir}")
        shutil.move(str(old_dir), str(new_dir))

    return ensure_dir(new_dir)
```

**Step 2: Update all docstrings in the same file**

Replace `~/.nanobot` with `~/.yeoman` and `NANOBOT_HOME` with `YEOMAN_HOME` in all
docstrings for `get_var_path`, `get_logs_path`, `get_run_path`, `get_cache_path`,
`get_operational_data_path`, `get_secrets_path`.

**Step 3: Commit**

```bash
git add yeoman/utils/helpers.py && git commit -m "feat: update path resolver for yeoman with nanobot migration fallback"
```

---

### Task 6: Update config loader and schema

**Files:**
- Modify: `yeoman/config/loader.py` (lines 24, 30, 35-37, 107)
- Modify: `yeoman/config/schema.py` (lines 111, 199, 393, 439)

**Step 1: Update `loader.py`**

- Line 24 docstring: `nanobot` → `yeoman`
- Line 30 docstring: `~/.nanobot/.env` → `~/.yeoman/.env`, `$NANOBOT_HOME` → `$YEOMAN_HOME`
- Lines 35-36: Update env var and path:
  ```python
  yeoman_home = os.environ.get("YEOMAN_HOME", "").strip()
  if not yeoman_home:
      yeoman_home = os.environ.get("NANOBOT_HOME", "").strip()
  base = Path(yeoman_home) if yeoman_home else Path.home() / ".yeoman"
  ```
- Line 107 docstring: `~/.nanobot/.env` → `~/.yeoman/.env`

**Step 2: Update `schema.py` defaults**

- Line 111: `auth_dir: str = "~/.yeoman/secrets/whatsapp-auth"`
- Line 199: `workspace: str = "~/.yeoman/workspace"`
- Line 393: `allowlist_path: str = "~/.config/yeoman/mount-allowlist.json"`
- Line 439 docstring: `nanobot` → `yeoman`

**Step 3: Commit**

```bash
git add yeoman/config/loader.py yeoman/config/schema.py
git commit -m "refactor: update config loader and schema paths for yeoman"
```

---

### Task 7: Update TypeScript bridge

**Files:**
- Modify: `bridge/src/index.ts` (lines 39-40, 66)

**Step 1: Update path defaults and log message**

```typescript
// Line 39:
const AUTH_DIR = process.env.AUTH_DIR || join(homedir(), '.yeoman', 'whatsapp-auth');
// Line 40:
const MEDIA_DIR = process.env.MEDIA_DIR || join(homedir(), '.yeoman', 'media');
// Line 66:
console.log('yeoman WhatsApp Bridge');
```

**Step 2: Commit**

```bash
git add bridge/src/index.ts && git commit -m "refactor: update bridge paths from .nanobot to .yeoman"
```

---

### Task 8: Update shell script

**Files:**
- Modify: `core_agent_lines.sh`

**Step 1: Replace all `nanobot` references with `yeoman`**

```bash
sed -i 's/nanobot/yeoman/g' core_agent_lines.sh
```

**Step 2: Verify**

Run: `grep nanobot core_agent_lines.sh`
Expected: no output

**Step 3: Commit**

```bash
git add core_agent_lines.sh && git commit -m "refactor: update line-counting script for yeoman"
```

---

### Task 9: Rename image assets

**Files:**
- Rename: `nanobot_logo.png` → `yeoman_logo.png`
- Rename: `nanobot_arch.svg` → `yeoman_arch.svg`

**Step 1: Rename assets**

```bash
git mv nanobot_logo.png yeoman_logo.png
git mv nanobot_arch.svg yeoman_arch.svg
```

**Step 2: Commit**

```bash
git add -A && git commit -m "refactor: rename image assets to yeoman"
```

---

### Task 10: Update documentation — README.md

**Files:**
- Modify: `README.md` (~54 occurrences)

**Step 1: Bulk replace `nanobot` references**

- Replace `nanobot-stack` → `yeoman` (project name)
- Replace `nanobot_logo.png` → `yeoman_logo.png`
- Replace `nanobot_arch.svg` → `yeoman_arch.svg`
- Replace `nanobot` CLI command references → `yeoman`
- Replace `~/.nanobot` → `~/.yeoman`
- Replace the fork notice (line 18) with:
  ```
  > Originally inspired by [HKUDS/nanobot](https://github.com/HKUDS/nanobot). MIT license preserved.
  ```
- Remove references to `UPSTREAM.md` (it will be trimmed)
- Remove the footer fork attribution (line ~351)
- Update GitHub repo URL references to `dimitree2k/yeoman`

**Step 2: Review the file manually to ensure coherence**

**Step 3: Commit**

```bash
git add README.md && git commit -m "docs: update README for yeoman rename"
```

---

### Task 11: Update documentation — CLAUDE.md, SECURITY.md, UPSTREAM.md

**Files:**
- Modify: `CLAUDE.md` (~26 occurrences)
- Modify: `SECURITY.md` (~27 occurrences)
- Modify: `UPSTREAM.md` (rewrite)

**Step 1: Update CLAUDE.md**

Bulk replace:
- `nanobot` → `yeoman` in module paths, commands, directory references
- `~/.nanobot` → `~/.yeoman`
- `nanobot-stack` → `yeoman`
- Update the header line to: `Lightweight, policy-first personal AI assistant runtime (~18k core lines).`
- Remove: `Independent fork of HKUDS/nanobot. MIT license.`

**Step 2: Update SECURITY.md**

Bulk replace:
- `nanobot` → `yeoman` throughout
- `~/.nanobot` → `~/.yeoman`
- Remove the upstream link line (`- Upstream project: https://github.com/HKUDS/nanobot`)

**Step 3: Rewrite UPSTREAM.md to minimal acknowledgment**

```markdown
# Project Origin

This project was originally inspired by [HKUDS/nanobot](https://github.com/HKUDS/nanobot).
The codebase has since been substantially rewritten. MIT license preserved.
```

**Step 4: Commit**

```bash
git add CLAUDE.md SECURITY.md UPSTREAM.md
git commit -m "docs: update CLAUDE.md, SECURITY.md, UPSTREAM.md for yeoman rename"
```

---

### Task 12: Update planning docs and skill docs

**Files:**
- Modify: `plans/legacy-cleanup-migration-plan.md` (~24 occurrences)
- Modify: `plans/external-access-options.md` (~37 occurrences)
- Modify: `plans/whatsapp-message-lifecycle.md` (~28 occurrences)
- Modify: skill `.md` files with minor references

**Step 1: Bulk replace in planning docs**

```bash
find plans/ -name "*.md" -exec sed -i 's/nanobot-stack/yeoman/g; s/~\/.nanobot/~\/.yeoman/g; s/nanobot/yeoman/g' {} +
```

**Step 2: Update skill docs**

```bash
find yeoman/skills/ -name "*.md" -exec sed -i 's/nanobot/yeoman/g' {} +
```

**Step 3: Commit**

```bash
git add plans/ yeoman/skills/
git commit -m "docs: update planning and skill docs for yeoman rename"
```

---

### Task 13: Update LICENSE

**Files:**
- Modify: `LICENSE`

**Step 1: Update copyright line**

```
# Old:
Copyright (c) 2025 nanobot contributors
# New:
Copyright (c) 2025 yeoman contributors
```

**Step 2: Commit**

```bash
git add LICENSE && git commit -m "docs: update LICENSE copyright to yeoman contributors"
```

---

### Task 14: Catch any remaining references

**Step 1: Search for any remaining `nanobot` references**

```bash
grep -rn "nanobot" --include="*.py" --include="*.toml" --include="*.md" --include="*.ts" --include="*.sh" --include="*.json" . | grep -v __pycache__ | grep -v .git/ | grep -v uv.lock | grep -v node_modules
```

**Step 2: Fix any remaining references found**

Each should be evaluated — some may be legitimate (e.g. the fork acknowledgment in README, UPSTREAM.md).

**Step 3: Commit if changes were made**

```bash
git add -A && git commit -m "refactor: fix remaining nanobot references"
```

---

### Task 15: Run tests and reinstall

**Step 1: Reinstall the package**

```bash
pip install -e .
```

**Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests pass

**Step 3: Verify CLI works**

Run: `yeoman --help`
Expected: help output from typer

**Step 4: Run linter**

Run: `ruff check yeoman/`
Expected: clean or only pre-existing issues

**Step 5: Commit any fixes needed**

---

### Task 16: Update Claude memory files

**Files:**
- Modify: `/home/dm/.claude/projects/-home-dm-Documents-nanobot/memory/MEMORY.md`

**Step 1: Update all `nanobot` references to `yeoman`**

Update the memory file to reflect the new project name, paths, and commands.

**Step 2: No commit needed (not in git repo)**
