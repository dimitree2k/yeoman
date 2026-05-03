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

1. Query current memory stats via `query_db` on `memory/memory.db` to understand
   sector distribution and average salience.
2. Check how many entries would be pruned: active rows older than 60 days with
   salience below 0.3.
3. If the would-prune count is `0`, do not call `prune_memory`; send a summary
   alert that reports the stats and says nothing matched the prune criteria.
4. If the would-prune count is greater than `0`, use the `prune_memory` tool
   with `age_days=60` and `salience_below=0.3`.
5. Send a summary alert with rows deleted, snapshot path, and before/after counts.

## Safety

- `requires_tests: false` — no source code is touched.
- The `prune_memory` tool snapshots the database before any deletion.
- Do not delete entries with salience >= 0.3 regardless of age.
