---
name: ops-memory-prune
domain: memory
enabled: true
version: 1
origin: manual
trigger:
  kind: cron
  expr: "0 3 * * 0"
escalate_to_llm: true
llm_budget:
  max_tokens: 8000
  max_tool_calls: 10
  llm_profile: overseerDefault
safety:
  max_actions_per_hour: 2
  requires_tests: false
---

## Purpose

Prune stale low-salience memory entries weekly to keep `memory.db` compact and
retrieval performance high.

## Procedure

1. Query current memory stats via `query_memory` to understand volume and domain
   distribution.
2. Prune entries older than 60 days with salience below 0.3.
3. Send a summary alert with rows deleted and snapshot path.

## Safety

- `requires_tests: false` — no source code is touched.
- A snapshot is taken before any deletion. Recovery is always possible.
- Do not delete entries with salience > 0.5 regardless of age.
