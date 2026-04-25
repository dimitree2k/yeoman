# Consciousness Layer - Design

Status: Draft for implementation
Date: 2026-04-25
Owner: Tim
Supersedes: `docs/superpowers/specs/2026-04-25-consciousness-layer-design-superseded.md`

## 1. Goal

Give Yeoman a tightly controlled ability to occasionally speak without being
directly addressed, starting with owner DMs only and expanding to opt-in groups
after the safety and approval paths are proven.

The central product constraint is: fewer, higher-quality proactive messages.
The central engineering constraint is: every hard guarantee is enforced in code,
not in prompts.

## 2. Why The Previous Draft Was Superseded

The previous draft captured the right product direction, but it had three
implementation bugs against the current codebase:

- It put per-chat spontaneity policy in `yeoman_shared/config/schema.py`.
  Current per-chat policy lives in `packages/gateway/yeoman_gateway/policy/schema.py`.
- It routed outbound speakup previews through `ApprovalMiddleware`, but current
  `pipeline/approval.py` only intercepts inbound owner messages matching
  workflow approval codes. It does not preview outbound proposals.
- It assumed a passive inbound bus subscription exists. Current `MessageBus`
  has one inbound consumer, so burst observation needs an explicit event.

This replacement spec fixes those integration points before defining the
consciousness layer itself.

## 3. Non-Goals For V1

- No proactive group messages until owner-DM behavior has proven useful.
- No burst trigger until inbound observation is implemented and tested.
- No self-evolution loop until speakup logging has enough data.
- No cross-chat reasoning.
- No proactive media, reactions, edits, or voice.
- No changes to the reactive responder's tone.

## 4. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Runtime wiring | `packages/gateway/yeoman_gateway/app/bootstrap.py` |
| Pipeline composition | `packages/gateway/yeoman_gateway/core/orchestrator.py` |
| Pipeline execution | `packages/gateway/yeoman_gateway/core/pipeline.py` |
| Bus types | `packages/gateway/yeoman_gateway/bus/events.py` |
| Bus implementation | `packages/gateway/yeoman_gateway/bus/queue.py` |
| Policy schema | `packages/gateway/yeoman_gateway/policy/schema.py` |
| Policy engine | `packages/gateway/yeoman_gateway/policy/engine.py` |
| Workflow approval middleware | `packages/gateway/yeoman_gateway/pipeline/approval.py` |
| Config schema | `packages/shared/yeoman_shared/config/schema.py` |
| Memory service | `packages/gateway/yeoman_gateway/memory/service.py` |
| Inbound archive | `packages/gateway/yeoman_gateway/storage/inbound_archive.py` |

If docs disagree with source, trust these files and tests first.

## 5. Integration Fixes Required First

### 5.1 Policy Integration

Per-chat spontaneity belongs in the policy schema, not shared config.

Add to `packages/gateway/yeoman_gateway/policy/schema.py`:

```python
ActionType = Literal[
    "answer_open_question",
    "surface_memory",
    "correct_error",
    "share_opinion",
    "light_humor",
    "cold_joke",
    "observation",
    "contrarian",
]

PreviewMode = Literal["none", "owner_dm"]

class SpontaneityPolicy(PolicyModel):
    enabled: bool = False
    profile: str = "off"
    daily_cap: int | None = Field(default=None, alias="dailyCap", ge=0, le=10)
    allowed_actions: list[ActionType] | None = Field(default=None, alias="allowedActions")
    preview: PreviewMode | None = None
    quiet_hours_start: str | None = Field(default=None, alias="quietHoursStart")
    quiet_hours_end: str | None = Field(default=None, alias="quietHoursEnd")

class SpontaneityPolicyOverride(PolicyModel):
    enabled: bool | None = None
    profile: str | None = None
    daily_cap: int | None = Field(default=None, alias="dailyCap", ge=0, le=10)
    allowed_actions: list[ActionType] | None = Field(default=None, alias="allowedActions")
    preview: PreviewMode | None = None
    quiet_hours_start: str | None = Field(default=None, alias="quietHoursStart")
    quiet_hours_end: str | None = Field(default=None, alias="quietHoursEnd")
```

