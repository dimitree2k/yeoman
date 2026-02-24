# Spacebot Ideas: Revised Applicability Assessment for Nanobot

## Context

Evolve nanobot into a more secure, flexible, observable platform without losing current strengths (deterministic policy, security staging, bubblewrap sandbox, WhatsApp depth, media pipeline, memory system).

This revision incorporates source-level analysis of **both** codebases plus the Kilo Code critical review and your own inputs about hidden features, existing memory, and code quality priorities.

---

## What Nanobot Already Has (Often Missed)

Before adding anything, acknowledge what's strong and shouldn't be reinvented:

| Capability | Status | Key Files |
|-----------|--------|-----------|
| Deterministic policy engine | Production | `policy/engine.py`, `adapters/policy_engine.py` |
| 3-stage security (input/tool/output) | Production | `security/engine.py`, `security/rules.py` |
| Bubblewrap sandbox isolation | Production | `agent/tools/exec_isolation.py` |
| WhatsApp media pipeline (images, audio, video, stickers, reactions) | Production | `channels/whatsapp.py`, `media/` |
| SQLite + embeddings memory (FTS5, vector search, WAL) | Production | `memory/store.py`, `memory/service.py` |
| User/chat/global memory scopes | Production | `memory/models.py` |
| Short session + memory-augmented context | Production | `session/manager.py` (50 msg cap) + `memory/service.py` recall |
| WhatsApp debounce/coalescing | Production | `channels/whatsapp.py` |
| Background memory capture + extraction | Production | `memory/service.py`, `memory/extractor.py` |
| Typed intent pipeline | Production | `core/intents.py`, `core/orchestrator.py` |
| Idea/backlog capture | Production | `memory/store.py` (idea_backlog_items table) |

---

## Reality Check: Spacebot Source vs Design Docs

Verified by reading actual source at `~/Projects/spacebot`:

| Feature | Spacebot Code Status | Nanobot Gap? |
|---------|---------------------|--------------|
| Channel/Branch/Worker process model | **Solid** (`src/agent/{channel,branch,worker}.rs`) | Gap, but current arch works |
| Compactor (3-tier: 80/85/95%) | **Solid** (`src/agent/compactor.rs`, 350 LOC) | **Gap** — but nanobot uses short sessions + memory recall, so less urgent than assumed |
| Prometheus metrics (feature-gated) | **Solid** (`METRICS.md`, hooks) | **Gap** — InMemoryTelemetry only |
| Control plane API (Axum, SSE) | **Solid** (`src/api/server.rs`, comprehensive) | **Gap** — CLI only |
| MCP integration (rmcp crate) | **Solid** (`src/mcp.rs`, 638 LOC) | **Gap** — no MCP at all |
| Skill installer (GitHub + file) | **Solid** (`src/skills.rs`, 511 LOC) | Partial — local load only |
| Model routing + fallback chains | **Solid** (`src/llm/routing.rs`, 398 LOC) | **Gap** — router is 64 LOC, no fallback |
| Memory bulletin (cortex phase 1) | **Partial** — bulletin loop works, rest is stubs | Gap — but low priority |
| File ingestion pipeline | **Solid** (`src/agent/ingestion.rs`, 350 LOC) | Gap |
| Message coalescing | **Solid** (channel-level debounce) | Partial — WA only |
| User-scoped memory | **Design doc only**, zero code | **Already implemented in nanobot** |
| Multi-agent graph | **Design doc only**, zero code | N/A — skip |
| Prompt-level routing | **Design doc only** | N/A — skip |

---

## Items to Drop from the Backlog

