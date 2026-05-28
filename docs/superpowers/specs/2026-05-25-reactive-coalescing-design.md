# Reactive Coalescing - Design

Status: Draft for review
Date: 2026-05-25
Owner: Tim
Builds on:
- `docs/superpowers/specs/2026-05-07-chat-situation-layer-design.md`
- `session-context/2026-05-24-omega-runtime-followup.md`

## 1. Purpose

Add a policy-gated runtime feature that lets Yeoman treat several near-simultaneous
bot-addressed group messages as one short mini-thread and answer once.

The first rollout target is the new Omega WhatsApp group persona. The group is
expected to behave like Finanzgruppe, but with more deliberate provocation and
spam attempts toward the bot. The desired behavior is not hard silence. If five
people address Yeoman at once, Yeoman should look at the whole pile as one
pipeline, answer the combined situation, and avoid five independent LLM calls.

Example:

```text
A: Omega du bist dumm
B: was sagst du dazu
C: ICD Diagnose bitte
D: bot ist lost
E: roast ihn
```

One response is better than five:

```text
Das ist kein Thread, das ist ein kollektiver F-Code-Speedrun. Einer von euch formuliert eine echte Frage, der Rest atmet kurz durch.
```

## 2. Goals

- Keep the feature generic and policy-gated so any WhatsApp group can opt in.
- Enable it initially only for Omega/private roast-style groups.
- Preserve the existing Finanzgruppe behavior unless its policy explicitly enables
  coalescing.
- Reduce reactive spam cost by batching simultaneous addressed messages into one
  responder call.
- Preserve message archive, memory capture, and ambient chat history.
- Keep the behavior explainable through metrics and logs.
- Make the model see the batch as one mini-thread, not as separate requests to
  answer line by line.

## 3. Non-Goals

- Do not build the full future chat-situation layer in this phase.
- Do not add semantic thread tracking, per-person taste modeling, or memory-graph
  writes.
- Do not change default behavior for chats that do not opt in.
- Do not coalesce owner/admin approval commands, persona-evolution approvals,
  speakup approvals, or other control flows.
- Do not suppress archive or memory observation for the original messages.
- Do not use coalescing to bypass policy, security, tool allowlists, or output
  safety.
- Do not make the bot reply more often. This feature is for fewer, better grouped
  replies.

## 4. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Inbound service loop | `packages/gateway/yeoman_gateway/app/bootstrap.py` |
| Message bus | `packages/gateway/yeoman_gateway/bus/queue.py` |
| Pipeline composition | `packages/gateway/yeoman_gateway/core/orchestrator.py` |
| Pipeline context | `packages/gateway/yeoman_gateway/core/pipeline.py` |
| Typed inbound/policy models | `packages/gateway/yeoman_gateway/core/models.py` |
| Policy schema and resolution | `packages/gateway/yeoman_gateway/policy/schema.py`, `packages/gateway/yeoman_gateway/policy/engine.py` |
| Policy adapter diagnostics | `packages/gateway/yeoman_gateway/adapters/policy_engine.py` |
| Implicit addressing | `packages/gateway/yeoman_gateway/pipeline/implicit_address.py`, `packages/gateway/yeoman_gateway/implicit_addressing.py` |
| Reply/ambient context | `packages/gateway/yeoman_gateway/pipeline/reply_context.py` |
| Responder middleware | `packages/gateway/yeoman_gateway/pipeline/responder.py` |
| Prompt context builder | `packages/gateway/yeoman_gateway/agent/context.py` |

Important current behavior:

- `OrchestratorService.run()` consumes inbound messages sequentially.
- `ReplyContextMiddleware` already builds `ambient_context_window`.
- `ImplicitBotAddressMiddleware` promotes strong implicit address signals in
  mention-only groups.
- `ResponderMiddleware` calls `ResponderPort.generate_reply()` once per inbound
  event that survives policy and security gates.

Because inbound processing is sequential, a naive middleware that sleeps for
three seconds would block later messages from entering the pipeline. The
coalescing implementation must store messages, halt the originals, and flush the
batch asynchronously by publishing one synthetic inbound event back to the bus.

## 5. Policy Surface

Add a generic chat policy block:

```json
{
  "reactiveCoalescing": {
    "enabled": true,
    "windowSeconds": 3.0,
    "maxMessages": 8,
    "maxCharsPerMessage": 280,
    "replyTo": "last",
    "bypassOwners": true
  }
}
```

Defaults:

```json
{
  "enabled": false,
  "windowSeconds": 3.0,
  "maxMessages": 8,
  "maxCharsPerMessage": 280,
  "replyTo": "last",
  "bypassOwners": true
}
```

Field meaning:

| Field | Meaning |
|-------|---------|
| `enabled` | Opt-in switch. Default false everywhere. |
| `windowSeconds` | How long to collect addressed messages before flushing. Suggested range: `0.5` to `10.0`. |
| `maxMessages` | Maximum addressed messages per batch. If reached early, flush immediately. |
| `maxCharsPerMessage` | Prompt-safe truncation for each batched line. |
| `replyTo` | Which WhatsApp message id receives the final reply. V1 supports `last`; `first` can be added cheaply if needed. |
| `bypassOwners` | Owner/admin messages skip coalescing and are handled immediately. |

Policy resolution should add these fields to `PolicyDecision` so downstream
middleware does not need to re-read policy.

## 6. Pipeline Placement

Add `ReactiveCoalescingMiddleware` after implicit addressing and before approval,
idea capture, access control, security, responder, and outbound stages.

Target order:

```text
Normalization
Deduplication
Archive
Contacts
ReplyContext
AdminCommand
Policy
ImplicitBotAddress
ReactiveCoalescing
SpeakupApproval
PersonaEvolutionApproval
Approval
IdeaCapture
AccessControl
NewChatNotify
NoReplyFilter
InputSecurity
Responder
Outbound
```

Why here:

- Policy has already decided whether the message should respond.
- Implicit addressing has already promoted strong implicit bot-address signals.
- Archive has already stored the original message.
- Reply/ambient context has already been attached where available.
- Control flows after this point can be bypassed for originals and handled by the
  synthetic event instead.

The middleware should only coalesce messages where:

- `event.channel == "whatsapp"`
- `event.is_group is True`
- `decision.accept_message is True`
- `decision.should_respond is True`
- `decision.reactive_coalescing_enabled is True`
- `event.raw_metadata["reactive_coalesced"]` is not true
- the event is not an owner/admin/control-flow message

## 7. Batch Lifecycle

Maintain an in-memory pending batch per `(channel, chat_id)`.

### On Eligible Original Message

1. Build a compact `CoalescedMessage` from the event:
   - `message_id`
   - `sender_id`
   - `sender_name` if present in metadata
   - `content` truncated to `maxCharsPerMessage`
   - `timestamp`
   - `mentioned_bot`
   - `reply_to_bot`
   - `reply_to_message_id`
2. Add it to the pending batch.
3. If no flush task exists, schedule one for `windowSeconds`.
4. If `maxMessages` is reached, flush immediately.
5. Add metric `reactive_coalesce_queued`.
6. Halt the original pipeline so no reply is generated for that single message.

### On Flush

1. Pop the pending batch.
2. If the batch is empty, do nothing.
3. If the batch has one message, still send it as a synthetic event when the
   feature is enabled. This keeps behavior consistent and gives the prompt the
   same coalescing instructions with only a small delay.
4. Build a synthetic `InboundEvent`:
   - `channel`, `chat_id`, `is_group` from the batch
   - `sender_id` from the last message sender
   - `content` from the last message content, or a compact combined text
   - `message_id` synthetic and unique, for example `coalesced:<last_id>`
   - `reply_to_message_id` set according to `replyTo`
   - `raw_metadata["reactive_coalesced"] = true`
   - `raw_metadata["reactive_coalesced_messages"] = [...]`
   - `raw_metadata["reactive_coalesced_count"] = n`
   - preserve useful ambient context from the last event where possible
5. Publish the synthetic event back into the bus.
6. Add metric `reactive_coalesce_flushed` with `count`.
7. Log a compact info line with channel, chat id, count, and reason.

The synthetic event must not be coalesced again.

## 8. Prompt Context

Update `ContextBuilder` so `reactive_coalesced_messages` becomes explicit prompt
context, not hidden metadata.

Prompt shape:

```text
Several people addressed you within a short time window. Treat these messages as
one mini-thread. Answer the combined situation once. Do not answer every line
individually. If most messages are bait, land one short social response and stop.

Coalesced addressed messages:
- [Alice] Omega du bist dumm
- [Bob] was sagst du dazu
- [Chris] ICD Diagnose bitte
```

The regular persona still controls tone. For Omega, the persona may choose a
short roast. For a professional chat, the same coalescing feature would produce
a calmer grouped answer because the persona and policy differ.

## 9. Interaction With Existing Systems

### Policy

Coalescing does not replace policy. Only messages that policy would already
answer are eligible. `whenToReply`, `whoCanTalk`, `blockedSenders`,
`allowedTools`, and owner status remain authoritative.

### Archive And Memory

Original messages still pass through archive before coalescing. They remain
available for reply context, memory notes, diagnostics, and future analysis.

Memory-notes capture for original coalesced messages should not be lost. If the
current pipeline only queues memory notes after the coalescing point, the
middleware should enqueue the same background capture intent for halted originals
when `decision.notes_enabled` is true, using the same security check pattern as
`NoReplyFilterMiddleware`.

### Security

Original messages are not individually sent to the responder, but the synthetic
combined event still goes through input security, responder, and output security.
The batch context must be truncated and treated as untrusted user content.

### Tools

Allowed tools come from the resolved policy for the synthetic event. Coalescing
does not expand tool access.

