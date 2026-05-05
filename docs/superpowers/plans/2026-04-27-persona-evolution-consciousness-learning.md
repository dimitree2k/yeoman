# Persona Evolution And Consciousness Learning Plan

> For agentic workers: implement this plan phase-by-phase. Keep checkbox state
> updated as work lands so progress survives session changes.

Status: in_progress
Related specs:
- `../specs/2026-04-25-consciousness-layer-design.md`
- `../specs/2026-04-27-group-dynamics-outcome-learning-design.md`

## Goal

Combine the older persona self-evolution idea with the newer consciousness
outcome-learning loop.

Consciousness remains the autonomy layer: it watches eligible chats, decides
whether to speak, chooses an intervention type, logs what happened, and learns
from outcomes.

Persona evolution becomes the durable identity layer: it periodically updates
the persona's lived-experience file from evidence gathered by consciousness,
semantic memory, and recent chat history. It should shape long-term confidence,
relationship understanding, and stable behavioral tendencies without changing
base persona invariants.

## Current Problem

The two learning worlds are disconnected.

- `personas/<name>.evolution.md` is loaded into prompts when present, but the
  existing daily `$evolve` job does not reliably update it.
- `TasteDistiller` writes tactical chat-level patterns to semantic memory, but
  proactive consciousness currently searches memory with an empty query, which
  likely prevents those patterns from being read.
- Learned taste is tactical and chat-scoped. Persona evolution needs slower,
  evidence-backed consolidation across multiple signals.

## Architecture Boundary

Do not let live consciousness ticks rewrite persona files.

Use this flow instead:

```text
chat activity
  -> consciousness planner proposes or stays silent
  -> speakup log records proposal, commit, denial, or silent pass
  -> outcome enricher labels delayed result
  -> taste distiller writes chat-scoped tactical patterns
  -> persona evolution distiller periodically consolidates durable lessons
  -> persona loader includes evolved layer in future replies and speakups
```

## Runtime Anchors

| Concern | Current file or path |
|---------|----------------------|
| Persona loading and `.evolution.md` inclusion | `packages/gateway/yeoman_gateway/policy/persona.py` |
| Consciousness planner prompt | `packages/gateway/yeoman_gateway/consciousness/agent.py` |
| Consciousness read/write tools | `packages/gateway/yeoman_gateway/consciousness/tools.py` |
| Speakup and outcome storage | `~/.yeoman/data/consciousness/speakups.db` |
| Speakup log implementation | `packages/gateway/yeoman_gateway/consciousness/log.py` |
| Outcome labeling | `packages/gateway/yeoman_gateway/consciousness/outcomes.py` |
| Tactical taste distillation | `packages/gateway/yeoman_gateway/consciousness/taste.py` |
| Semantic memory storage | `~/.yeoman/data/memory/memory.db` |
| Semantic memory implementation | `packages/gateway/yeoman_gateway/memory/service.py`, `packages/gateway/yeoman_gateway/memory/store.py` |
| Reactive reply memory recall | `packages/gateway/yeoman_gateway/adapters/responder_llm.py` |
| Prompt memory injection | `packages/gateway/yeoman_gateway/agent/context.py` |
| Existing private evolution skill | `~/.yeoman/workspace/skills/evolve/SKILL.md` |

## Non-Goals

- Do not mutate base persona files like `alpha-2.md`.
- Do not let one bad outcome rewrite stable personality.
- Do not optimize for raw engagement or message volume.
- Do not create per-person persona switching.
- Do not copy raw private messages into `.evolution.md`.
- Do not bypass `consciousness.enabled`, chat opt-in, daily caps, preview,
  quiet hours, action allowlists, or security checks.

## Phase 0 - Fix Tactical Taste Retrieval

Goal: make existing learned chat taste actually available to proactive
consciousness decisions.

- [x] Add a memory service method or store query for recent chat-scoped
  preference memories without requiring a non-empty lexical query.
- [x] Add a consciousness tool method such as `read_learned_chat_taste()`.
- [x] Replace `search_memory("", chat_id, ...)` in `ConsciousnessAgent` with
  explicit learned-pattern retrieval.
- [x] Keep `search_memory(query, ...)` for query-specific memory lookup.
- [x] Tests: proactive planner prompt includes chat-scoped preference memories
  when they exist.
- [x] Tests: empty-query memory lookup does not silently hide learned taste.

Exit criteria:

- [x] A seeded `Proactive speakup taste pattern:` memory appears in the
  consciousness planner prompt for the matching chat.

## Phase 1 - Make Tactical Taste Observable

Goal: make outcome learning legible before feeding it into persona evolution.

- [x] Add a CLI/status query for learned chat taste by channel and chat id.
- [x] Add a compact query for recent speakups, outcomes, and distillations.
- [x] Add log lines when taste distillation writes, skips, or fails.
- [x] Add a small operational check that reports:
  - sent speakups
  - labeled outcomes
  - taste distillation count
  - last learned taste per eligible chat

Exit criteria:

- [x] It is possible to answer "what did Yeoman learn in this chat?" without
  directly opening SQLite.

## Phase 2 - Replace Broken `$evolve` Cron With In-Process Evolution Job

Goal: stop relying on an agent job that cannot reliably access DBs, contacts,
and files.

- [x] Add a `persona_evolution` module under
  `packages/gateway/yeoman_gateway/`.
- [x] Add a read-only evidence collector that gathers:
  - current `<persona>.evolution.md`
  - policy chats using that persona
  - recent speakups and outcomes
  - learned chat taste memories
  - recent semantic memories
  - bounded recent inbound archive counts
- [x] Decide whether to add a distiller route such as `persona.evolution`.
  Current decision: keep the deterministic structured proposal renderer until
  a real LLM distillation need appears.
- [x] Produce a structured proposed report, not a direct write in the first
  iteration.
- [x] Replace the private `evo1a2md` behavior with the typed
  `persona_evolution` cron payload while keeping the job id for continuity.
- [x] Tests: evidence collector respects persona file mapping and chat scope.
- [x] Tests: proposed updates never modify base persona files.
- [x] Tests: raw messages are excluded or summarized before LLM distillation.

Exit criteria:

- [x] A manual command can generate a proposed `alpha-2.evolution.md` report from
  current runtime evidence.

## Phase 3 - Durable Persona Evolution Format

Goal: make `.evolution.md` useful, bounded, and auditable.

- [x] Define a stable section contract:
  - `How This File Works`
  - `Trait Drift`
  - `Domain Confidence`
  - `Relationship Map`
  - `Schema Log`
  - `Consciousness Outcome Lessons`
  - `Consolidation Changelog`
- [x] Require evidence counts for every proposed change.
- [x] Require confidence and date on new durable lessons.
- [x] Limit each consolidation to small changes.
- [x] Preserve base persona invariants by reading the base persona before
  accepting proposed updates.
- [x] Tests: invalid section names and missing evidence are rejected; base
  persona hash changes are rejected before apply.

Exit criteria:

- [x] Evolution files grow in quality, not just length.
- [x] Every new durable lesson can be traced to enough supporting evidence.

## Phase 4 - Owner Review And Apply

Goal: keep persona mutation controlled until the loop proves reliable.

- [x] Add preview mode as the default for persona evolution.
- [x] Write proposed diffs under private runtime state, not source.
- [x] Add owner approval command or CLI command to apply a proposed evolution
  diff.
- [x] Record applied evolution metadata:
  - persona file
  - previous hash
  - new hash
  - evidence window
  - approval source
- [x] Tests: rejected or expired proposals do not change files.
- [x] Tests: apply fails if the base or evolution file changed since proposal.

Exit criteria:

- [x] The system can propose persona evolution automatically while file changes
  remain owner-controlled.

## Phase 5 - Scheduled Autonomy With Guardrails

Goal: let the system maintain its lived-experience layer without making unsafe
live edits.

- [x] Add config for persona evolution scheduling:
  - enabled
  - cron expression
  - minimum speakup/outcome samples
  - preview vs auto-apply
  - personas allowlist
- [x] Start with preview-only.
- [ ] Allow auto-apply only for low-risk sections after enough successful
  reviewed runs.
- [x] Add metrics for proposed, applied, rejected, and no-op consolidations.
- [x] Add a runtime status summary showing next evolution run and last result.

Exit criteria:

- [x] Persona evolution runs on schedule, proposes bounded changes, and never
  affects live message delivery during the same tick.

## Implementation Order

1. Fix proactive learned taste retrieval.
2. Add observability for taste and outcome learning.
3. Build manual persona evolution proposal generation.
4. Add owner review and apply.
5. Replace the broken private cron job.
6. Consider scheduled preview runs.
7. Consider limited auto-apply only after repeated clean reviews.

## Validation Matrix

Run targeted checks while iterating:

```bash
uv run python -m pytest tests/gateway/test_consciousness_phase3.py -q
uv run python -m pytest tests/gateway/ -q
uv run ruff check packages/gateway tests/gateway
```

Before enabling scheduled evolution:

```bash
uv run python -m pytest tests/ -q
uv run ruff check .
```

Operational validation:

```bash
yeoman restart
yeoman status
sqlite3 ~/.yeoman/data/memory/memory.db "select scope_key, content from memory2_nodes where content like 'Proactive speakup taste pattern:%' and is_deleted = 0;"
sqlite3 ~/.yeoman/data/consciousness/speakups.db "select status, outcome, count(*) from speakups group by status, outcome;"
```

## Open Questions

- Should persona evolution apply only to personas currently active in policy,
  or also to inactive personas with recent runtime evidence?
- Which sections are safe for future auto-apply, if any?
- Should tactical chat taste remain only in semantic memory, or should durable
  high-confidence chat patterns be copied into the persona evolution file?
- Should owner feedback commands such as `/teach <speakup_id> good|bad` be
  added before persona evolution uses outcome data heavily?