| Item | Why Drop |
|------|----------|
| **P0-1 Runtime Split** | Current orchestrator works (1139 LOC, battle-tested). No production incident driving this. Do only if metrics prove it's needed. Kilo Code review: "If the answer is architectural purity, that's not P0." |
| **P0-2 Branch-First Dispatch** | Not implemented in Spacebot either. Vapor. Cannot steal what doesn't exist. |
| **P0-3 Compactor (as P0)** | **Demoted to P2.** Nanobot's context is already short (50-message cap) + memory-augmented recall. Context overflow is NOT the pain point both docs assumed. If long sessions become a problem, add it then. |
| **P2-11 User-Scoped Memory** | Already implemented — `user`/`chat`/`global` scope model with sector-to-scope mapping. At most a minor retrieval enhancement. |
| **P2-12 Multi-Agent Graph** | Design doc only in Spacebot, 491 LOC of docs, zero code. Unproven. |
| **P2-14 Process Timeline** | Depends on runtime split which is deferred. |

---

## Revised Priority Order

### Phase 0: Code Health (Do First)

**Rationale**: You value clean code, pythonic approach, documentation, standards. Establish the foundation before adding features.

1. **Remove legacy artifacts**
   - Delete `bridge/src/server.ts.bak`, `bridge/src/whatsapp.ts.bak`
   - Audit for any other stale files

2. **Decompose oversized files** (7 files over 1000 LOC)
   - `cli/commands.py` (2,467 LOC) → split into `cli/commands/{channel,memory,admin,config,skill}.py`
   - `adapters/policy_engine.py` (1,741 LOC) → extract logical sections
   - `policy/admin/service.py` (1,542 LOC) → split by admin command group
   - Others: evaluate case by case

3. **Ensure `__all__` exports** in all public modules (currently 83%)

4. **Add docstrings** to undocumented public APIs — especially the media pipeline, which is sophisticated but hidden

5. **Run full lint/type pass**: `ruff check nanobot/ && mypy nanobot/core nanobot/adapters` — fix all warnings

### Phase 1: Proven Value — Observability + Reliability

**1. Model Routing + Fallback Chains** (extends `nanobot/media/router.py`)
- **Why first**: Prevents manual recovery on provider outages. Enables cheaper models for background tasks. Infrastructure that later features depend on (compactor can use cheap models, MCP tools can use task-specific models).
- **Steal from Spacebot**: `src/llm/routing.rs` — resolution hierarchy (task → process → fallback), rate limit cooldown with configurable seconds, per-task model override.
- **Scope**: Extend `ModelRouter` (currently 64 LOC) with fallback list per profile and 429/5xx cooldown tracking.
- **Files**: Update `nanobot/media/router.py`, `nanobot/config/schema.py`, `nanobot/providers/factory.py`
- **Success metric**: Zero failed requests during single-provider outage (measurable in logs today).

**2. Prometheus Metrics** (replaces `nanobot/adapters/telemetry.py`)
- **Why**: Makes everything measurable. Current InMemoryTelemetry (24 LOC) is a noop sink.
- **Steal from Spacebot**: `METRICS.md` (metric naming convention), `src/llm/pricing.rs` (cost estimation), feature-gate pattern.
- **Scope**: Pluggable metrics backend behind TelemetryPort. Prometheus exporter + `/metrics` endpoint. Keep in-memory for tests/dev.
- **Key metrics**: `nanobot_llm_requests_total`, `nanobot_llm_tokens_total`, `nanobot_llm_cost_dollars`, `nanobot_tool_calls_total`, `nanobot_llm_request_duration_seconds`
- **Files**: New `nanobot/telemetry/prometheus.py`, update `nanobot/core/ports.py`, update `nanobot/adapters/telemetry.py`
- **Security**: `/metrics` endpoint bind to localhost only by default.
- **Success metric**: Grafana dashboard showing P95 response time + cost per model.

**3. Control Plane API (minimal slice)**
- **Why**: CLI-only limits operational agility. Start small.
- **Scope**: FastAPI server, minimal endpoints: `/health`, `/status`, `/channels`, `/config/reload`, `/metrics` (Prometheus). SSE event stream deferred.
- **Steal from Spacebot**: `src/api/server.rs` endpoint surface design, state sharing pattern.
- **Files**: New `nanobot/api/server.py`, new `nanobot/api/routes/`, update `nanobot/app/bootstrap.py`
- **Security**: Token auth required from day one. Rate limiting on all endpoints. Audit logging for admin operations.
- **Success metric**: `curl localhost:8080/health` returns 200.

