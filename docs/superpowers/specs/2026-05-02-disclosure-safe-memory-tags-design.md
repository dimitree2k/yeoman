# Disclosure-Safe Memory Tags - Design

Status: Approved for implementation
Date: 2026-05-02
Owner: Tim

## 1. Goal

Let Yeoman remember sensitive facts without speaking about them too freely.

The first implementation is intentionally not a topic graph. It adds lightweight
metadata to existing memory entries and a pre-generation disclosure gate that
controls how retrieved memories are rendered into prompt context.

The feature is complete even if no future graph system is ever built.

## 2. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Memory entry model | `packages/gateway/yeoman_gateway/memory/models.py` |
| SQLite memory schema and search | `packages/gateway/yeoman_gateway/memory/store.py` |
| Memory capture, manual writes, recall, prompt rendering | `packages/gateway/yeoman_gateway/memory/service.py` |
| Memory CLI | `packages/gateway/yeoman_gateway/cli/memory_commands.py` |
| Reactive responder memory injection | `packages/gateway/yeoman_gateway/adapters/responder_llm.py` |
| Prompt assembly | `packages/gateway/yeoman_gateway/agent/context.py` |
| `/forget` behavior and soft-delete precedent | `docs/superpowers/specs/2026-03-18-forget-command-design.md` |
| Runtime memory lifecycle docs | `docs/architecture/whatsapp-message-lifecycle.md` |

If docs disagree with source, trust these files and tests first.

## 3. Non-Goals

- No graph database.
- No topic-node or edge tables.
- No graph traversal during recall.
- No second LLM pass that reviews every generated reply for appropriateness.
- No automatic taboo detection as the primary control path.
- No deletion of sensitive memory just because it is not speakable.
- No changes to recent chat history, inbound archives, or reply-context storage.
- No autonomous policy/persona edits.

## 4. Core Idea

Memory recall can still find sensitive memories, but raw memory text is not
automatically prompt-safe.

The disclosure gate sits between retrieval and prompt rendering:

```text
memory recall candidates
  -> read metadata tags
  -> resolve disclosure decision
  -> render allowed memory and guardrails
  -> LLM generates reply from filtered context
```

This avoids the risky pattern of injecting raw private facts and then asking the
model to be careful. The model should not see taboo content unless the code has
decided that the current context allows it.

## 5. Metadata Schema

Store V1 metadata inside existing `MemoryEntry.meta_json` to avoid a schema
migration for the first slice.

```json
{
  "topics": ["funeral", "family"],
  "sensitivity": "taboo",
  "disclosure_mode": "never_initiate",
  "subjects": ["timo"],
  "notes": "owner-marked"
}
```

Allowed fields:

| Field | Type | Meaning |
|-------|------|---------|
| `topics` | list of strings | Human-readable topic tags for search/debug/admin use |
| `sensitivity` | string | Privacy/social sensitivity classification |
| `disclosure_mode` | string | How raw memory content may be rendered |
| `subjects` | list of strings | Optional people or entities the memory is about |
| `notes` | string | Optional owner-facing admin note |

Topic strings are plain slugs in V1. Do not introduce `topic_id` or graph IDs.
This keeps the first feature independent from any later graph architecture.

## 6. Sensitivity Levels

| Level | Meaning |
|-------|---------|
| `normal` | Ordinary memory; render normally when recalled |
| `sensitive` | Potentially delicate; render when the query is directly related |
| `private` | Personal/private; render raw content only in owner-safe contexts |
| `taboo` | Do not initiate or reveal; render raw content only when explicitly allowed |

Missing or invalid sensitivity defaults to `normal`.

## 7. Disclosure Modes

| Mode | Meaning |
|------|---------|
| `speakable` | Raw memory content can be rendered normally |
| `context_only` | Raw memory can be hidden or summarized as a non-specific guardrail |
| `owner_only` | Raw memory can be rendered only when the current sender is an owner or the context is owner-only |
| `never_initiate` | Raw memory is hidden unless the user explicitly raises the topic in an allowed context |

Missing or invalid mode is derived from sensitivity:

| Sensitivity | Default mode |
|-------------|--------------|
| `normal` | `speakable` |
| `sensitive` | `context_only` |
| `private` | `owner_only` |
| `taboo` | `never_initiate` |

## 8. Disclosure Decisions

The renderer makes one decision per memory hit:

| Decision | Prompt behavior |
|----------|-----------------|
| `render_raw` | Include normal memory line in `[Retrieved Memory]` |
| `render_guardrail` | Do not include content; include non-specific guidance |
| `hide` | Do not include the memory or any guardrail |

Initial rules:

| Sensitivity/mode | Owner context | Explicitly raised topic | Default decision |
|------------------|---------------|--------------------------|------------------|
| `normal` / `speakable` | any | any | `render_raw` |
| `sensitive` / `context_only` | any | yes | `render_raw` |
| `sensitive` / `context_only` | any | no | `render_guardrail` |
| `private` / `owner_only` | yes | any | `render_raw` |
| `private` / `owner_only` | no | any | `render_guardrail` |
| `taboo` / `never_initiate` | yes | yes | `render_raw` |
| `taboo` / `never_initiate` | no | yes | `render_guardrail` |
| `taboo` / `never_initiate` | any | no | `render_guardrail` |