Then add:

- `ChatPolicy.spontaneity: SpontaneityPolicy = Field(default_factory=SpontaneityPolicy)`
- `ChatPolicyOverride.spontaneity: SpontaneityPolicyOverride | None = None`
- Compiled fields in `PolicyEngine` or a new resolver method that returns the
  resolved policy for one channel/chat.

Do not make owner DMs implicitly enabled in schema validation. Implicit
enablement belongs in service logic and must still respect the global kill
switch.

### 5.2 Global Config Integration

Global service controls belong in `packages/shared/yeoman_shared/config/schema.py`.

Add:

```python
class ConsciousnessConfig(BaseModel):
    enabled: bool = False
    owner_dm_default_enabled: bool = Field(default=False, alias="ownerDmDefaultEnabled")
    cron_hour: int = Field(default=19, alias="cronHour", ge=0, le=23)
    cron_minute: int = Field(default=0, alias="cronMinute", ge=0, le=59)
    agent_max_iterations: int = Field(default=3, alias="agentMaxIterations", ge=1, le=8)
    agent_max_input_tokens: int = Field(default=10000, alias="agentMaxInputTokens", ge=1000)
    max_speakup_length_chars: int = Field(default=500, alias="maxSpeakupLengthChars", ge=1)
    default_daily_cap: int = Field(default=1, alias="defaultDailyCap", ge=0, le=10)
    approval_timeout_seconds: int = Field(default=3600, alias="approvalTimeoutSeconds", ge=60)
    burst_enabled: bool = Field(default=False, alias="burstEnabled")
    burst_threshold_messages: int = Field(default=8, alias="burstThresholdMessages", ge=2)
    burst_window_minutes: int = Field(default=15, alias="burstWindowMinutes", ge=1)
```

Then add `Config.consciousness: ConsciousnessConfig`.

`consciousness.enabled = false` is a hard kill switch. No service task should
start and no speakup should be committed when the flag is false.

### 5.3 Inbound Observation Event

Do not add a second consumer to `MessageBus.inbound`; it would race the
orchestrator. Add an explicit observation event.

Add to `packages/gateway/yeoman_gateway/bus/events.py`:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class InboundObservedEvent:
    channel: str
    chat_id: str
    sender_id: str
    content: str
    timestamp: float
    message_id: str | None = None
    is_group: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

Then include it in `GatewayEvent`.

Update `MessageBus.publish_inbound()` to also publish an
`InboundObservedEvent` to the existing event queue. This makes inbound
observation best-effort and isolated from normal message processing. If the
event queue is full, observation can drop; the actual chat message must still
enter the inbound queue.

This event is required for the future burst trigger. Phase 1 does not use it
to speak.

### 5.4 Approval Routing

Do not reuse `WorkflowState` directly for speakups. Its `PendingApproval`
schema is job-specific (`next_job_id`, `previous_output`, `remaining_depth`).

Create a dedicated approval path:

| Component | Path | Responsibility |
|-----------|------|----------------|
| `SpeakupApprovalStore` | `consciousness/approval.py` | Persist pending speakup proposals |
| `SpeakupApprovalMiddleware` | `pipeline/speakup_approval.py` | Intercept owner approval/deny messages |
| `PendingSpeakupApproval` | `consciousness/approval.py` | Approval record for one proposal |

Suggested approval ids:

- Approve: `spk-approve-<proposal_id>`
- Deny: `spk-deny-<proposal_id>`

`PendingSpeakupApproval`:

```python
@dataclass(slots=True)
class PendingSpeakupApproval:
    proposal_id: str
    target_channel: str
    target_chat_id: str
    owner_channel: str
    owner_chat_id: str
    message: str
    action_type: str
    profile: str
    created_at: float
    expires_at: float
    context_snapshot: dict[str, object]
```

