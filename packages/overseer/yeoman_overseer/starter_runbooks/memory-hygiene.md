---
name: memory-hygiene
domain: memory
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 20
  max_tokens: 10000
trigger:
  kind: cron
  expr: "0 3 * * *"
safety:
  max_actions_per_hour: 2
  cooldown_s: 3600
---

## Memory Hygiene

1. Read `yeoman memory source-accounting` for aggregate Knowledge/source-reference counts.
2. Sample curated statements through Gateway-authorized `query_memory` with an explicit owner-approved `chat_id` and bounded `limit`. No legacy memory fallback is supported after selection/retirement.
3. Inspect statement metadata with `yeoman memory statements list` and `show <statement-id>`. Content requires explicit owner diagnostics via `--content`.
4. Report stale references, pending jobs and quarantined/revoked statements for existing curation. No historical extraction is requested.
5. Never use `query_db` for history/identity stores, direct retention SQL, legacy DELETE or `prune_memory` on frozen targets. Stop on pause/unavailability.

Observe and report only; do not erase statements or contact participants from this job.
