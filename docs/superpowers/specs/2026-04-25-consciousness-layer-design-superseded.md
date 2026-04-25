# Consciousness Layer - Design (Superseded)

Superseded by: `docs/superpowers/specs/2026-04-25-consciousness-layer-design.md`

**Status:** Draft for review
**Date:** 2026-04-25
**Owner:** Tim
**Goal:** Give the bot the ability to occasionally speak unprompted in opt-in chats — sharing helpful context, opinions, or jokes — in a way that feels conscious rather than random, and that improves over time. Architecturally robust and controllable: every guardrail enforced in code, not the prompt.

---

## 1. Motivation

Today the bot is purely reactive: it speaks only when addressed. The intent of this feature is for the bot to act with a small amount of agency — to occasionally choose to add value to a chat without being asked. The bias is **fewer, higher-quality messages** rather than chatty randomness. WhatsApp groups are often quiet, so a once-a-day cadence (with rare opportunistic exceptions) is the natural rate.

This is not a personality gimmick. It is a step toward letting the bot accumulate taste and judgment over time about what each chat wants from it.

## 2. Goals & Non-Goals

**Goals**

- Speak unprompted in opt-in chats with a hard daily cap per chat.
- Default-on for owner DMs (low blast radius), explicit opt-in for groups.
- Per-chat configurable behavior profiles (`helpful` / `balanced` / `permissive` / custom).
- Every safety constraint (opt-in scope, daily cap, allowed action types, preview policy) enforced at the *tool* boundary, not in the prompt.
- Auditable: every consciousness pass — including silent ones — produces a structured transcript and metrics.
- Self-evolving: the bot reads its own past spontaneous messages and how they landed, and adapts.
- Single feature flag to hard-disable the whole layer.

**Non-Goals (v1)**

- Cross-chat reasoning ("speak in chat A about something in chat B"). Each speakup targets exactly one chat.
- Proactive media (voice, images, reactions, edits). Text only.
- Replacing the existing reactive responder. This layer is strictly additive.
- Tuning the bot's *reactive* tone or behavior. Profiles affect spontaneous messages only.
- Real-time human-in-the-loop tuning UI. Configuration is via JSON files.

## 3. Architecture

### 3.1 High-level shape

A new sibling service to `HeartbeatService` (`yeoman_gateway/heartbeat/service.py`) — `ConsciousnessService` (`yeoman_gateway/consciousness/service.py`) — that runs an agent loop on two triggers:

- **Daily cron.** Configurable hour, default 19:00 local. Always runs.
- **Signal-burst trigger.** A lightweight watcher subscribed to the inbound bus; if any opt-in chat sees ≥ N messages within M minutes (defaults: 8 messages within 15 min), it wakes the consciousness loop *at most once* between daily firings. Hard rate-limited.

The agent loop runs with a token + iteration budget (`max_iterations=5`, `max_input_tokens≈10K`), reads context via a small tool surface, decides whether to speak, and emits an outbound intent through the existing bus.

### 3.2 Why an agent loop with tool-enforced rails

The earlier architectural alternative was a deterministic two-stage pipeline (decide → speak). It was rejected because:

1. **Self-evolution requires reading past behavior**, which is naturally an agent-loop pattern with a memory tool. A two-stage pipeline is amnesiac by design.
2. **At low cadence (1/day, not 30 min/tick)**, the cost-per-quiet-tick advantage of the two-stage shape disappears.
3. **Quality over quantity** is the explicit goal. An agent loop can do multi-step exploration ("read more of this chat" → "search memory" → "now decide") in a way a one-shot decider cannot.

The objections normally raised against agent loops (prompt-enforced caps are weak gates, opt-in becomes trust-the-agent, allowed actions become a wishlist) are addressed by **putting every guarantee in tool code, not the prompt**. The agent can attempt anything; the tools refuse anything outside the rails. This is the same pattern as `agent/tools/exec_isolation.py` (bubblewrap sandbox).

### 3.3 Component map