`hide` is reserved for future policy escalation or malformed metadata. The first
implementation may use `render_guardrail` as the conservative fallback for
private/taboo hits so Yeoman can still be gentle without leaking facts.

## 9. Explicit Topic Raised

V1 uses a conservative text heuristic:

- Normalize query and topic tags to lowercase ASCII-ish tokens.
- A topic is explicitly raised when a topic slug or one of its whitespace
  variants appears in the current user message or quoted message text.
- Do not use graph neighbors, semantic expansion, or inferred aliases in V1.

Examples:

| Memory topic | Current message | Explicit? |
|--------------|-----------------|-----------|
| `funeral` | `Was war bei der Beerdigung los?` | no, unless the tag also includes `beerdigung` |
| `beerdigung` | `Was war bei der Beerdigung los?` | yes |
| `family` | `Why is Timo quiet?` | no |

This may under-detect. That is acceptable because under-detection hides raw
sensitive content; over-detection can leak it.

## 10. Guardrail Rendering

Guardrails must be non-specific. They must not include the memory content,
subject, exact topic, or inferred event.

Example prompt block:

```text
[Private Context Guardrails]
- A retrieved memory contains private or taboo context. Do not reveal, name, or
  initiate the private topic. Keep the reply non-specific, gentle, and socially
  careful.
```

If multiple guarded hits exist, collapse them into a small bounded list. Do not
add one verbose guardrail per hit.

## 11. Admin Controls

V1 owner controls should be CLI-first:

1. `yeoman memory add` accepts optional metadata:
   - `--topics funeral,family`
   - `--sensitivity taboo`
   - `--disclosure never_initiate`
   - `--subjects timo`
2. `yeoman memory search` displays sensitivity and topics in its table.

Editing tags on an existing memory is part of V1:

```text
yeoman memory tag <entry-id> --topics funeral,beerdigung --sensitivity taboo --disclosure never_initiate
```

The command updates only metadata fields in `meta_json`; it does not rewrite
memory content, scope, salience, confidence, or timestamps except `updated_at`.

## 12. Automatic Capture Behavior

Automatic capture stores disclosure metadata with a narrow deterministic rule.
The rule is intentionally not a general safety classifier.

Default classification:

- outside-world politics, war, public deaths, public illness, scandals,
  provocations, offensive jokes, finance, trading, work, AI, and news stay
  `normal` / `speakable`;
- group-member, user, known-contact, or close-relative death, funeral, severe or
  chronic illness, and self-harm become `taboo` / `never_initiate`;
- direct personal medication or dosage context may become
  `sensitive` / `context_only`.

This policy is also used by the deterministic retag command for old memories.
The cheap-model backfill may suggest metadata, but the narrow policy should
override broad model labels so outside-world topics are not accidentally
restricted.

## 13. Data Flow

Reactive reply path:

```text
LLMResponder.generate_reply()
  -> memory.build_retrieved_context(...)
  -> memory.recall_for_event(...)
  -> memory._render_hits(...)
  -> disclosure gate evaluates each MemoryHit
  -> raw memory lines plus private guardrails are returned
  -> ContextBuilder injects filtered memory block
```

Consciousness path:

```text
ConsciousnessAgent.run_once()
  -> ConsciousnessTools.search_memory(...)
  -> MemoryService.search(...)
  -> future extension can use same render helper
```

The first implementation must cover reactive reply memory rendering. If
consciousness still receives raw `MemoryHit` data through `search_memory`, it
must not regress reactive privacy. A follow-up can apply the same renderer to
consciousness prompts if needed.

## 14. Error Handling

- Malformed `meta_json` behaves as empty metadata.
- Unknown sensitivity defaults to `normal`.
- Unknown disclosure mode derives from sensitivity.
- Invalid topics or subjects are ignored after normalization.
- If all raw memory lines are filtered out but guardrails exist, render only
  `[Private Context Guardrails]`.
- If no raw memory and no guardrails remain, render no retrieved-memory block.

## 15. Testing

Add focused tests:

- Manual memory records metadata in `meta_json`.
- Search displays topics and sensitivity for manually tagged memories.
- Normal memory still renders in `[Retrieved Memory]`.
- Sensitive memory not explicitly raised renders a guardrail without raw text.
- Sensitive memory explicitly raised renders raw text.
- Private memory renders raw text for owner context and guardrail for non-owner context.
- Taboo memory does not render raw text when merely semantically recalled.
- Malformed metadata does not crash rendering.
- Existing memory recall tests still pass.

## 16. Rollout

1. Add metadata parsing and disclosure helpers with unit tests.
2. Add optional metadata args to `yeoman memory add`.
3. Update memory rendering to apply disclosure decisions.
4. Add responder regression tests showing taboo content is not injected raw.
5. Run targeted memory/responder tests.
6. Restart the gateway after Python runtime changes.

Success means Arvid can remember a sensitive fact but will not freely expose it
through retrieved-memory prompt injection.