### Phase 2: Ecosystem

**4. Skill Installer** (extends `nanobot/agent/skills.py`)
- **Why**: Current skill loading is local-only. GitHub install enables sharing.
- **Steal from Spacebot**: `src/skills.rs` install workflow.
- **Scope**: New `nanobot/skills/installer.py`, CLI subcommands `skill install/list/remove`.
- **Security**: Path traversal validation on archives. Content sandboxing.

### Phase 3: Intelligence (If Needed)

**6. Memory Bulletin** — ambient knowledge synthesis, hourly. Steal bulletin loop pattern from `src/agent/cortex.rs`. Skip cortex stubs.

**7. Compactor** — only if long-session context overflow becomes a measured problem. Current 50-message cap + memory recall is sufficient for most use cases. If added: token-aware (not message-count), preserve media context, 3-tier thresholds from Spacebot.

**8. File Ingestion Pipeline** — `workspace/ingest` directory watcher, chunk processing. Useful for bulk knowledge import.

### Defer Indefinitely

- Process runtime split (P0-1) — no proven need
- Branch-first dispatch (P0-2) — vapor
- **MCP integration (P1-6)** — deferred until there's a concrete external tool need. High security risk (dynamic tools vs static policy engine). Revisit when the ecosystem matures.
- Multi-agent graph (P2-12) — unproven
- Cross-channel coalescing (P2-13) — WhatsApp debounce works, others don't need it yet
- Process timeline (P2-14) — no process model to log
- Cron reliability (P2-15) — do when cron becomes a pain point

---

## Security Constraints (Non-Negotiable for All Phases)

1. Control plane API MUST require token auth from day one
2. Control plane MUST have rate limiting on all endpoints
3. Audit logging for all admin operations (API + CLI)
4. Skill installer MUST validate archives against path traversal
5. Fallback routing MUST NOT silently escalate to more expensive/less-secure models
6. Session data isolation — no cross-chat context leaks
7. No regression in policy evaluation, security staging, or bubblewrap isolation

---

## What Not to Lose (Expanded)

1. Deterministic per-chat policy controls
2. Security staging and redaction (3-stage pipeline)
3. Bubblewrap isolation and grant model
4. WhatsApp runtime depth and repair ergonomics
5. **Session persistence format** (JSONL — simple, debuggable)
6. **CLI ergonomics** (first-class interface, not fallback)
7. **Deterministic intent pipeline** (`Orchestrator.handle()` returns `list[OrchestratorIntent]`)
8. **Media pipeline** (vision, ASR, TTS, video frames, sticker description)
9. **Memory architecture** (SQLite + FTS5 + embeddings, WAL, sector taxonomy)

---

## Migration & Rollback

- Backward compatibility for session JSONL files, SQLite memory DBs, policy.json
- Database migration scripts for any schema changes
- Config file versioning for new fields (additive only, defaults for old configs)
- Rollback strategy: feature flags for new subsystems (metrics, MCP, API)

---

## Success Metrics (Measurable)

| Feature | Metric |
|---------|--------|
| Model fallback | Zero failed requests during single-provider outage |
| Prometheus metrics | Grafana dashboard shows P95 latency + cost/model |
| Control plane API | `curl localhost:8080/health` returns 200 |
| Code cleanup | `ruff check nanobot/` + `mypy nanobot/core nanobot/adapters` = 0 errors |
| File decomposition | No file over 800 LOC in core modules |
| Documentation | All public APIs have docstrings |

---

## Verification

For each implemented feature:
- Unit tests matching current coverage patterns (`tests/test_*.py`)
- Integration test with at least one real channel
- No regression in `pytest tests/` + `ruff check nanobot/` + `mypy nanobot/core nanobot/adapters`
- Measured success metric achieved before marking complete