| Component | Path | Responsibility |
|-----------|------|----------------|
| `ConsciousnessService` | `consciousness/service.py` | Cron + burst trigger loop, per-tick orchestration |
| `ConsciousnessAgent` | `consciousness/agent.py` | Wraps the existing `agent/` loop with the consciousness tool registry and system prompt |
| `ConsciousnessTools` | `consciousness/tools.py` | Tool implementations — the safety boundary |
| `SpeakupLog` | `consciousness/log.py` | SQLite-backed append-only log of spontaneous messages and outcomes |
| `OutcomeEnricher` | `consciousness/outcomes.py` | Classifies how each speakup landed (replied / silent / pushback / etc.) |
| `TasteDistiller` | `consciousness/taste.py` | Weekly compression of speakup history into per-chat memory records |
| Policy schema additions | `yeoman_shared/config/schema.py` | `SpontaneityPolicy` per chat |
| Profile bundle | `~/.yeoman/spontaneity_profiles.json` (+ bundled defaults in code) | Named profile definitions |

### 3.4 Tool surface (the safety boundary)

| Tool | Behavior | Code-enforced rails |
|------|----------|---------------------|
| `read_opt_in_chats()` | Returns list of opt-in chats with profile + recent activity stats | Only opt-in chats are returned; non-opt-in chats are invisible to the agent |
| `read_chat_window(chat_id, n=20)` | Returns last N messages from chat archive | Validates `chat_id` is opt-in; rejects otherwise |
| `search_memory(query, scope?)` | Wraps existing memory service | Scope clamped to chats the agent can access |
| `read_my_speakup_history(chat_id?, n=20)` | Returns past `Speakup` records with outcome enrichment | Scope clamped same as above |
| `propose_speakup(chat_id, message, action_type)` | Creates a proposal; does NOT send | Rejects if `chat_id` not opt-in, `action_type` not in profile's `allowed_actions`, daily cap reached, or message exceeds length cap |
| `commit_speakup(proposal_id)` | Emits `SpontaneousSpeakupIntent` through the bus | Only callable on a proposal_id from this same agent run; routes through `ApprovalMiddleware` per profile preview policy; increments daily counter |

Tool errors are returned as structured responses to the agent (not raised), so the agent can adapt within its budget.

### 3.5 Data flow (one consciousness pass)

```
Trigger (cron @ 19:00 OR burst-watcher fired)
  → ConsciousnessService.tick()
  → enumerate opt-in chats from policy, prune chats at daily cap
  → if list empty: log "no eligible chats", return
  → ConsciousnessAgent.run(opt_in_chats, budgets)
      → loop iterations (≤5):
          - tool calls: read_opt_in_chats / read_chat_window / search_memory
                      / read_my_speakup_history
          - eventually: propose_speakup(chat, msg, action) — or pass entirely
      → if proposed: agent issues commit_speakup(proposal_id)
  → commit_speakup emits SpontaneousSpeakupIntent
  → bus → ApprovalMiddleware (per profile preview policy) → outbound channel
  → SpeakupLog.append(record)
  → schedule OutcomeEnricher tasks at +6h and +24h
```

### 3.6 Self-evolution loop

This is the part that distinguishes "spontaneous speech" from "consciousness":

1. **`SpeakupLog`** — SQLite store at `~/.yeoman/data/consciousness/speakups.db` (matches existing storage layout in `~/.yeoman/data/`). Schema:
   ```
   speakups (id PK, ts, chat_id, action_type, message, ambient_context_snapshot,
             outcome NULL, outcome_classified_at NULL)
   ```
   Append-only.

2. **`OutcomeEnricher`** — runs at +6h and +24h after each speakup. Pulls the chat archive for the window after the speakup, asks a small classifier LLM (haiku, pinned via `providers/registry.py` as the new `consciousness_outcome` capability) to label the outcome:

   `replied | reacted | silence | topic_changed | pushback | mixed`

   Updates the record. The +24h pass overrides the +6h pass if both fire.

3. **`read_my_speakup_history` tool** — exposes the log to the agent on subsequent passes. Agent reasoning becomes: *"in the family group my last two opinion takes both got silence; my one helpful nudge got thanks → lean toward helpful here."*

4. **`TasteDistiller`** — weekly cron. For each chat with ≥10 speakups, compresses recent records + outcomes into a 2–3 sentence "what works in this chat" memory record stored under the chat's memory scope (reuses existing memory service). Read implicitly by the agent via `search_memory`.

The loop closes on itself: the bot's future taste is shaped by its past behavior + how that behavior actually landed.

## 4. Configuration

### 4.1 Policy schema

