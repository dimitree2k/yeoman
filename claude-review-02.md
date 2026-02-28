# Spacebot Ideas: Revised Applicability Assessment for Nanobot

## Context

Evolve yeoman into a more secure, flexible, observable platform without losing current strengths (deterministic policy, security staging, bubblewrap sandbox, WhatsApp depth, media pipeline, memory system).

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
| Compactor (3-tier: 80/85/95%) | **Solid** (`src/agent/compactor.rs`, 350 LOC) | **Gap** — but yeoman uses short sessions + memory recall, so less urgent than assumed |
| Prometheus metrics (feature-gated) | **Solid** (`METRICS.md`, hooks) | **Gap** — InMemoryTelemetry only |
| Control plane API (Axum, SSE) | **Solid** (`src/api/server.rs`, comprehensive) | **Gap** — CLI only |
| MCP integration (rmcp crate) | **Solid** (`src/mcp.rs`, 638 LOC) | **Gap** — no MCP at all |
| Skill installer (GitHub + file) | **Solid** (`src/skills.rs`, 511 LOC) | Partial — local load only |
| Model routing + fallback chains | **Solid** (`src/llm/routing.rs`, 398 LOC) | **Gap** — router is 64 LOC, no fallback |
| Memory bulletin (cortex phase 1) | **Partial** — bulletin loop works, rest is stubs | Gap — but low priority |
| File ingestion pipeline | **Solid** (`src/agent/ingestion.rs`, 350 LOC) | Gap |
| Message coalescing | **Solid** (channel-level debounce) | Partial — WA only |
| User-scoped memory | **Design doc only**, zero code | **Already implemented in yeoman** |
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

5. **Run full lint/type pass**: `ruff check yeoman/ && mypy yeoman/core yeoman/adapters` — fix all warnings

### Phase 1: Proven Value — Observability + Reliability

**1. Model Routing + Fallback Chains** (extends `yeoman/media/router.py`)
- **Why first**: Prevents manual recovery on provider outages. Enables cheaper models for background tasks. Infrastructure that later features depend on (compactor can use cheap models, MCP tools can use task-specific models).
- **Steal from Spacebot**: `src/llm/routing.rs` — resolution hierarchy (task → process → fallback), rate limit cooldown with configurable seconds, per-task model override.
- **Scope**: Extend `ModelRouter` (currently 64 LOC) with fallback list per profile and 429/5xx cooldown tracking.
- **Files**: Update `yeoman/media/router.py`, `yeoman/config/schema.py`, `yeoman/providers/factory.py`
- **Success metric**: Zero failed requests during single-provider outage (measurable in logs today).

**2. Prometheus Metrics** (replaces `yeoman/adapters/telemetry.py`)
- **Why**: Makes everything measurable. Current InMemoryTelemetry (24 LOC) is a noop sink.
- **Steal from Spacebot**: `METRICS.md` (metric naming convention), `src/llm/pricing.rs` (cost estimation), feature-gate pattern.
- **Scope**: Pluggable metrics backend behind TelemetryPort. Prometheus exporter + `/metrics` endpoint. Keep in-memory for tests/dev.
- **Key metrics**: `yeoman_llm_requests_total`, `yeoman_llm_tokens_total`, `yeoman_llm_cost_dollars`, `yeoman_tool_calls_total`, `yeoman_llm_request_duration_seconds`
- **Files**: New `yeoman/telemetry/prometheus.py`, update `yeoman/core/ports.py`, update `yeoman/adapters/telemetry.py`
- **Security**: `/metrics` endpoint bind to localhost only by default.
- **Success metric**: Grafana dashboard showing P95 response time + cost per model.

**3. Control Plane API (minimal slice)**
- **Why**: CLI-only limits operational agility. Start small.
- **Scope**: FastAPI server, minimal endpoints: `/health`, `/status`, `/channels`, `/config/reload`, `/metrics` (Prometheus). SSE event stream deferred.
- **Steal from Spacebot**: `src/api/server.rs` endpoint surface design, state sharing pattern.
- **Files**: New `yeoman/api/server.py`, new `yeoman/api/routes/`, update `yeoman/app/bootstrap.py`
- **Security**: Token auth required from day one. Rate limiting on all endpoints. Audit logging for admin operations.
- **Success metric**: `curl localhost:8080/health` returns 200.

### Later Phases (Parked — revisit after Phase 1)

- **Skill Installer** — GitHub-based install for remote skills
- **Memory Bulletin** — ambient knowledge synthesis (hourly cortex loop)
- **Compactor** — only if context overflow becomes measured problem
- **File Ingestion** — workspace/ingest directory watcher
- **MCP integration** — deferred until concrete external tool need
- Everything else from original backlog — deferred indefinitely

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
| Code cleanup | `ruff check yeoman/` + `mypy yeoman/core yeoman/adapters` = 0 errors |
| File decomposition | No file over 800 LOC in core modules |
| Documentation | All public APIs have docstrings |

