# Group Dynamics And Outcome Learning - Design

Status: Draft for review
Date: 2026-04-27
Owner: Tim
Builds on: `docs/superpowers/specs/2026-04-25-consciousness-layer-design.md`

## 1. Goal

Make Yeoman better at reading dynamic group chats before it speaks
proactively. The system should learn when a contribution is useful, when social
warmth helps, and when silence is the best choice.

The design preserves Yeoman's own personality. It does not overfit to each
person's preferred style. Adaptation happens at the group-state and outcome
level: what is happening in the room, what kind of intervention fits that
moment, and what happened after prior interventions.

The product target is a balanced mix of usefulness and social warmth. Yeoman
should not optimize for raw engagement or message volume.

## 2. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Consciousness cron and burst orchestration | `packages/gateway/yeoman_gateway/consciousness/service.py` |
| Proposal-or-silence planner wrapper | `packages/gateway/yeoman_gateway/consciousness/agent.py` |
| Hard speakup rails and eligible chat tools | `packages/gateway/yeoman_gateway/consciousness/tools.py` |
| Speakup log, counters, and outcome samples | `packages/gateway/yeoman_gateway/consciousness/log.py` |
| Post-speakup outcome classification | `packages/gateway/yeoman_gateway/consciousness/outcomes.py` |
| Chat taste distillation | `packages/gateway/yeoman_gateway/consciousness/taste.py` |
| Burst trigger from inbound observations | `packages/gateway/yeoman_gateway/consciousness/burst.py` |
| Inbound archive windows | `packages/gateway/yeoman_gateway/storage/inbound_archive.py` |
| Per-chat spontaneity policy | `packages/gateway/yeoman_gateway/policy/schema.py`, `packages/gateway/yeoman_gateway/policy/engine.py` |

If docs disagree with source, trust these files and tests first.

## 3. Non-Goals

- No individual-person taste engine in V1.
- No autonomous edits to persona, policy, prompts, or configuration.
- No objective that maximizes engagement, reply count, or time spent chatting.
- No proactive behavior outside existing `consciousness.enabled`,
  per-chat opt-in, daily caps, preview, quiet hours, allowed actions, and
  security output checks.
- No cross-group sharing of learned patterns unless a later design explicitly
  adds an owner-approved global learning path.
- No raw message copying into learned taste memories.

## 4. Principles

### Stable Personality

Yeoman keeps its own voice. Learning should affect timing, action selection,
and level of restraint, not mutate the bot into a different personality for
each participant.

### Group Health Over Engagement

Positive outcomes are useful follow-up, clarified plans, conflict reduction,
well-timed humor, or a helpful topic summary. A message that merely causes more
chat is not automatically successful.

### Hard Rails In Code

The planner can suggest a message, but code decides whether the chat is
eligible, whether the action type is allowed, whether the daily cap is
available, whether preview is required, and whether security permits the final
text.

### Learn Compact Patterns

The system stores aggregate patterns like "short logistics summaries work after
long planning bursts" instead of raw transcripts or brittle per-person rules.
Patterns need confidence and should be updated as more outcome samples arrive.

## 5. Design Overview

Add a group dynamics layer inside `yeoman_gateway/consciousness/`:

| Component | Path | Responsibility |
|-----------|------|----------------|
| `RoomStateClassifier` | `consciousness/group_state.py` | Classify the current group moment from recent messages |
| `InterventionPolicy` | `consciousness/intervention.py` | Select allowed action candidates or silence from room state and learned patterns |
| `GroupOutcomeEnricher` update | `consciousness/outcomes.py` | Expand outcomes from coarse labels into group-health labels |
| `GroupTasteDistiller` update | `consciousness/taste.py` | Distill outcome samples into group-level patterns with confidence |
| `ConsciousnessAgent` prompt update | `consciousness/agent.py` | Include room state and learned patterns in proposal-or-silence prompt |
| `ConsciousnessTools` extensions | `consciousness/tools.py` | Expose read-only room state and learned-pattern helpers to the agent |

This is not a second proactive system. It is an enhancement to the existing
proposal-or-silence flow.

## 6. Room State

`RoomStateClassifier` takes the recent chat window for one eligible group and
returns a structured snapshot:

```python
@dataclass(frozen=True, slots=True)
class RoomStateSnapshot:
    channel: str
    chat_id: str
    state: str
    energy: str
    confidence: float
    open_question: bool
    conflict_risk: str
    useful_action_candidates: tuple[str, ...]
    social_action_candidates: tuple[str, ...]
    reasons: tuple[str, ...]
```

Allowed `state` values:

- `banter`
- `planning`
- `question_open`
- `conflict`
- `celebration`
- `silence_after_burst`
- `technical_help`
- `emotional_support`
- `topic_drift`
- `logistics`
- `unclear`

Allowed `energy` values:

- `quiet`
- `normal`
- `active`
- `high`

Allowed `conflict_risk` values:

- `low`
- `medium`
- `high`

The classifier should prefer conservative output. If confidence is low or the
state is unclear, downstream policy should lean toward silence.

## 7. Intervention Policy

`InterventionPolicy` receives:

- resolved spontaneity profile and allowed actions
- `RoomStateSnapshot`
- daily usage and trigger type
- recent speakup history
- distilled group taste patterns

It returns either silence or one action candidate. It does not write messages
itself.

Allowed action candidates extend the existing policy action set:

- `stay_silent`
- `answer_open_question`
- `summarize`
- `ask_clarifying_question`
- `surface_memory`
- `share_opinion`
- `light_humor`
- `de_escalate`
- `suggest_next_step`

