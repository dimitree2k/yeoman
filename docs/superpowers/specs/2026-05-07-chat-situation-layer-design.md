# Chat Situation Layer - Design

Status: Draft for review
Date: 2026-05-07
Owner: Tim
Builds on:
- `docs/superpowers/specs/2026-04-27-group-dynamics-outcome-learning-design.md`
- `docs/superpowers/specs/2026-05-02-topic-graph-memory-architecture.md`

## 1. Purpose

Add a short-lived chat understanding layer that helps Yeoman interpret what is
happening in active group chats before it replies or speaks proactively.

The first practical failure case is the Finanzgruppe "Frank discussed too much"
moment on 2026-05-07. The existing `talkativeCooldown` only tracks consecutive
same-sender messages with lexical token overlap inside the responder path. That
missed a human-obvious situation: one participant gave repeated methodological
pushback inside the same semantic dispute, with short reactions and topic
wording changes between the long messages.

The broader goal is not only cooldown. Yeoman should maintain a compact runtime
snapshot of the room: active threads, topic shifts, reply relationships,
dialogue acts, sentiment, disagreement, banter, open questions, and whether the
bot has already said enough.

## 2. Research Context

This design borrows from established conversation and NLP work:

| Area | Relevance |
|------|-----------|
| Conversation disentanglement | Multi-party chats often contain interleaved threads. A runtime thread tracker should infer which prior utterance a new message belongs to. |
| Dialogue act classification | Messages need labels such as question, answer, backchannel, agreement, disagreement, repair, joke, and request. |
| Dialogue topic segmentation | Topic shifts should be detected semantically, not only through lexical overlap. |
| Argument mining and stance detection | Technical disputes need claim, support, attack, and stance signals. |
| Grounding and common ground | Good timing depends on what the room currently treats as settled, unclear, or contested. |
| Dialogue state tracking | A tracker-style event state is a useful architecture pattern, but Yeoman should keep its own policy and pipeline. |

Useful references:

- Elsner and Charniak, "Disentangling Chat"
- IBM Research, "Context-aware conversation thread detection in multi-party chat"
- Stolcke et al., "Dialogue Act Modeling for Automatic Tagging and Recognition of Conversational Speech"
- Lawrence and Reed, "Argument Mining: A Survey"
- Clark and Brennan, "Grounding in Communication"
- Rasa DialogueStateTracker and tracker stores, as an architecture pattern

Available libraries are mostly partial building blocks. Rasa is useful as a
tracker model, spaCy and Hugging Face support classification pipelines,
BERTopic supports dynamic topics, and smaller dialogue-act or argument-mining
packages exist. None should become a mandatory core dependency in V1. Yeoman
should start with a small internal layer and optionally plug in classifiers
later.

## 3. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Inbound pipeline composition | `packages/gateway/yeoman_gateway/core/orchestrator.py` |
| Typed inbound events and pipeline context | `packages/gateway/yeoman_gateway/core/pipeline.py`, `packages/gateway/yeoman_gateway/core/intents.py` |
| Implicit address and current conversation state | `packages/gateway/yeoman_gateway/implicit_addressing.py`, `packages/gateway/yeoman_gateway/pipeline/implicit_address.py` |
| Responder prompt context | `packages/gateway/yeoman_gateway/agent/context.py` |
| Reactive responder and current cooldown | `packages/gateway/yeoman_gateway/adapters/responder_llm.py` |
| Policy schema and resolved policy | `packages/gateway/yeoman_gateway/policy/schema.py`, `packages/gateway/yeoman_gateway/policy/engine.py` |
| Inbound archive and reply context | `packages/gateway/yeoman_gateway/storage/inbound_archive.py` |
| Consciousness agent and tools | `packages/gateway/yeoman_gateway/consciousness/agent.py`, `packages/gateway/yeoman_gateway/consciousness/tools.py` |
| Consciousness outcome learning | `packages/gateway/yeoman_gateway/consciousness/outcomes.py`, `packages/gateway/yeoman_gateway/consciousness/taste.py` |
| Semantic memory | `packages/gateway/yeoman_gateway/memory/` |

If docs disagree with source, trust source and tests first.