Flow:

1. `commit_speakup()` sees preview mode `owner_dm`.
2. It stores `PendingSpeakupApproval`.
3. It sends the owner a preview DM with approve/deny codes.
4. `SpeakupApprovalMiddleware` intercepts owner replies matching `spk-approve-*`
   or `spk-deny-*`.
5. On approve, it publishes the outbound message to the target chat.
6. On deny or timeout, no daily cap is consumed.

This keeps workflow approvals and speakup approvals separate. A later refactor
can unify both into a generic approval service if needed.

## 6. Consciousness Components

| Component | Path | Phase | Responsibility |
|-----------|------|-------|----------------|
| `ConsciousnessService` | `consciousness/service.py` | 1 | Cron loop, owner-DM tick orchestration |
| `ConsciousnessAgent` | `consciousness/agent.py` | 1 | Generates one proposal or silence |
| `ConsciousnessTools` | `consciousness/tools.py` | 1 | Tool boundary and hard rails |
| `SpeakupLog` | `consciousness/log.py` | 1 | SQLite append-only log and daily counters |
| `SpeakupApprovalStore` | `consciousness/approval.py` | 2 | Persist group preview proposals |
| `OutcomeEnricher` | `consciousness/outcomes.py` | 3 | Classify post-speakup outcome |
| `TasteDistiller` | `consciousness/taste.py` | 3 | Distill chat taste records |
| `BurstObserver` | `consciousness/burst.py` | 4 | Consume `InboundObservedEvent` for burst wakeups |

## 7. Tool Boundary

The agent can only affect the world through tools. Prompt instructions are not
trusted for hard guarantees.

V1 tools:

| Tool | Behavior | Hard rails |
|------|----------|------------|
| `read_eligible_chats()` | Returns only owner DM targets in Phase 1 | Global flag on; channel/chat eligible; quiet hours respected |
| `read_chat_window(chat_id, n=20)` | Reads recent messages from archive | Only eligible chat ids accepted; `n` capped |
| `search_memory(query, chat_id)` | Searches scoped memory | Scope clamped to the target chat/user only |
| `read_speakup_history(chat_id, n=20)` | Reads prior speakups | Only eligible chat ids accepted; `n` capped |
| `propose_speakup(chat_id, message, action_type, confidence)` | Creates proposal | Eligible chat; action allowed; length cap; confidence threshold |
| `commit_speakup(proposal_id)` | Sends or queues preview | Same-run proposal only; daily cap rechecked atomically |

Tool errors return structured data to the agent. They should not raise except
for actual infrastructure failure.

## 8. Speakup Log

SQLite store:

```text
~/.yeoman/data/consciousness/speakups.db
```

Tables:

```sql
CREATE TABLE speakups (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  committed_at REAL,
  channel TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  profile TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL,
  trigger TEXT NOT NULL,
  context_snapshot_json TEXT NOT NULL,
  outcome TEXT,
  outcome_classified_at REAL
);

CREATE INDEX idx_speakups_chat_day
  ON speakups(channel, chat_id, committed_at);
```

Status values:

- `proposed`
- `sent`
- `queued_for_approval`
- `denied`
- `expired`
- `send_failed`
- `rejected`
- `silent_pass`

Daily cap is computed from committed `sent` rows, not proposals.

Do not write every speakup into long-term memory in Phase 1. Keep raw speakups
in `SpeakupLog`; add distillation later to avoid polluting memory retrieval.

## 9. Phase 1 Behavior

Scope:

- Owner DMs only.
- `helpful` profile only.
- Cron trigger only.
- No approval flow.
- No groups.
- No burst trigger.
- No outcome enricher.
- No taste distiller.

Data flow:

```text
GatewayRuntime starts ConsciousnessService if config.consciousness.enabled
  -> service wakes at configured local time
  -> read owner DM targets from policy/config
  -> skip if quiet hours, cap hit, no recent useful signal, or owner disabled it
  -> agent reads bounded chat window + scoped memory + speakup history
  -> agent proposes one message or returns silence
  -> tools enforce rails
  -> commit publishes OutboundMessage(metadata={"spontaneous": true, ...})
  -> SpeakupLog records sent or silent pass
```

Phase 1 should bias to silence. A silent pass is a valid successful outcome.

## 10. Phase 2 Behavior

Scope:

- Explicit opt-in groups.
- `balanced` and `permissive` profiles.
- Owner-DM preview required for group messages by default.
- `SpeakupApprovalStore` and `SpeakupApprovalMiddleware` enabled.

Group opt-in policy example:

```json
{
  "channels": {
    "whatsapp": {
      "chats": {
        "12345-67890@g.us": {
          "comment": "Family group",
          "spontaneity": {
            "enabled": true,
            "profile": "balanced",
            "dailyCap": 1,
            "preview": "owner_dm"
          }
        }
      }
    }
  }
}
```

Default group preview mode should be `owner_dm`, even for permissive profiles.
`preview: "none"` for groups should be allowed only by explicit policy.

## 11. Phase 3 Behavior

Add the self-improvement loop only after enough Phase 1/2 data exists.

Outcome labels:

- `replied`
- `reacted`
- `silence`
- `topic_changed`
- `pushback`
- `mixed`

`OutcomeEnricher` runs delayed classifications. It should update
`SpeakupLog.outcome` and never send messages itself.

`TasteDistiller` writes compact chat-scope memory only after enough samples,
for example at least 10 speakups in one chat. Distilled records should describe
patterns, not raw messages.

## 12. Phase 4 Behavior

Add burst triggering only after `InboundObservedEvent` is shipped and observed.

Rules:

- Burst trigger is disabled by default.
- Burst can wake the service at most once between daily cron firings per chat.
- Burst state persists across restarts.
- Burst never bypasses eligibility, daily cap, preview, quiet hours, or profile rails.

Implementation:

- `BurstObserver` subscribes to `InboundObservedEvent`.
- It maintains a rolling count per `(channel, chat_id)`.
- When a threshold is crossed, it requests a consciousness tick for that chat.
- The request is ignored if Phase 4 config is disabled or the chat is not eligible.

## 13. Profiles

Bundled defaults live in code, with optional user overrides at:

```text
~/.yeoman/spontaneity_profiles.json
```

Profiles:

```json
{
  "helpful": {
    "description": "Answer open questions and surface relevant memory. No opinions, no jokes.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error"],
    "tone_hints": "warm, concise, helpful, never sarcastic",
    "daily_cap": 1,
    "preview": "none",
    "min_confidence": 0.75
  },
  "balanced": {
    "description": "Helpful plus light personality. Opinions only when context invites them.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error", "share_opinion", "light_humor"],
    "tone_hints": "friendly, concise, attentive to the room",
    "daily_cap": 1,
    "preview": "owner_dm",
    "min_confidence": 0.8
  },
  "permissive": {
    "description": "Full personality, still constrained by daily caps and preview policy.",
    "allowed_actions": ["answer_open_question", "surface_memory", "correct_error", "share_opinion", "light_humor", "cold_joke", "observation", "contrarian"],
    "tone_hints": "playful, mild contrarian streak ok, never mean",
    "daily_cap": 1,
    "preview": "owner_dm",
    "min_confidence": 0.85
  }
}
```

Unknown profile falls back to `helpful` and logs a warning.

## 14. Provider Routing

Add capability routes in `packages/gateway/yeoman_gateway/providers/registry.py`:

- `consciousness.agent`
- `consciousness.outcome`
- `consciousness.taste`

Do not route these by keyword fallback. They must be explicit model profiles,
matching the existing provider routing direction in this project.

## 15. Security And Privacy

- `consciousness.enabled = false` hard-disables all service behavior.
- Eligibility, opt-in, daily cap, preview, action allowlist, length cap,
  confidence threshold, and quiet hours are enforced in tool code.