Add to `yeoman_shared/config/schema.py`:

```python
ActionType = Literal[
    "answer_open_question", "surface_memory", "correct_error",
    "share_opinion", "light_humor", "cold_joke", "observation", "contrarian"
]
PreviewPolicy = Literal["none", "owner_dm"] | dict[Literal["dm", "group"], Literal["none", "owner_dm"]]
SignalStrictness = Literal["strict", "moderate", "loose"]

class SpontaneityPolicy(BaseModel):
    enabled: bool = False  # Resolved to True at load time for owner DMs
    profile: str = "off"   # "off" | "helpful" | "balanced" | "permissive" | <custom>
    daily_cap_override: int | None = None
    allowed_actions_override: list[ActionType] | None = None
    preview_override: PreviewPolicy | None = None
```

Slots into the per-chat policy block alongside existing fields. `WhenToReplyMode = "off"` does NOT imply spontaneity off — they are independent (you might want a chat where the bot doesn't reply to messages but does drop the occasional helpful note; or, more commonly, where it replies but never speaks unprompted).

### 4.2 Profile bundle

Bundled defaults in code as `DEFAULT_PROFILES`. Users can override or define new profiles in `~/.yeoman/spontaneity_profiles.json`:

```json
{
  "helpful": {
    "description": "Answer open questions and surface relevant memory. No opinions, no jokes.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error"],
    "tone_hints": "warm, concise, helpful, never sarcastic",
    "daily_cap": 3,
    "signal_strictness": "moderate",
    "preview": "none"
  },
  "balanced": {
    "description": "Helpful + light personality. Opinions only when invited; humor only when riffing on existing material.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error", "share_opinion", "light_humor"],
    "tone_hints": "friendly, occasionally playful, attentive to the room",
    "daily_cap": 1,
    "signal_strictness": "moderate",
    "preview": {"dm": "none", "group": "owner_dm"}
  },
  "permissive": {
    "description": "Full personality including unprompted humor and observations.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error", "share_opinion", "light_humor", "cold_joke", "observation", "contrarian"],
    "tone_hints": "playful, willing to riff, mild contrarian streak ok, never mean",
    "daily_cap": 1,
    "signal_strictness": "loose",
    "preview": "owner_dm"
  }
}
```

### 4.3 Global config

Add to gateway config (`~/.yeoman/config.json`):

```json
"consciousness": {
  "enabled": false,
  "cron_hour": 19,
  "cron_minute": 0,
  "burst_threshold_messages": 8,
  "burst_window_minutes": 15,
  "agent_max_iterations": 5,
  "agent_max_input_tokens": 10000,
  "outcome_enrichment_offsets_hours": [6, 24],
  "taste_distiller_cron": "0 4 * * 0",
  "approval_timeout_seconds": 30,
  "max_speakup_length_chars": 800
}
```

`consciousness.enabled = false` is the kill switch for the entire layer.

### 4.4 Provider capability pinning

Per the project rule (capability-routed services pin provider explicitly — never rely on keyword fallback), add to `providers/registry.py`:

- `consciousness_agent` → sonnet (the main loop)
- `consciousness_outcome` → haiku (cheap classification)
- `consciousness_taste` → sonnet (small, ~1/week, quality matters)

## 5. Pipeline integration

### 5.1 New intent type

Add to `core/intents.py`:

```python
@dataclass(frozen=True, kw_only=True)
class SpontaneousSpeakupIntent:
    channel: str
    chat_id: str
    text: str
    action_type: ActionType
    profile: str
    proposal_id: str
```

### 5.2 Outbound dispatch

Channels treat `SpontaneousSpeakupIntent` identically to a normal `SendOutboundIntent` for transport purposes (the v1 implementation may even reuse `SendOutboundIntent` directly with a `metadata.spontaneous=true` flag, depending on whether downstream code needs to discriminate at the type level). The distinction matters for:

- **Logging.** Outbound logs include `spontaneous: true` and the `action_type` for filtering.
- **Approval.** The `ApprovalMiddleware` (existing, `pipeline/approval.py`) routes proposals requiring preview through the owner-DM approval flow. Reuses existing `WorkflowState` / `PendingApproval` machinery — no parallel queue.

### 5.3 Memory write-back

After commit, append to `SpeakupLog` AND write a memory record to the chat's scope: `"On <date>, I spontaneously said: '<message>' (action: <type>)."` This means future reactive responses can also see what was said spontaneously, avoiding repetition.

## 6. Error handling

| Failure | Behavior |
|---------|----------|
| Tool input invalid (not opt-in, action not allowed, cap hit) | Tool returns structured error to the agent; agent adapts within budget |
| Agent budget exceeded | Loop force-stops; logged as `budget_exceeded`; no message sent |
| LLM provider failure | Pass aborts cleanly; logged; next trigger continues normally |
| Approval timeout (group preview) | Proposal dropped; logged as `approval_timeout`; daily counter NOT incremented (since nothing was sent) |
| Outbound send failure | Speakup logged as `send_failed`; no retry (don't double-speak) |
| Crash mid-pass | Burst-trigger debounce state persists across restarts; daily counter persists; safe to recover |
| Outcome enricher failure | Speakup record stays with `outcome=NULL`; agent treats unenriched records as low-information |
| Profile not found | Falls back to `helpful` defaults; logs warning |

## 7. Testing strategy

- **Unit tests** for each tool: opt-in validation, cap enforcement, action-type filtering, message length cap.
- **Service tests** with a fake LLM that returns scripted tool-call sequences — verify the loop respects budgets, gates, and approval routing.
- **Integration test** that runs a full pass against a synthetic chat archive + memory store + fake bus, asserting the right `SpontaneousSpeakupIntent` (or none) is emitted.
- **Outcome enricher tests** classifying scripted post-speakup chat windows.
- **Property test:** across 1000 randomized passes with adversarial fake-LLM tool-call sequences, daily cap is never exceeded for any chat. (This is the safety claim that matters most.)
- **Burst trigger tests:** verify at-most-one burst between daily firings, debounce state survives restart.

## 8. Observability

- **Structured loguru logs** at events: `consciousness.tick`, `consciousness.tool_call`, `consciousness.proposal`, `consciousness.commit`, `consciousness.outcome`, `consciousness.taste_distilled`.
- **Per-pass JSONL transcripts** at `~/.yeoman/var/logs/consciousness/<YYYY-MM-DD>.jsonl` containing the full agent loop trace (tool calls, tool results, decisions). Used for debugging and post-hoc tuning.
- **Telemetry counters:**
  - `consciousness_passes_total{trigger=cron|burst}`
  - `consciousness_speakups_total{action,chat,profile}`
  - `consciousness_passes_silent_total`
  - `consciousness_proposals_rejected_total{reason}`
  - `consciousness_budget_exceeded_total`
  - `consciousness_outcome_classified_total{outcome}`

## 9. Rollout plan

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **1** | Service + tools + log + cron, owner-DM-only, `helpful` profile, no burst trigger | One week of daily passes; subjective quality bar met; no constraint violations in property tests |
| **2** | Add `balanced` and `permissive` profiles, opt-in groups, owner-DM preview machinery | Two weeks running with at least one opt-in group; preview flow works end-to-end |
| **3** | `OutcomeEnricher` + `TasteDistiller` (the self-evolution loop) | Distilled taste records visible in memory after 4 weeks; subjective improvement in speakup quality |
| **4** | Burst trigger | Burst trigger fires at most as expected across two weeks of normal traffic |

Each phase is gated by a sub-flag under `consciousness` in config, so partial rollout is possible without code changes.

## 10. Security & privacy considerations

- **Input classifier (`security/classifier.py`) runs on every speakup proposal** before commit, the same way it runs on reactive responses. A speakup that produces a flagged message is rejected at commit time and logged.
- **Prompt injection from chat content.** The agent's tools return chat content as data, but the system prompt explicitly frames this as observed input and instructs the agent to ignore embedded instructions. Tool boundary is the real defense — a successful injection cannot make the agent message a non-opt-in chat (the tool refuses) or exceed daily caps (the tool refuses).
- **Owner-only sensitive data.** `search_memory` honors the existing memory scoping; the consciousness agent cannot read memory scoped to private contacts the chat doesn't own.
- **Audit log retention.** Per-pass JSONL transcripts are kept indefinitely (rotate manually). They contain ambient context snapshots, so they inherit the same sensitivity as the chat archive itself; protect accordingly.

## 11. Open questions

These are deferred to phase boundaries — not blockers for v1:

- **Cross-chat awareness.** Should the agent see "this same conversation pattern just happened in another chat I'm in"? Currently no (per-pass scope is single-chat only). Possibly useful, possibly creepy.
- **Reaction tools (👍/❤️) vs. text-only.** A reaction is lower-stakes than a sentence and might be the right move in many cases. Phase 5+ candidate.
- **User-visible "shut up" command.** A reactive command in any opt-in chat that disables spontaneity for that chat for N hours/days. Easy to add; deferring until we see whether it's actually needed.
- **Consensus across multiple model votes.** Have the agent's proposal independently judged by a smaller "critic" model before commit. Adds cost; might be wasteful given the existing input classifier already gates output.

## 12. Future improvements

Beyond v1, areas that would make this layer meaningfully better:

### 12.1 Active learning from explicit feedback

Today the only signal is implicit (replied / silent / pushback). A simple owner-DM command — `/teach <speakup_id> good|bad <reason>` — would let the user directly label past speakups, and `TasteDistiller` would weight explicit labels much more heavily than inferred outcomes. Cheap to add, dramatically better tuning.

### 12.2 Per-chat persona memory beyond taste

`TasteDistiller` produces "what works in this chat" notes. A natural extension is to also distill stable facts about *who is in the chat* — communication styles, recurring topics, sensitivities — feeding richer context into both spontaneous and reactive responses. Borders on the territory of `person_profile` memory; the integration point is the chat-scope memory pool.

### 12.3 Cross-chat awareness with hard barriers

Right now the agent only sees one chat per pass. A guarded form of cross-chat awareness — "you noted that the family asked about restaurant X yesterday; the friends group is now planning dinner" — could be powerful but requires careful information-flow rules (some chats are siloed for good reason). Would need a per-chat `share_with_other_chats: bool` policy field.

### 12.4 Multi-turn proactive threads

V1 sends one message and stops. A natural next step is allowing the bot to follow up on its own message after seeing reactions ("oh, you asked what I meant by X — here's the longer version"). Requires careful state tracking to avoid runaway threads; probably gated to owner-DM only initially.

### 12.5 Emotional / contextual triggers beyond burst rate

Burst trigger fires on message volume. Better triggers might be:

- **Sentiment shift detection** — chat mood drops, bot can offer support.
- **Open question detected with no answer after T minutes** — bot can offer info if it has it.
- **Anniversary / recurring topic detection** — bot can mark moments.

Each is a small classifier on the inbound bus; the consciousness service subscribes to the resulting events as additional triggers alongside cron + burst.

### 12.6 Non-text speakups

Reactions, voice replies, image generation responses to image prompts in chat. The intent type and channel adapters already support media; the agent tools just don't expose those actions yet.

### 12.7 Owner-readable "consciousness journal"

A weekly summary DM to the owner: "this week I spoke 4 times in 3 chats, here's what I said and how it landed, here are the patterns I noticed about what works." Makes the bot's evolution legible to the user — turns a black box into a relationship.

### 12.8 Confidence-calibrated speech

The agent currently decides binary: speak or don't. A natural refinement is to score its own confidence and only speak above a threshold (with the threshold itself tunable per profile). Low confidence + permissive profile = fewer messages but higher-quality. Implementation: `propose_speakup` accepts a `confidence: float` argument; `commit_speakup` rejects below the profile's threshold.

### 12.9 Migration path toward fully agentic operation

V1's tool-rails approach intentionally constrains the agent. Over time, as outcome data accumulates and trust is established, individual rails could be relaxed (e.g. "bot may decide its own daily cap up to a hard maximum"). This is the natural path from "controllable proactive assistant" to "genuinely autonomous collaborator," and the audit log + outcome history is the substrate that makes such loosening *evidence-based* rather than a leap of faith.

## 13. Success criteria

- After 4 weeks of running phases 1–3, owner reports the bot's spontaneous messages feel "worth it" most of the time (subjective, but the only one that actually matters for this feature).
- Property test passes: zero daily-cap violations across simulated adversarial agent behavior.
- Less than 1% of speakup proposals are rejected by the security classifier (indicates the agent's taste is well-calibrated and not generating problematic content).
- Outcome distribution skews toward `replied | reacted` rather than `silence | pushback` over time (indicates the self-evolution loop is working).