Implementation must add any new action values to the policy schema before they
can be used. New behaviors must not be smuggled through the generic
`observation` action. If a chat's resolved allowed actions do not include the
selected action, `ConsciousnessTools.propose_speakup()` must reject it with the
existing `action_not_allowed` path.

Initial policy rules:

| Room state | Preferred actions |
|------------|-------------------|
| `question_open` | `answer_open_question`, `ask_clarifying_question` |
| `planning`, `logistics` | `summarize`, `suggest_next_step` |
| `technical_help` | `answer_open_question`, `summarize` |
| `banter`, `celebration` | `light_humor`, `stay_silent` |
| `conflict` | `de_escalate`, `stay_silent` |
| `emotional_support` | `stay_silent`, `surface_memory` only when clearly helpful |
| `silence_after_burst` | `summarize`, `ask_clarifying_question`, `stay_silent` |
| `topic_drift`, `unclear` | `stay_silent` |

Use a balanced scoring model:

```text
score = usefulness_score + warmth_score - interruption_cost - recent_failure_penalty
```

The threshold for speaking must be high enough that silence is common. A group
message with `preview: "owner_dm"` still queues for approval before delivery.

## 8. Outcome Learning

Expand outcome labels from coarse chat response categories into group-health
categories:

- `helped`
- `clarified`
- `sparked_useful_reply`
- `positive_banter`
- `ignored`
- `topic_changed`
- `derailed`
- `pushback`
- `too_much`
- `bad_timing`
- `de_escalated`
- `mixed`

`OutcomeEnricher` should continue to wait before classification and inspect the
post-speakup archive window. It should include the room state, chosen action,
trigger, and preview status in the classifier payload so outcomes can be linked
to the context that caused the intervention.

For backward compatibility, existing labels map into the expanded set:

| Existing label | Expanded label |
|----------------|----------------|
| `replied` | `sparked_useful_reply` when useful, otherwise `mixed` |
| `reacted` | `positive_banter` when tone is positive, otherwise `mixed` |
| `silence` | `ignored` |
| `topic_changed` | `topic_changed` |
| `pushback` | `pushback` |
| `mixed` | `mixed` |

## 9. Taste Distillation

`TasteDistiller` should move from generic "proactive speakup taste pattern" to
room-state-aware group patterns:

```text
Group dynamics pattern:
- state: planning
- action: summarize
- outcome tendency: helped
- pattern: Short summaries after long logistics bursts usually help this group.
- confidence: 0.82
- evidence_count: 14
```

Patterns should be written as chat-scope memory records using the existing
`MemoryService.record_manual()` path. They must not copy raw messages. They
should include enough structure in the text to be retrieved by `search_memory`
for future room-state prompts.

The distiller should require enough samples per group and should prefer
updating or replacing prior group-dynamics patterns over accumulating many
near-duplicates.

## 10. Data Flow

Daily cron path:

```text
ConsciousnessService.tick_once()
  -> ConsciousnessAgent.run_once(trigger="cron")
  -> ConsciousnessTools.read_eligible_chats()
  -> read_chat_window()
  -> RoomStateClassifier.classify()
  -> read_speakup_history() + search_memory(group dynamics patterns)
  -> InterventionPolicy.select()
  -> planner proposes message or silence
  -> ConsciousnessTools.propose_speakup()
  -> ConsciousnessTools.commit_speakup()
  -> OutcomeEnricher labels delayed result
  -> TasteDistiller writes/updates group pattern after enough samples
```

Burst path:

```text
InboundObservedEvent
  -> BurstObserver threshold/debounce/eligibility
  -> ConsciousnessService.tick_once(trigger="burst", target_chat_id=...)
  -> same group dynamics path
```

The burst trigger must not bypass the room-state classifier. A high-activity
burst can still lead to silence.

## 11. Error Handling

- If room-state classification fails, record a silent pass with
  `reason="room_state_failed"`.
- If classification confidence is below threshold, prefer silence with
  `reason="room_state_low_confidence"`.
- If taste memory lookup fails, continue without learned patterns.
- If outcome classification fails, leave the row pending for a later pass.
- If distillation fails, keep existing patterns unchanged.
- Security and daily-cap failures keep their current hard behavior.

## 12. Testing

Add focused tests before implementation:

- `RoomStateClassifier` parses valid classifier output and rejects malformed
  labels.
- Low-confidence or `unclear` room state causes silence.
- `InterventionPolicy` prefers summary for planning/logistics and silence for
  unclear/topic-drift.
- Conflict state never chooses humor or contrarian actions.
- Group preview still queues owner approval before outbound delivery.
- Burst-triggered runs use the same classifier and intervention policy as cron.
- Outcome classifier stores expanded labels and preserves backward-compatible
  labels.
- Taste distiller writes aggregate patterns without raw message copies.
- Daily cap, quiet hours, allowed actions, and security checks still dominate
  learned preferences.

## 13. Rollout

1. Implement passive room-state classification and log snapshots only. No new
   speakups from the classifier yet.
2. Add intervention policy in shadow mode. Compare selected actions against the
   existing planner decisions.
3. Enable classifier-informed planning for owner-preview groups only.
4. Enable outcome labels and taste distillation.
5. After enough samples, allow learned group patterns to influence intervention
   scoring.

Success means fewer bad-timing messages, more useful summaries or clarifying
questions during active group moments, and positive social contributions that
fit Yeoman's existing personality.
