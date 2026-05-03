# Topic Graph Memory Architecture - Future Spec

Status: Future consideration
Date: 2026-05-02
Owner: Tim

## 1. Purpose

This document captures the long-term topic graph idea separately from the
current disclosure-safe memory tags feature.

It is not part of the first implementation. It may be built later, partially
built later, or never built. The disclosure-safe memory tags feature must remain
useful without this graph.

## 2. Problem

Yeoman's current long-term memory is optimized for search and prompt recall. It
stores memory entries with scopes, sectors, kinds, salience, confidence, and
optional embeddings. That works for finding likely relevant notes, but it does
not model how topics, people, events, and projects relate over time.

A topic graph could support richer navigation:

- find the larger topic behind scattered memories
- group memories by event or project
- show why two memories are connected
- avoid duplicate topic clusters
- propagate privacy constraints across related memory
- let the owner correct topic structure explicitly

## 3. Relationship To Phase 1

The disclosure-safe memory tags feature is independent.

Phase 1 stores plain metadata such as:

```json
{
  "topics": ["funeral", "family"],
  "sensitivity": "taboo",
  "disclosure_mode": "never_initiate"
}
```

The graph may later read those strings as import hints, but Phase 1 must not
store graph IDs, graph edges, or depend on graph traversal.

## 4. Non-Goals For This Future Spec

- No immediate implementation plan.
- No migration requirement for Phase 1.
- No replacement of the existing SQLite memory store.
- No automatic rewrite of old memory content.
- No graph-heavy prompt expansion by default.
- No autonomous public disclosure of connected private facts.

## 5. Architecture Overview

The graph should be an overlay on top of existing `MemoryEntry` rows.

```text
memory2_nodes
  -> linked to graph nodes through graph_memory_links

graph_nodes
  -> topic, person, event, project, place, group_state

graph_edges
  -> related_to, about_person, part_of_event, same_as, supersedes, happened_in_chat

graph_policy
  -> sensitivity and disclosure propagation rules
```

Existing memory search remains the primary retrieval path. The graph adds
navigation, clustering, and carefully bounded expansion.

## 6. Node Types

| Node type | Meaning |
|-----------|---------|
| `topic` | A durable subject such as `finance`, `funeral`, `magic_cards` |
| `person` | A known person or contact identity |
| `event` | A concrete occurrence such as a trip, funeral, trade, meeting, party |
| `project` | A continuing effort such as Yeoman development or financial planning |
| `place` | A location relevant to memories |
| `group_state` | Learned patterns about a group dynamic or social context |

Node fields:

```text
id
type
slug
display_name
aliases_json
sensitivity
disclosure_mode
created_at
updated_at
merged_into_id
```

## 7. Edge Types

| Edge type | Meaning |
|-----------|---------|
| `related_to` | General weak relationship |
| `about_person` | Memory/topic/event concerns a person |
| `part_of_event` | Topic or memory belongs to one event |
| `same_as` | Alias or duplicate candidate |
| `supersedes` | Newer understanding replaces older topic/memory cluster |
| `happened_in_chat` | Event or topic is tied to one chat |
| `derived_from_memory` | Graph node came from a specific memory entry |

Edges need confidence and source:

```text
source = manual | classifier | migration | distiller
confidence = 0.0..1.0
```

Manual edges should dominate classifier edges.

## 8. Privacy Propagation

The graph makes privacy harder because connected nodes can leak sensitive facts
indirectly.

Example:

```text
funeral -> father -> Timo -> quiet lately
```

If traversal is careless, Yeoman might avoid saying "funeral" but still reveal
that Timo's quietness is linked to a private family event.

Propagation rules must be explicit:

| Edge type | Propagate sensitivity? |
|-----------|------------------------|
| `same_as` | yes |
| `part_of_event` | yes |
| `about_person` | yes, but only toward disclosure guardrails |
| `related_to` | no by default |
| `happened_in_chat` | no |
| `supersedes` | yes from newer to older for hiding obsolete/private details |

The default should under-expand and under-disclose.

## 9. Retrieval Model

Graph-assisted recall should be bounded:

```text
semantic/FTS recall finds candidate memories
  -> candidate memories map to graph nodes
  -> graph expands at most 1 hop
  -> expansion applies edge allowlist and score threshold
  -> disclosure gate filters raw memory content
  -> prompt receives compact allowed context plus guardrails
```

No V1 graph behavior should load an unbounded neighborhood. Expansion limits
should include:

- max hops: 1 initially
- max added memories: small fixed number
- allowed edge types per use case
- sensitivity propagation before prompt rendering
- trace output for every added or blocked item

## 10. Admin And Debug Tools

A topic graph is only maintainable if the owner can inspect and correct it.

Needed tools before graph-assisted recall becomes active:

```text
yeoman memory topics search <query>
yeoman memory topics show <topic>
yeoman memory topics merge <source> <target>
yeoman memory topics split <topic>
yeoman memory topics mark <topic> --sensitivity taboo --disclosure never_initiate
yeoman memory graph trace --query <query> --chat-id <chat>
```

Trace output should answer:

- which memory matched the original query
- which graph node was reached
- which edge expanded recall
- which sensitivity/disclosure rule applied
- which memories were hidden or converted into guardrails

## 11. Model Usage

Cheap models are acceptable for graph maintenance tasks:

- topic classification
- alias suggestions
- edge suggestions
- duplicate topic detection
- sensitivity suggestions

The main reply model should not be responsible for building or repairing the
graph during a normal reply. Graph maintenance should run asynchronously,
batchable, and auditable.

## 12. Rollout Shape If Built

1. Add graph tables and manual topic administration.
2. Import Phase 1 topic strings as topic-node suggestions.
3. Add offline classifier suggestions for topic links and duplicate aliases.
4. Add graph trace tools.
5. Enable read-only graph navigation in CLI.
6. Enable graph-assisted recall in shadow mode.
7. Enable one-hop graph-assisted recall for owner-approved contexts.

Do not enable graph-assisted recall before trace/debug tooling exists.

## 13. Risks

| Risk | Mitigation |
|------|------------|
| Duplicate topic clusters | alias and merge tools before live recall |
| Wrong edges | confidence, source, manual override, trace output |
| Privacy leakage by association | conservative propagation and disclosure gate |
| Context bloat | one-hop expansion and small memory caps |
| Debuggability loss | graph trace command before runtime use |
| Second memory system drift | graph remains overlay; memory entries remain source facts |

## 14. Success Criteria

If built, the graph is successful when:

- the owner can browse related memories by topic/event/person
- graph-assisted recall improves relevance without large prompt bloat
- private topic clusters stay private by propagation rules
- every graph-expanded prompt item has an explainable trace
- disabling graph-assisted recall leaves normal memory behavior intact
