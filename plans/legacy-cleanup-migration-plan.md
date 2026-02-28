# Legacy Cleanup and Data Migration Plan

## Executive Summary

After analyzing the codebase, there are **minimal legacy artifacts** requiring cleanup. The previous developers maintained good hygiene. The main items are:

1. **Duplicate `InMemoryTelemetry`** - Two implementations exist, need consolidation
2. **Existing data migrations** - Already implemented and working
3. **Message type unification** - Planned but not started (Design B from plan)

---

## 1. Duplicate InMemoryTelemetry (Priority: Medium)

### Current State

Two `InMemoryTelemetry` classes exist:

| Location | Lines | Features |
|----------|-------|----------|
| `yeoman/adapters/telemetry.py` | 23 | Simple counter-only, debug logging |
| `yeoman/telemetry/inmemory.py` | 65 | Full implementation (counters, gauges, histograms, timings) |

### Usage

```bash
# Current imports
yeoman/app/bootstrap.py:from yeoman.adapters.telemetry import InMemoryTelemetry
yeoman/cli/commands.py:from yeoman.adapters.telemetry import InMemoryTelemetry
```

### Migration Plan

1. **Update imports** in `bootstrap.py` and `commands.py`:
   ```python
   # Change from:
   from yeoman.adapters.telemetry import InMemoryTelemetry
   
   # Change to:
   from yeoman.telemetry import InMemoryTelemetry
   ```

2. **Deprecate** `yeoman/adapters/telemetry.py`:
   - Add deprecation warning
   - Keep for backward compatibility for 1-2 releases
   - Re-export from new location

3. **Remove** `yeoman/adapters/telemetry.py` after deprecation period

### Migration Code

```python
# yeoman/adapters/telemetry.py (updated)
"""Simple structured telemetry sink for vNext intents.

DEPRECATED: Use yeoman.telemetry.InMemoryTelemetry instead.
This module will be removed in a future release.
"""

from __future__ import annotations

import warnings

from yeoman.telemetry.inmemory import InMemoryTelemetry

warnings.warn(
    "yeoman.adapters.telemetry.InMemoryTelemetry is deprecated. "
    "Use yeoman.telemetry.InMemoryTelemetry instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["InMemoryTelemetry"]
```

---

## 2. Existing Data Migrations (No Action Needed)

The codebase already has robust data migrations:

### Chat Registry Migration
**File**: `yeoman/storage/chat_registry.py`

| Migration | Description | Status |
|-----------|-------------|--------|
| `_migrate_legacy_default_path()` | Moves `~/.yeoman/inbound/chat_registry.db` to `~/.yeoman/data/inbound/` | ✅ Active |
| `migrate_from_seen_chats()` | Converts legacy `seen_chats.json` to registry DB | ✅ Active |

### Session Migration
**File**: `yeoman/utils/helpers.py`

| Migration | Description | Status |
|-----------|-------------|--------|
| Session directory migration | Moves `~/.yeoman/sessions/*.jsonl` to `~/.yeoman/data/sessions/` | ✅ Active |

### Persona Path Migration
**File**: `yeoman/policy/persona.py`

| Migration | Description | Status |
|-----------|-------------|--------|
| `_legacy_persona_relative()` | Maps old `memory/personas/*` paths to `personas/*` | ✅ Active |

### Config Migration
**File**: `yeoman/config/loader.py`

| Migration | Description | Status |
|-----------|-------------|--------|
| memory2 collapse | Converts deprecated `memory2` config to `memory` | ✅ Active |

**Recommendation**: These migrations are working and should remain in place until a major version bump.

---

## 3. Message Type Unification (Future Work)

### Current State (Design B from Plan)

Three message types exist:

| Type | Location | Purpose |
|------|----------|---------|
| `InboundEvent` | `yeoman/core/models.py` | Orchestrator input (15 fields) |
| `InboundMessage` | `yeoman/bus/events.py` | Bus transport |
| `InboundEvent` (bridge) | `bridge/src/protocol.ts` | WhatsApp bridge protocol (19 fields) |

### Migration Plan (From Original Plan)

This is **Phase 0.4-0.7** of the original plan and has **not been started**:

1. **Phase 0.4**: Add `Message.from_inbound_event()` converter
2. **Phase 0.5**: Migrate channels to produce `Message` directly
3. **Phase 0.6**: Update pipeline middleware to use `Message`
4. **Phase 0.7**: Update ports, context builder, remove old types

### Recommendation

**Defer** - This is a significant refactor with no urgent driver. The current system works. Consider this when:
- Adding new channels
- Encountering media handling bugs
- Needing to pass structured media through the pipeline

---

## 4. Files Already Cleaned Up

The plan mentioned removing these files, but they **no longer exist**:

| File | Status |
|------|--------|
| `bridge/src/server.ts.bak` | ✅ Already removed |
| `bridge/src/whatsapp.ts.bak` | ✅ Already removed |

---

## 5. Recommended Actions

### Immediate (This Release)

| Action | Effort | Risk |
|--------|--------|------|
| Update imports from `adapters.telemetry` to `telemetry` | 5 min | Low |
| Add deprecation warning to `adapters/telemetry.py` | 5 min | Low |

### Next Release

| Action | Effort | Risk |
|--------|--------|------|
| Remove `adapters/telemetry.py` | 2 min | Low |

### Future (Major Version)

| Action | Effort | Risk |
|--------|--------|------|
| Remove legacy data migrations | 30 min | Medium |
| Message type unification (Design B) | 2-3 days | High |

---

## 6. Implementation Checklist

```markdown
[ ] Update imports in yeoman/app/bootstrap.py
[ ] Update imports in yeoman/cli/commands.py
[ ] Add deprecation warning to yeoman/adapters/telemetry.py
[ ] Run tests to verify no breakage
[ ] Document change in CHANGELOG
```

---

## 7. Rollback Plan

If issues arise after the telemetry migration:

1. Revert import changes in `bootstrap.py` and `commands.py`
2. The old `adapters/telemetry.py` still exists with deprecation warning
3. No data loss possible - only code paths changed

---

## Summary

**Good news**: The codebase is already clean. The only actionable legacy item is the duplicate `InMemoryTelemetry`, which is a simple import change. All data migrations are working correctly and should remain in place.
