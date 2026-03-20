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

Review the semantic memory database for stale or low-quality entries.

### Your task

1. Use `query_memory` to sample recent entries across different topics.
2. Use `query_db` to count entries by age and salience:
   ```sql
   SELECT COUNT(*), AVG(salience) FROM memory2_nodes WHERE created_at < date('now', '-90 days')
   ```
3. If more than 100 entries are older than 90 days with salience below 0.3, send an alert recommending a prune run.
4. Report a one-paragraph summary of memory health in your final response.

Do not delete anything. Observe and report only.