## 4. Non-Goals

- Do not replace the existing responder, policy engine, memory service, or
  consciousness service.
- Do not add an always-on multi-agent LLM loop for every group message.
- Do not optimize for engagement, reply count, or "keeping the chat alive."
- Do not build per-person persona switching.
- Do not store private raw transcripts in new long-term memories.
- Do not make the bot intervene in groups that are not policy-enabled.
- Do not let situation analysis bypass `whenToReply`, tool allowlists, preview,
  daily caps, security checks, or output filters.
- Do not build the future topic graph in this phase.

## 5. Relationship To Memory And Topic Graphs

The chat situation layer is deliberately not the primary memory graph.

A memory or topic graph is appropriate for durable knowledge: people, topics,
projects, events, stable preferences, recurring group dynamics, and approved
long-term lessons. A live chat situation is different. It is short-lived,
probabilistic, and easy to misread. A moment like "Frank is pushing too long on
this thread" should help Yeoman decide whether to answer, stay silent, or cool
the thread down right now. It should not automatically become a durable fact
like "Frank is annoying" or "Frank tends to derail discussions."

The boundary is:

| Layer | Lifetime | Purpose | Example |
|-------|----------|---------|---------|
| Chat situation | Minutes to days | Interpret the current room and active threads | "Same participant gave repeated methodological pushback in this thread." |
| Consciousness taste | Days to weeks | Learn tactical intervention patterns from outcomes | "Short acknowledgements beat long counterpush in technical Finanzgruppe disputes." |
| Memory graph | Weeks to years | Link durable topics, people, events, and approved lessons | "Finanzgruppe often discusses trading, AI, and health-news risk." |
| Persona evolution | Long-term | Distill stable behavior lessons into Yeoman's own style | "Prefer concise, evidence-backed corrections in noisy group chats." |

The chat situation layer may later emit distilled, owner-reviewable candidates
into consciousness taste, memory, or persona evolution. It must not directly
write every transient room judgment into long-term memory.

## 6. Design Principles

### Runtime State, Not Personality

The layer models what is happening in the chat right now. It does not rewrite
Yeoman's persona. It may influence timing, context, action selection, and
restraint.

### Small Signals First

Cheap deterministic signals should run on every inbound message. Expensive
semantic or LLM classification should run only when trigger conditions justify
it.

### One Shared Snapshot

Responder, consciousness, diagnostics, and future tools should read the same
compact `ChatSituationSnapshot`. They should not each invent separate social
state heuristics.

### Shadow Before Acting

New situation judgments should first be logged and inspected in shadow mode.
Only after evidence should they drive cooldowns, proactive speech, or responder
prompting.

### Explainable Enough

Every intervention caused by this layer should be traceable to compact reasons:
thread id, dominant speaker, signal scores, recent bot actions, and policy
decision.

## 7. Architecture Overview

Add a new package:

```text
packages/gateway/yeoman_gateway/chat_situation/
  __init__.py
  models.py
  signals.py
  threads.py
  assessors.py
  store.py
  service.py
```

Data flow:

```text
InboundObservedEvent
  -> ChatSituationService.observe(event)
  -> SignalExtractor
  -> ThreadTracker
  -> Optional SemanticAssessor
  -> ChatSituationStore
  -> latest ChatSituationSnapshot

Responder path
  -> read snapshot
  -> add compact prompt context
  -> optionally replace talkativeCooldown lexical streak logic

Consciousness path
  -> read snapshot
  -> include room state and open thread context
  -> prefer silence when bot already over-participated

Diagnostics
  -> show snapshot and traces for "why did/didn't Yeoman react?"
```

The service should subscribe to inbound observations rather than only
responder calls. That lets it understand ambient group activity even when the
bot is in `mention_only` mode.

## 8. Core Data Model

### Message Signal

```python
@dataclass(frozen=True, slots=True)
class MessageSignal:
    channel: str
    chat_id: str
    message_id: str
    sender_id: str | None
    created_at: float
    text_len: int
    reply_to_message_id: str | None
    reply_to_bot: bool
    mentioned_bot: bool
    from_bot: bool
    is_question: bool
    is_request: bool
    is_backchannel: bool
    is_reaction_like: bool
    has_media: bool
    lexical_tokens: frozenset[str]
```