### Typing Indicator

The first originals should not start typing. Typing starts only for the synthetic
event, shortly before the single generated response.

### Voice

Voice behavior follows existing policy. For Omega, keep output text-first unless
the chat policy explicitly enables voice.

### Consciousness

Reactive coalescing is separate from burst/lull/cron consciousness. It should
not count as a proactive speakup, should not consume speakup daily cap, and
should not change burst/lull observation. Original messages still publish
`InboundObservedEvent` through the message bus as today.

## 10. Failure Modes And Fallbacks

| Failure | Behavior |
|---------|----------|
| Flush task crashes | Log exception, drop pending batch, emit `reactive_coalesce_flush_error`. |
| Synthetic publish fails | Log exception; do not retry indefinitely. |
| Batch grows beyond `maxMessages` | Flush immediately. |
| Message has no id | Use timestamp plus sender fallback in batch context; reply-to may be omitted. |
| Owner/admin message arrives | Bypass coalescing and handle immediately. |
| Gateway restarts with pending batch | Pending in-memory batch is lost. Original messages remain archived. |

No durable queue is needed in V1. Losing a pending 3-second batch on restart is
acceptable and much safer than adding persistence for transient spam control.

## 11. Diagnostics

Add metrics:

- `reactive_coalesce_queued`
- `reactive_coalesce_flushed`
- `reactive_coalesce_dropped_original`
- `reactive_coalesce_bypass`
- `reactive_coalesce_flush_error`

Add log lines:

```text
reactive_coalesce queued channel=whatsapp chat=... count=...
reactive_coalesce flushed channel=whatsapp chat=... count=5 reply_to=...
reactive_coalesce bypass channel=whatsapp chat=... reason=owner
```

Policy diagnostics should include the effective `reactiveCoalescing` block.

## 12. Testing Plan

Focused tests:

1. Policy schema accepts `reactiveCoalescing` defaults and chat overrides.
2. Policy resolution exposes coalescing fields on `PolicyDecision`.
3. Disabled policy preserves current behavior.
4. Five addressed group messages within the window produce one synthetic inbound
   event and no original responder calls.
5. Synthetic event bypasses coalescing and reaches the responder once.
6. Non-addressed ambient messages are not coalesced.
7. Owner messages bypass coalescing when `bypassOwners` is true.
8. `maxMessages` triggers early flush.
9. Context builder renders coalesced messages and one-answer guidance.
10. Original messages remain archived before coalescing.
11. Memory notes are preserved for halted originals when enabled.

Suggested test files:

- `tests/gateway/test_reactive_coalescing.py`
- `tests/gateway/test_policy_reactive_coalescing.py`
- `tests/gateway/test_context_windowing.py`

Run focused checks first:

```bash
uv run pytest tests/gateway/test_reactive_coalescing.py
uv run pytest tests/gateway/test_policy_reactive_coalescing.py
uv run pytest tests/gateway/test_context_windowing.py
uv run ruff check packages/gateway/yeoman_gateway packages/shared/yeoman_shared tests/gateway
```

Then run the broader gateway suite if time allows:

```bash
uv run pytest tests/gateway/
```

## 13. Rollout

1. Ship code with default `reactiveCoalescing.enabled = false`.
2. Add an Omega chat policy entry once the group id is known:

```json
{
  "comment": "Omega",
  "personaFile": "personas/omega.md",
  "modelProfile": "assistantDefault",
  "whoCanTalk": {
    "mode": "everyone"
  },
  "whenToReply": {
    "mode": "mention_only"
  },
  "allowedTools": {
    "mode": "allowlist",
    "tools": [
      "web_search",
      "web_fetch",
      "summarize_history"
    ],
    "deny": [
      "exec",
      "spawn"
    ]
  },
  "voice": {
    "output": {
      "maxSentences": 3,
      "maxChars": 500
    }
  },
  "talkativeCooldown": {
    "enabled": true,
    "streakThreshold": 3,
    "topicOverlapThreshold": 0.34,
    "cooldownSeconds": 900,
    "delaySeconds": 2.5,
    "useLlmMessage": false
  },
  "reactiveCoalescing": {
    "enabled": true,
    "windowSeconds": 3.0,
    "maxMessages": 8,
    "maxCharsPerMessage": 280,
    "replyTo": "last",
    "bypassOwners": true
  },
  "spontaneity": {
    "enabled": false,
    "preview": "owner_dm"
  }
}
```

3. Restart the Python gateway after code changes.
4. Verify effective policy for the Omega chat.
5. Run a live spam test with 3-5 addressed messages and confirm one outbound.
6. Inspect logs for `reactive_coalesce flushed`.

## 14. Open Choices

These defaults are chosen for V1 unless review changes them:

- `windowSeconds = 3.0`
- `maxMessages = 8`
- `replyTo = last`
- owner/admin messages bypass coalescing
- single eligible messages still go through the synthetic path after the short
  delay when the feature is enabled

