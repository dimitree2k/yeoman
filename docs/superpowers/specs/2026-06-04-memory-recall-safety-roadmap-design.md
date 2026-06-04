# Memory Recall Safety Roadmap - Design

Status: Proposed implementation sequence
Date: 2026-06-04
Owner: Tim

## 1. Goal

Improve Yeoman memory recall without weakening chat privacy.

This spec covers three related candidates:

1. Multi-query recall with diversity quotas.
2. Read-only memory trace diagnostics.
3. Future graph overlay and graph-assisted recall.

The candidates are ordered deliberately. Multi-query recall can improve live
reply quality now. Trace diagnostics should follow immediately because they make
recall behavior explainable. Graph-assisted recall must stay future/shadow work
until disclosure metadata, trace output, and owner correction tools are strong
enough.

## 2. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| Memory models | `packages/gateway/yeoman_gateway/memory/models.py` |
| SQLite memory store, FTS, vector candidate scan | `packages/gateway/yeoman_gateway/memory/store.py` |
| Capture, recall, scoring, rendering | `packages/gateway/yeoman_gateway/memory/service.py` |
| LLM memory extraction contract | `packages/gateway/yeoman_gateway/memory/extractor.py` |
| Disclosure metadata and render gate | `packages/gateway/yeoman_gateway/memory/disclosure.py` |
| Memory CLI | `packages/gateway/yeoman_gateway/cli/memory_commands.py` |
| Existing disclosure design | `docs/superpowers/specs/2026-05-02-disclosure-safe-memory-tags-design.md` |
| Existing future graph design | `docs/superpowers/specs/2026-05-02-topic-graph-memory-architecture.md` |

Live evidence observed while writing this spec:

- Active memory DB path: `~/.yeoman/data/memory/memory.db`.
- Active rows: about 3,047 non-deleted memory nodes.
- Embeddings: about 3,155 vectors.
- Most rows are chat-scoped; the largest kind is `episodic/utterance`.
- Live recall is already bounded: 8 final results, 2,400 prompt chars, lexical
  limit 24, vector limit 24, vector candidate pool 256.

These numbers make bounded multi-query recall feasible on SQLite. They do not
justify moving to Postgres, pgvector, Qdrant, Neo4j, or a separate memory
service for this phase.

## 3. Safety Invariant

Candidate generation may become broader, but prompt rendering must remain
disclosure-gated.

```text
current message + reply context
  -> build one or more recall queries
  -> fetch lexical/vector candidates
  -> merge, rank, and diversify candidates
  -> apply disclosure gate per memory hit
  -> render only allowed raw memory plus non-specific guardrails
```

No feature in this roadmap may inject raw memory into a live reply before
`disclosure_decision()` has run.

## 4. Memory Extraction Boundary

A chat message is source evidence, not automatically one durable memory.

The durable memory layer should store distilled facts/events with provenance.
For example, a source message like:

```text
I married Natasha in Las Vegas last year.
```

may produce memories such as:

```text
semantic/relationship: The user is married to Natasha.
episodic/milestone: The user got married in Las Vegas in 2025.
```

It does not need to split every grammatical clause. It must split when disclosure,
subject, or retrieval usefulness differs.

New extractor rule:

```text
If one source message or group batch contains facts with different subjects,
different sensitivity, different disclosure behavior, or clearly different
retrieval use cases, output separate memory candidates. Keep each memory concise
and independently safe to disclose according to its metadata.
```

This is part of candidate 1 because multi-query recall will consider more
candidates. More candidates are safe only if mixed public/private facts are less
likely to live in the same row.

## 5. Candidate 1: Multi-Query Recall With Diversity Quotas

### Added Value

Current recall uses one normalized query built from the current message plus
quoted reply text. That can miss narrow facts when the user asks with vague or
multi-topic wording.

Multi-query recall improves:

- name/person recall;
- ticker, place, project, and recurring-topic recall;
- vague follow-up questions where quoted context carries the useful terms;
- top-N diversity so one popular topic does not fill all 8 result slots.

### Design

Keep `MemoryService.recall_for_event()` as the public entrypoint.

Add a small internal query builder:

```text
primary_query = current message + quoted reply text
narrow_queries = extracted names, tickers, contact labels, and short topic terms
max_queries = 3
```

Each query uses the existing lexical/vector paths. Results are merged by memory
entry ID. Existing score components remain:

- lexical score;
- vector score;
- salience score;
- recency score.

Add diversity after normal scoring:

1. Reserve up to 1 result slot for the strongest hit from each non-primary query.
2. Fill remaining slots by normal final score.
3. Never exceed `memory.recall.max_results`.
4. Never exceed `memory.recall.max_prompt_chars` after disclosure rendering.

This is intentionally simpler than BrainDB's full keyword-mediated architecture.
Yeoman should not add keyword-entity tables for this phase.

### Privacy Behavior

Multi-query recall may retrieve sensitive rows more often. That is acceptable only
because raw rendering still goes through the disclosure gate.

Additional requirements:

- Trace must mark which query produced each hit.
- Guarded or hidden hits must not consume all final visible memory slots if other
  safe hits are available.
- Debug output that shows guarded/hidden hit content must be CLI-only and
  operator-facing.

### Performance And Cost

Bound the feature:

- maximum 3 query variants per event;
- reuse the existing vector candidate pool;
- no extra storage tables;
- no external database;
- no graph expansion.

At current live scale, 3 query variants against SQLite are acceptable. If the DB
grows past roughly 50k active embedded rows, revisit vector indexing before
raising query count.

## 6. Candidate 2: Read-Only Memory Trace Diagnostics

### Added Value

Trace diagnostics answer why a memory was or was not available to the responder.
They are the first safety tool needed before any graph work.

