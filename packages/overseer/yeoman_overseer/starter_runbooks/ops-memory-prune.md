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

Maintain curated Knowledge through its existing owner-authorized maintenance and curation contracts.

## Procedure

1. Read `yeoman memory source-accounting` for aggregate Knowledge and source-reference statistics. This performs no extraction and opens no retired legacy history file.
2. Inspect current statement metadata using `yeoman memory statements list`. Use `show <statement-id> --content` only for explicit owner diagnostics.
3. Report expired, quarantined, revoked and pending-job counts. Age or low salience alone does not authorize deleting curated statements.
4. Apply only an already-authorized specific statement erasure through `yeoman memory statements erase <statement-id>`; otherwise report candidates for owner curation.
5. Never call `prune_memory` on legacy history/identity stores or run direct DELETE, retention SQL, legacy backfill or re-extraction.
6. Stop on paused/unavailable reads. Keep source revocation and statement curation separate; frozen identity/history files remain untouched.
