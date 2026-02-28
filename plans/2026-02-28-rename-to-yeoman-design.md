# Rename yeoman to yeoman

**Date:** 2026-02-28
**Status:** Approved

## Context

This project was forked from HKUDS/yeoman. Since then, 85% of the codebase has been
rewritten or added new. Only ~15% of the current 27k lines trace back to the original.
The project has its own identity and architecture. Time to rename.

## New Identity

| Aspect | Old | New |
|--------|-----|-----|
| PyPI package | `yeoman` | `yeoman` |
| Python package dir | `yeoman/` | `yeoman/` |
| Import path | `from yeoman.*` | `from yeoman.*` |
| CLI command | `yeoman` / `yeoman` | `yeoman` |
| Runtime data dir | `~/.yeoman/` | `~/.yeoman/` |
| Env variable | `NANOBOT_HOME` | `YEOMAN_HOME` |
| GitHub repo | `dimitree2k/yeoman` | `dimitree2k/yeoman` |

Fork acknowledgment kept as a single line in README:
> Originally inspired by [HKUDS/yeoman](https://github.com/HKUDS/yeoman). MIT license preserved.

## Scope

~870 occurrences across ~140 files:

- **564** Python import/reference changes across 136 source files
- **23** test files with import updates
- **274** documentation references across 18 files
- **4** TypeScript bridge references
- **2** shell script updates
- **pyproject.toml** — package name, scripts, build paths, mypy overrides
- **Asset renames** — `nanobot_logo.png`, `nanobot_arch.svg`

## Execution Order

1. Rename `yeoman/` directory to `yeoman/`
2. Bulk find-replace all Python imports (`yeoman.` → `yeoman.`)
3. Update `pyproject.toml` (package name, scripts, build config, mypy)
4. Update `helpers.py` path resolver with migration logic
5. Update bridge TypeScript (`bridge/src/index.ts`)
6. Update tests (imports)
7. Rename assets, update documentation
8. Run tests to verify
9. Reinstall (`pip install -e .`)

## Runtime Migration

On first run after upgrade, if `~/.yeoman/` exists but `~/.yeoman/` does not:
- Print message: "Migrating runtime directory from ~/.yeoman to ~/.yeoman"
- Move the directory
- `NANOBOT_HOME` env var continues to work as fallback

## Backward Compatibility

**Provided:**
- `NANOBOT_HOME` env var fallback (checks both, prefers `YEOMAN_HOME`)
- Auto-migration of `~/.yeoman/` → `~/.yeoman/` on first run

**Not provided (clean break):**
- `yeoman` CLI command stops working
- `from yeoman.*` imports stop working (no external consumers)

## Files Requiring Manual Attention

- `yeoman/utils/helpers.py` — central path resolver, migration logic
- `README.md` — 54 occurrences, rewrite sections
- `CLAUDE.md` — 26 occurrences, update module map and references
- `SECURITY.md` — 27 occurrences
- `UPSTREAM.md` — trim to minimal acknowledgment
- `LICENSE` — update "yeoman contributors" to "yeoman contributors"

## What Stays Unchanged

- `bridge/` directory name (generic)
- Git history (no rewriting)
- Architecture and all runtime behavior