### Thread State

```python
@dataclass(frozen=True, slots=True)
class ChatThreadState:
    thread_id: str
    channel: str
    chat_id: str
    started_at: float
    updated_at: float
    title: str
    participants: tuple[str, ...]
    message_ids: tuple[str, ...]
    representative_terms: tuple[str, ...]
    semantic_topic: str | None
    status: str
    dominant_speaker_id: str | None
    dominant_speaker_message_count: int
    bot_message_count: int
    open_question: bool
    disagreement_score: float
    heat_score: float
    banter_score: float
    overdiscussion_score: float
    confidence: float
```

Allowed `status` values:

- `active`
- `cooling`
- `resolved`
- `drifted`
- `stale`
- `unclear`

### Situation Snapshot

```python
@dataclass(frozen=True, slots=True)
class ChatSituationSnapshot:
    channel: str
    chat_id: str
    generated_at: float
    active_thread_ids: tuple[str, ...]
    room_energy: str
    room_tone: str
    dominant_thread_id: str | None
    dominant_speaker_id: str | None
    bot_recently_answered: bool
    bot_overparticipation_score: float
    open_question_count: int
    conflict_risk: str
    suggested_posture: str
    reasons: tuple[str, ...]
    confidence: float
```

Allowed `room_energy` values:

- `quiet`
- `normal`
- `active`
- `high`

Allowed `room_tone` values:

- `neutral`
- `playful`
- `technical`
- `heated`
- `supportive`
- `mixed`
- `unclear`

Allowed `suggested_posture` values:

- `answer`
- `short_answer`
- `react_only`
- `cooldown`
- `de_escalate`
- `summarize`
- `stay_silent`
- `unclear`

## 9. Signal Extraction

V1 deterministic signals:

- sender id and participant id
- reply-to links
- whether the message is from the bot
- direct bot interaction from existing `conversation_state`
- short reaction/backchannel detection
- question/request detection
- media presence
- timing gaps
- lexical tokens using the existing responder token helper or a shared helper
- per-sender recent message counts
- bot recent answer count inside the same chat

This layer should reuse or relocate existing helper logic where appropriate.
It should not duplicate brittle regexes in multiple files without tests.

## 10. Thread Tracking

V1 thread assignment should be hybrid:

1. If a message replies to another known message, attach it to that message's
   thread.
2. Else, compare with recent active threads using lexical overlap, timing, and
   participants.
3. If embeddings are already enabled and cheap enough, use embedding similarity
   as an optional signal.
4. If confidence remains low, start a new thread or mark the assignment
   `unclear`.

The design should not require an embedding service to ship V1. Semantic
upgrade can be enabled behind config later.

For the Frank case, the tracker should keep both long methodological messages
inside one semantic thread even if surface terms differ. A later semantic
assessor can label that thread as `andes_hantavirus_r0_methodology`.

## 11. Semantic Assessment

The optional semantic assessor consumes a small thread window and returns:

- semantic topic label
- dialogue acts per recent message
- stance relation: support, attack, neutral, repair, joke, backchannel
- sentiment and heat estimate
- whether one participant is dominating
- whether the bot has already answered enough
- whether the next best posture is silence, short acknowledgement, cooldown,
  de-escalation, summary, or answer

V1 should run this assessor only when one of these triggers is true:

- active group has at least N messages in M minutes
- same participant has at least 2 substantive messages in one active thread
- recent bot answer count is above a small threshold
- a direct bot reply follows a long dispute
- consciousness is considering a proactive speakup
- owner runs a diagnostic command

The assessor must output JSON matching a strict schema. Invalid or low
confidence output falls back to deterministic signals.

## 12. Responder Integration

The responder should receive a compact situation block through `ContextBuilder`
similar to the existing `[Conversation State]` and `[Recent Messages]` blocks.

Example:

```text
[Chat Situation]
room_tone: technical
room_energy: active
dominant_thread: andes_hantavirus_r0_methodology
dominant_speaker: 4917632625469
bot_recently_answered: true
suggested_posture: short_answer
reason: same participant gave repeated methodological pushback; avoid long repeat answer
```