- Chat content returned by tools must be framed as untrusted observed data.
- `search_memory` must clamp scope to the target chat/user. No cross-chat
  search in V1.
- Group speakups require explicit opt-in.
- Group preview defaults to owner-DM approval.
- Transcripts and logs contain chat context and must live under private runtime
  paths, not in the source repo.
- Do not log secrets or full provider prompts in source-controlled files.

## 16. Observability

Structured log events:

- `consciousness.tick`
- `consciousness.silent`
- `consciousness.tool_call`
- `consciousness.proposal`
- `consciousness.commit`
- `consciousness.approval_queued`
- `consciousness.approval_accepted`
- `consciousness.approval_denied`
- `consciousness.outcome`
- `consciousness.taste_distilled`

Runtime transcript path:

```text
~/.yeoman/var/logs/consciousness/YYYY-MM-DD.jsonl
```

Telemetry counters:

- `consciousness_passes_total{trigger}`
- `consciousness_speakups_total{action,channel,profile}`
- `consciousness_passes_silent_total{reason}`
- `consciousness_proposals_rejected_total{reason}`
- `consciousness_budget_exceeded_total`
- `consciousness_approvals_total{status}`
- `consciousness_outcome_classified_total{outcome}`

## 17. Testing Strategy

Phase 0 tests:

- Policy schema accepts `spontaneity` in defaults, channel default, and per-chat overrides.
- Policy schema rejects unknown fields and invalid daily caps.
- `MessageBus.publish_inbound()` still enqueues inbound messages while also
  emitting `InboundObservedEvent`.
- Event queue overflow cannot block inbound message delivery.
- `SpeakupApprovalStore` persists, reloads, expires, approves, and denies proposals.

Phase 1 tests:

- Owner-DM-only eligibility.
- Global kill switch prevents service start and commit.
- Daily cap cannot be exceeded.
- Quiet hours prevent commits.
- Tool calls reject non-eligible chat ids.
- Fake agent can propose; commit publishes exactly one outbound message.
- Fake agent can stay silent; silent pass is logged.
- Security classifier rejection prevents commit.

Phase 2 tests:

- Group opt-in required.
- Group preview queues owner approval instead of sending directly.
- Approval code sends the message to the target chat.
- Deny code prevents send.
- Expired approval does not send and does not consume daily cap.

Property test:

- Across randomized adversarial agent tool-call sequences, daily cap is never
  exceeded for any `(channel, chat_id, local_date)`.

## 18. Rollout Plan

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| 0 | Integration fixes: policy, config, inbound observation event, speakup approval primitives | Tests pass; no behavior enabled by default |
| 1 | Owner-DM-only helpful cron speakups | One week of daily runs; no cap violations; owner says messages are useful enough to continue |
| 2 | Explicit opt-in groups with owner-DM preview | Preview flow works end-to-end for at least one group |
| 3 | Outcome enricher and taste distiller | Distilled chat taste records improve proposal quality without polluting memory |
| 4 | Burst trigger via `InboundObservedEvent` | Burst fires only within configured limits over two weeks |

## 19. Open Questions

- Should owner DMs be default-enabled after global opt-in, or should every chat
  require explicit `spontaneity.enabled=true`?
- Should there be a chat command like `/spontaneity off` before group rollout?
- Should a critic model review proposed group messages before owner preview?
- How long should consciousness transcripts be retained?
- Should profile overrides live only in `~/.yeoman/spontaneity_profiles.json`,
  or should some be embedded in policy for portability?

## 20. Success Criteria

- Zero daily-cap violations in property tests and live logs.
- Zero messages to non-opt-in groups.
- Owner can inspect exactly why a speakup was sent or skipped.
- Owner reports Phase 1 owner-DM speakups are useful often enough to justify
  continuing.
- For groups, no message is sent without either explicit `preview: "none"` or
  owner approval.