---

## Architectural Vision: Middleware Pipeline + Unified Message Model

These two refactors can be woven into Phase 0 (code health) since they decompose the largest files and improve type safety. They are pure refactors — identical runtime behavior, no new features.

---

### Design A: Middleware Pipeline (replaces monolithic `Orchestrator.handle()`)

**Problem**: `orchestrator.py` is 1139 lines in a single `handle()` method with 18 sequential stages, early-exit branches, and accumulated state. Hard to test individual stages, hard to insert new stages (metrics, rate limiting, compaction check), hard to read.

**Solution**: Decompose into independent middleware classes with a ~50-line pipeline runner. Uses the **pipeline chain** pattern (same as Express.js, Django, FastAPI) — each middleware calls `next()` to pass through or halts to short-circuit. Chosen over event-bus (hard to debug implicit ordering in Python) and decorator-wrapping (unreadable at 14 levels deep).

#### Core Abstractions

```python
# yeoman/core/pipeline.py (~50 lines)

@dataclass
class PipelineContext:
    """Mutable state flowing through the middleware chain."""
    event: InboundEvent                           # Can be replaced/enriched by middleware
    decision: PolicyDecision | None = None        # Set by PolicyMiddleware
    intents: list[OrchestratorIntent] = field(default_factory=list)
    halted: bool = False                          # Signals early exit

class Middleware(Protocol):
    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None: ...

NextFn = Callable[[PipelineContext], Awaitable[None]]

class Pipeline:
    def __init__(self, layers: list[Middleware]) -> None:
        self._layers = layers

    async def run(self, event: InboundEvent) -> list[OrchestratorIntent]:
        ctx = PipelineContext(event=event)
        await self._execute(ctx, index=0)
        return ctx.intents

    async def _execute(self, ctx: PipelineContext, index: int) -> None:
        if ctx.halted or index >= len(self._layers):
            return
        layer = self._layers[index]
        await layer(ctx, lambda c: self._execute(c, index + 1))
```

**Key design**: Each middleware calls `await next(ctx)` to pass through, or sets `ctx.halted = True` and appends intents to short-circuit. Post-processing is done after `await next(ctx)` returns.

#### Stage → Middleware Mapping

| Stage | Middleware Class | File | Short-circuits? |
|-------|-----------------|------|-----------------|
| 1. Normalize | `NormalizationMiddleware` | `pipeline/normalize.py` | Yes (empty) |
| 2. Dedup | `DeduplicationMiddleware` | `pipeline/dedup.py` | Yes (duplicate) |
| 3. Archive | `ArchiveMiddleware` | `pipeline/archive.py` | No |
| 4. Context enrich | `ReplyContextMiddleware` | `pipeline/reply_context.py` | No |
| 5. Admin commands | `AdminCommandMiddleware` | `pipeline/admin.py` | Yes (handled) |
| 6. Policy eval | `PolicyMiddleware` | `pipeline/policy.py` | No (sets ctx.decision) |
| 7. Idea/backlog | `IdeaCaptureMiddleware` | `pipeline/idea_capture.py` | Yes (captured) |
| 8. Access denied | `AccessControlMiddleware` | `pipeline/access.py` | Yes (!accept) |
| 9. New chat notify | `NewChatNotifyMiddleware` | `pipeline/new_chat.py` | No |
| 10. No-reply filter | `NoReplyFilterMiddleware` | `pipeline/no_reply.py` | Yes (!respond) |
| 11. Input security | `InputSecurityMiddleware` | `pipeline/security_input.py` | Yes (blocked) |
| 12. Typing + LLM | `ResponderMiddleware` | `pipeline/responder.py` | No |
| 13. Output security | `OutputSecurityMiddleware` | `pipeline/security_output.py` | No (modifies reply) |
| 14-17. Outbound | `OutboundMiddleware` | `pipeline/outbound.py` | No |

**Note**: Stages 12-17 (typing → LLM → reaction → output security → voice → threading → assembly) can be grouped into 2-3 middleware classes since they're tightly coupled around the reply lifecycle. The responder middleware handles typing + LLM call; the outbound middleware handles everything after the reply text is produced.

#### Dependency Distribution

The current `Orchestrator.__init__` takes 14 parameters. Under the middleware model, each middleware takes only the dependencies it needs:

```python
# Construction in bootstrap.py
pipeline = Pipeline([
    NormalizationMiddleware(),
    DeduplicationMiddleware(ttl_seconds=config.dedupe_ttl),
    ArchiveMiddleware(archive=reply_archive),
    ReplyContextMiddleware(archive=reply_archive, window_limit=6, ambient_limit=8),
    AdminCommandMiddleware(handler=admin_handler),
    PolicyMiddleware(policy=policy_port),
    IdeaCaptureMiddleware(security=security_port),
    AccessControlMiddleware(),
    NewChatNotifyMiddleware(owner_resolver=owner_resolver),
    NoReplyFilterMiddleware(),
    InputSecurityMiddleware(security=security_port),
    ResponderMiddleware(responder=responder_port, typing_notifier=notifier),
    OutboundMiddleware(security=security_port, tts=tts, router=model_router),
])
```

#### File Layout

```
yeoman/core/
  pipeline.py           # Pipeline runner + PipelineContext + Middleware protocol (~50 LOC)
  orchestrator.py       # Kept as thin wrapper: constructs Pipeline, delegates handle()
yeoman/pipeline/
  __init__.py
  normalize.py          # ~20 LOC
  dedup.py              # ~40 LOC
  archive.py            # ~20 LOC
  reply_context.py      # ~120 LOC (ambient + reply window)
  admin.py              # ~60 LOC
  policy.py             # ~15 LOC
  idea_capture.py       # ~90 LOC (multilingual markers)
  access.py             # ~80 LOC (blocked sender + notes capture)
  new_chat.py           # ~100 LOC (WhatsApp owner notification)
  no_reply.py           # ~70 LOC (passive notes capture)
  security_input.py     # ~40 LOC
  responder.py          # ~50 LOC (typing + delegate to ResponderPort)
  outbound.py           # ~120 LOC (reaction, output security, voice, threading, assembly)
```

**Total**: ~875 LOC across 14 files vs 1139 LOC in one file. Net reduction + massive testability improvement.

#### Migration Strategy

1. Create `Pipeline` class and `PipelineContext` in `core/pipeline.py`
2. Extract one middleware at a time, starting from the edges (normalization, dedup)
3. Keep `Orchestrator.handle()` as the integration test — it constructs a Pipeline internally and delegates
4. Once all stages are extracted, `Orchestrator` becomes a thin factory (~30 LOC)
5. Existing tests continue to test via `Orchestrator.handle()` — zero test breakage
6. New tests can test individual middleware in isolation

#### What Changes in Ports

**Nothing.** The ports (`PolicyPort`, `ResponderPort`, `SecurityPort`, `ReplyArchivePort`) remain unchanged. Middleware classes consume ports the same way the monolithic method did.

---

### Design B: Unified Message Model (replaces 3 message types)

**Problem**: Three separate message types with overlapping fields:
1. WhatsApp's local `InboundEvent` (bridge protocol, 19 fields)
2. Bus's `InboundMessage` (generic, metadata dict)
3. Core's `InboundEvent` (orchestrator input, 15 fields + raw_metadata dict)

Media is prepended to text (`[image_description] ...`), transcripts are in raw_metadata, channel-specific data is untyped.

**Solution**: Structured content blocks with typed metadata.

#### Core Types

```python
# yeoman/core/message.py

@dataclass(frozen=True, slots=True)
class ContentBlock:
    """One element of message content."""
    kind: Literal["text", "image", "audio", "video", "sticker", "file"]
    text: str | None = None           # Text content, or caption for media
    path: str | None = None           # Media file path
    mime_type: str | None = None
    size_bytes: int | None = None
    transcript: str | None = None     # ASR output (audio/video)
    description: str | None = None    # Vision output (image/video/sticker)

@dataclass(frozen=True, slots=True)
class Identity:
    """Canonical sender representation."""
    id: str                           # Platform-specific ID
    display_name: str | None = None   # Human-readable name
    platform_handle: str | None = None  # @username, phone number, etc.

@dataclass(frozen=True, slots=True)
class ReplyRef:
    """Reference to a message being replied to."""
    message_id: str
    text: str | None = None           # Quoted text
    sender: Identity | None = None    # Who wrote the original

@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """Channel-agnostic message envelope."""
    id: str | None = None
    channel: str
    chat_id: str
    sender: Identity
    content: list[ContentBlock]       # Ordered content elements
    reply_to: ReplyRef | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_group: bool = False
    mentioned_bot: bool = False
    reply_to_bot: bool = False
    participant: str | None = None    # WhatsApp JID (kept for policy compat)
    metadata: dict[str, object] = field(default_factory=dict)
```

#### How Channels Construct Messages

**WhatsApp** (currently the most complex):
```python
# Instead of prepending [image_description] to text and putting transcript in raw_metadata:
content = []
if event.text:
    content.append(ContentBlock(kind="text", text=event.text))
if event.media_kind == "image":
    content.append(ContentBlock(
        kind="image",
        path=event.media_path,
        mime_type=event.media_type,
        size_bytes=event.media_bytes,
        description=vision_result,  # from _enrich_media_event
    ))
if event.media_kind == "audio":
    content.append(ContentBlock(
        kind="audio",
        path=event.media_path,
        transcript=asr_result,  # from _enrich_media_event
    ))
```