The current `talkativeCooldown` should remain available, but its trigger should
eventually read `overdiscussion_score` from the active thread instead of only
same-sender lexical overlap.

Initial behavior should be shadow-only for the new score. After validation, a
chat policy flag can enable situation-based cooldown:

```json
{
  "talkativeCooldown": {
    "enabled": true,
    "mode": "semantic_thread",
    "streakThreshold": 4,
    "overdiscussionThreshold": 0.7
  }
}
```

`mode` defaults to the current lexical behavior until the new path is proven.

## 13. Consciousness Integration

Consciousness should read the same snapshot before building the proposal prompt.

It should use the snapshot to:

- avoid stale or unrelated thread anchors
- prefer silence when the bot recently over-participated
- identify an open question worth answering
- choose summary or de-escalation when a chat goes from active to quiet
- avoid echoing another person's joke
- avoid entering a high-heat thread with a weak line

This remains a proposal-or-silence helper. It does not bypass existing
consciousness eligibility, caps, preview, action allowlists, or security rails.

## 14. Storage

Use runtime SQLite under `~/.yeoman/data/chat_situation/`.

Initial tables:

```text
message_signals
  channel
  chat_id
  message_id
  sender_id
  created_at
  signal_json

threads
  thread_id
  channel
  chat_id
  started_at
  updated_at
  status
  snapshot_json

message_thread_links
  channel
  chat_id
  message_id
  thread_id
  confidence
  source

situation_snapshots
  channel
  chat_id
  generated_at
  snapshot_json
```

Retention should be short by default:

- message signals: 7 days
- thread snapshots: 14 days
- situation snapshots: 14 days

Do not store new permanent personal facts here. Durable learning still belongs
in memory, consciousness taste, or persona evolution after separate gates.

## 15. Security And Privacy

### Data Minimization

Store compact signals and summaries, not full raw transcript duplicates. Raw
messages already live in the inbound archive. The situation store should link
by message id and only keep short labels, scores, and reasons.

### Disclosure Boundaries

Situation snapshots must not relax memory disclosure rules. A sensitive or
private topic label should be treated as context for restraint, not as content
to reveal.

### Prompt Injection

Chat-derived situation labels are untrusted. They should be rendered as system
or developer-controlled metadata, not as instructions from users. The responder
must not follow quoted or summarized chat text as tool instructions.

### Policy Isolation

The layer may recommend a posture. It must not decide that Yeoman is allowed to
talk. Existing policy remains authoritative.

### Abuse And Targeting

Dominant-speaker and overdiscussion signals can feel personal. User-facing
messages should avoid exposing internal scoring. Prefer light phrasing such as
"kurze Pause auf dem Thread" over "you have an overdiscussion score of 0.82."

### Auditability

When a situation-based cooldown or proactive action fires, log:

- chat id and thread id
- trigger source
- compact reasons
- confidence
- policy mode
- selected posture

Do not log secrets, API keys, private runtime config, or full prompt payloads.

## 16. Performance

Normal chat must remain cheap and fast.

Required controls:

- deterministic extraction target: under 5 ms per message on normal hardware
- no blocking LLM calls in the inbound hot path
- async semantic assessment through a background task or bounded queue
- per-chat debounce so a burst of messages schedules one assessment, not one
  assessment per message
- fixed limits for messages per thread window
- SQLite writes batched or small enough not to delay channel dispatch
- stale threads compacted or expired during maintenance

Responder prompt context must be compact. Target under 1,000 characters for the
`[Chat Situation]` block in normal cases.

## 17. Cost Controls

V1 should work without extra model calls.

Model calls are allowed only for optional semantic assessment and must be
profile-gated:

| Path | Default cost policy |
|------|---------------------|
| every inbound message | deterministic only |
| active thread trigger | cheap classifier or no-op unless enabled |
| responder prompt | read latest cached snapshot only |
| consciousness prompt | read latest cached snapshot only |
| owner diagnostic | may run semantic assessor on demand |
| offline evaluation | may batch assess archived windows |

Cost budgets should be configurable:

```json
{
  "chatSituation": {
    "enabled": true,
    "semanticAssessment": {
      "enabled": false,
      "route": "chat.situation.assess",
      "minGapSeconds": 60,
      "maxPerChatPerHour": 12,
      "maxInputMessages": 24,
      "maxOutputTokens": 600
    }
  }
}
```

If the budget is exhausted, the layer should keep deterministic tracking active
and mark semantic assessment as skipped.

## 18. Diagnostics

Add read-only diagnostics before enabling behavior changes:

```text
yeoman chat-situation show --channel whatsapp --chat-id <chat>
yeoman chat-situation threads --channel whatsapp --chat-id <chat>
yeoman chat-situation trace --channel whatsapp --chat-id <chat> --since 2h
```

Diagnostics should answer:

- which threads are active
- which messages were assigned to each thread
- why a thread was considered same-topic or new-topic
- whether a cooldown would have fired in shadow mode
- whether the bot had recently over-answered
- which policy gate allowed or blocked any action

For live debugging, logs should include enough compact context to answer "why
didn't Yeoman react?" without opening private transcripts by default.

## 19. Rollout Plan

### Phase 0: Spec And Runtime Fixes

- Fix unrelated syntax/import blockers.
- Write this design.
- Do not change live behavior.

### Phase 1: Deterministic Shadow State

- Add models, store, signal extractor, and thread tracker.
- Subscribe to inbound observations.
- Persist short-lived snapshots.
- Add diagnostics.
- Add tests with the Frank/Andes fixture.
- No responder or consciousness behavior changes yet.

### Phase 2: Responder Read-Only Context

- Add compact `[Chat Situation]` prompt block.
- Keep it disabled by default or enabled only for a test chat.
- Log model behavior changes for review.

### Phase 3: Semantic Thread Assessment

- Add optional assessor with strict JSON schema.
- Gate by config and per-chat policy.
- Enforce budgets and debounce.
- Use shadow-mode scoring first.

### Phase 4: Situation-Based Cooldown

- Add `talkativeCooldown.mode = semantic_thread`.
- Enable first only for Finanzgruppe.
- Keep revert path to lexical mode.
- Verify live with diagnostics and tests.

### Phase 5: Consciousness Integration

- Feed snapshots into consciousness prompts.
- Use posture to reduce weak or stale speakups.
- Link outcomes back to situation snapshots.

## 20. Testing Strategy

Unit tests:

- deterministic signal extraction
- reply-to thread assignment
- lexical same-thread assignment
- reaction/backchannel messages do not reset useful thread state incorrectly
- overdiscussion score rises for repeated substantive same-thread pushback
- privacy-sensitive labels do not render raw private content
- invalid semantic assessor output falls back safely

Integration tests:

- Frank/Andes fixture reproduces missed lexical cooldown and shadow-detects
  semantic overdiscussion
- mention-only group still does not reply unless policy allows
- responder receives compact situation context when enabled
- consciousness reads snapshot without bypassing caps or action allowlists

Operational checks:

```bash
uv run pytest tests/gateway/test_chat_situation*.py
uv run pytest tests/gateway/test_responder_social_holdback.py
uv run pytest tests/gateway/test_consciousness_phase4.py
uv run ruff check packages/gateway/yeoman_gateway/chat_situation tests/gateway/test_chat_situation*.py
```

## 21. Open Questions

1. Should V1 add embeddings as optional infrastructure, or keep semantic
   grouping entirely LLM-assessor based until there is enough data?
2. Should situation snapshots be visible to the main reply model by default, or
   first only used for deterministic cooldown and diagnostics?
3. Should the first behavior change be "cooldown message" or "prefer silence /
   shorter answer"?
4. Should chat situation state be available to memory extraction as a hint, or
   kept fully separate to avoid long-term contamination?

## 22. Recommended First Implementation

Build Phase 1 only.

The first implementation should create the package, deterministic store,
thread tracker, diagnostics, and a replay test around the 2026-05-07
Frank/Andes sequence. It should not add LLM assessment, new model routes, or
live behavior changes.

That gives Yeoman an inspectable foundation and lets us compare "what humans
thought happened" against "what the runtime inferred" before spending tokens or
changing live group behavior.