Trace should answer:

- which query variants were used;
- which scopes were searched;
- which lexical/vector candidates were found;
- how scores were merged;
- which diversity quota selected or skipped a hit;
- which disclosure decision applied;
- which raw memory lines, guardrails, or hidden items reached prompt rendering.

### Design

Add an operator CLI command:

```text
yeoman memory trace --query "..." --channel whatsapp --chat-id <chat> --sender-id <sender>
```

The command must be read-only. It should call the same recall internals as live
reply generation and render a structured report.

The first version does not need graph output. It traces candidate 1 only.

### Privacy Behavior

Trace can expose private memory by design, so it must remain owner/operator CLI
only. Do not make it a normal chat tool. Do not expose it through group chat.

If trace output is later exposed through any remote surface, it must redact hidden
memory content by default and require an explicit owner-only flag to show raw
content.

## 7. Candidate 3: Future Graph Overlay And Graph-Assisted Recall

### Added Value

A graph overlay can help Yeoman understand durable relationships between topics,
people, events, projects, and group patterns.

Useful outcomes:

- browse related memories by topic/person/event;
- group scattered memories into a coherent owner-facing view;
- reduce duplicate topic clusters through merge/split tooling;
- support future one-hop recall expansion for owner-approved contexts.

### Why It Must Not Be First

Graph traversal can leak private context indirectly.

Example:

```text
funeral -> father -> Timo -> quiet lately
```

Even if the raw funeral memory is hidden, a careless graph expansion could reveal
that Timo's current behavior is connected to a private family event.

Graph work requires stronger metadata and correction tools than candidate 1:

- node sensitivity;
- edge sensitivity propagation;
- edge type allowlists;
- confidence and source on nodes and edges;
- manual override and merge/split commands;
- graph trace before live prompt use.

### Design

Keep the existing future graph spec as the architecture direction. This roadmap
adds the implementation gate:

1. Build candidate 2 trace diagnostics first.
2. Add graph tables and admin commands only after trace exists.
3. Import existing plain topic strings as suggestions, not authoritative graph
   nodes.
4. Run graph expansion in shadow mode.
5. Compare graph-expanded candidates against normal recall.
6. Enable one-hop graph-assisted recall only for owner-approved contexts.

Graph-assisted recall must default to:

- max 1 hop;
- small fixed added-memory cap;
- no `related_to` expansion into private contexts;
- disclosure propagation before prompt rendering;
- trace output for every added, blocked, or hidden item.

## 8. Implementation Order

Recommended order:

1. **Extractor split-by-boundary refinement.**
   Tighten `MemoryExtractorService` instructions and tests so mixed-subject or
   mixed-disclosure content becomes separate memory candidates.
2. **Multi-query recall with simple quotas.**
   Implement candidate 1 behind normal config defaults. Keep final result and
   prompt limits unchanged.
3. **Memory trace CLI.**
   Implement candidate 2 using the same trace data produced by candidate 1.
4. **Memory quality cleanup tools.**
   Use trace results to identify raw-ish `episodic/utterance` rows that should be
   split, retagged, or ignored.
5. **Graph overlay administration.**
   Only after trace and metadata quality improve.
6. **Graph shadow mode.**
   Collect evidence without changing live prompts.
7. **Owner-approved one-hop graph recall.**
   Enable only after safety review.

## 9. What To Combine

Combine these in one implementation plan:

- extractor split-by-boundary refinement;
- multi-query recall query builder;
- diversity quota;
- internal trace data model.

They touch the same recall and capture concepts and can be tested together.

Do not combine these into the first implementation:

- graph tables;
- graph edge classifiers;
- graph-assisted live prompt expansion;
- database migration away from SQLite;
- remote trace tools.

The first implementation should leave Yeoman with better recall and better trace
data, but no new persistence model.

## 10. Data And Metadata Requirements

Each extracted memory should continue storing:

- `source_message_id`;
- `channel`;
- `chat_id`;
- `sender_id` or `contact_id`;
- `sector`;
- `kind`;
- `salience`;
- `confidence`;
- `meta_json` disclosure metadata.

For improved privacy, `meta_json` should support these optional fields over time:

```json
{
  "topics": ["relationship", "travel"],
  "sensitivity": "normal",
  "disclosure_mode": "speakable",
  "subjects": ["user", "natasha"],
  "source_span": "single_message",
  "metadata_source": "extractor",
  "metadata_confidence": 0.8
}
```

`metadata_source=owner` or manual CLI tags should dominate extractor suggestions.

## 11. Testing Strategy

Focused tests should cover:

- extractor prompt contains the split-by-disclosure-boundary rule;
- a synthetic extractor response with multiple candidates persists multiple rows;
- heuristic fallback still stores one row, but its metadata remains disclosure-gated;
- multi-query recall preserves existing one-query behavior when no narrow query is
  available;
- narrow query hits can reserve slots without exceeding `max_results`;
- hidden/guarded hits do not leak raw text through rendering;
- trace output records query origin, score components, quota decision, and
  disclosure decision;
- graph code is not invoked by candidate 1 or candidate 2.

## 12. Success Criteria

Candidate 1 is successful when:

- vague or multi-topic messages recall more relevant facts in tests;
- existing memory recall tests still pass;
- rendered prompt memory remains within configured limits;
- sensitive memory remains hidden or guardrailed.

Candidate 2 is successful when:

- an operator can explain a memory hit or miss from one CLI command;
- trace data matches the live recall path;
- trace tooling performs no writes.

Candidate 3 is ready for a new implementation plan only when:

- trace diagnostics are in place;
- owner/admin metadata correction exists;
- graph expansion can run in shadow mode;
- every graph-expanded candidate has an explainable safety decision.