**Telegram**:
```python
content = []
if message.text:
    content.append(ContentBlock(kind="text", text=message.text))
if message.voice:
    content.append(ContentBlock(
        kind="audio",
        path=downloaded_path,
        transcript=groq_transcription,
    ))
if message.photo:
    content.append(ContentBlock(kind="image", path=downloaded_path))
```

**Discord**:
```python
content = [ContentBlock(kind="text", text=payload["content"])]
for attachment in payload.get("attachments", []):
    content.append(ContentBlock(kind="file", path=downloaded_path))
```

#### How the Context Builder Consumes Content Blocks

Currently `context.py` receives `event.content` as a flat string with `[image_description]` and `[transcription]` markers prepended by the channel.

With content blocks, the context builder renders structured sections:

```python
def _render_content_blocks(blocks: list[ContentBlock]) -> str:
    parts = []
    for block in blocks:
        match block.kind:
            case "text":
                parts.append(block.text)
            case "image":
                if block.description:
                    parts.append(f"[Image: {block.description}]")
            case "audio":
                if block.transcript:
                    parts.append(f"[Voice message transcript: {block.transcript}]")
            case "video":
                if block.description:
                    parts.append(f"[Video: {block.description}]")
            case "sticker":
                if block.description:
                    parts.append(f"[Sticker: {block.description}]")
            case _:
                if block.text:
                    parts.append(block.text)
    return "\n".join(parts)
```

This moves media rendering logic from scattered channel code into one centralized function.

#### Bus Integration

The bus transports `Message` directly. Drop `InboundMessage` — it's an unnecessary intermediate class. Nanobot is single-process, the bus is just an `asyncio.Queue` between coroutines, no serialization boundary exists.

```python
class MessageBus:
    inbound: asyncio.Queue[Message]
    outbound: asyncio.Queue[OutboundMessage]
```

#### Migration Strategy

1. Define `Message`, `ContentBlock`, `Identity`, `ReplyRef` in `core/message.py`
2. Add `Message.from_inbound_event(event: InboundEvent) -> Message` converter — enables gradual migration
3. Update one channel at a time to produce `Message` instead of calling `_handle_message()`
4. Update `PipelineContext` to carry `Message` instead of `InboundEvent`
5. Update `ResponderPort.generate_reply()` signature to accept `Message`
6. Update context builder to use content blocks
7. Remove old `InboundEvent`, `InboundMessage`, `OutboundEvent` once all consumers migrated

**Migration order**: Bus → WhatsApp → Telegram → Discord → Orchestrator/Pipeline → ResponderPort → Context Builder

#### What Changes in Ports

- `PolicyPort.evaluate(Message) → PolicyDecision` (minor signature change)
- `ResponderPort.generate_reply(Message, PolicyDecision) → str | None` (minor signature change)
- `SecurityPort` unchanged (operates on text strings)
- `ReplyArchivePort` unchanged (operates on channel/chat/message IDs)

#### Impact on Tests

- `test_policy_engine.py` (1227 LOC) — needs `Message` construction helpers (add `tests/factories.py`)
- `test_tool_validation.py` — no change (tests tool execution, not message types)
- `test_whatsapp_channel_v2.py` — update InboundEvent construction to Message
- `test_reply_context_resolution.py` — update to use Message, but logic is identical

---

### Implementation Phasing for Both Designs

**These refactors slot into Phase 0 (Code Health)** since they decompose the two largest concerns:

1. **Phase 0.1**: Define `Message`, `ContentBlock`, `Identity`, `ReplyRef` types (new file, no changes to existing code)
2. **Phase 0.2**: Define `Pipeline`, `PipelineContext`, `Middleware` protocol (new file, no changes)
3. **Phase 0.3**: Extract middleware classes one at a time from `orchestrator.py` — keep Orchestrator as wrapper
4. **Phase 0.4**: Add `Message.from_inbound_event()` converter — enables gradual channel migration
5. **Phase 0.5**: Migrate channels to produce `Message` directly (one channel at a time)
6. **Phase 0.6**: Update pipeline middleware to use `Message` instead of `InboundEvent`
7. **Phase 0.7**: Update ports, context builder, remove old types

Each step is independently testable and deployable. No big-bang migration.

---

## Verification

For each implemented feature:
- Unit tests matching current coverage patterns (`tests/test_*.py`)
- Integration test with at least one real channel
- No regression in `pytest tests/` + `ruff check yeoman/` + `mypy yeoman/core yeoman/adapters`
- Measured success metric achieved before marking complete
