---
name: quality-response-sample
domain: quality
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 15
  max_tokens: 15000
trigger:
  kind: cron
  expr: "0 5 * * 0"
safety:
  max_actions_per_hour: 1
  cooldown_s: 86400
---

## Response Quality Sampling

Sample recent message exchanges and assess response quality.

### Your task

1. Use `query_db` to find the most active chat IDs from the past week:
   ```sql
   SELECT to_chat, COUNT(*) as msgs FROM outbound_log
   WHERE sent_at > datetime('now', '-7 days')
   GROUP BY to_chat ORDER BY msgs DESC LIMIT 3
   ```
2. Read the inbound archive for one of those chats (e.g., `~/.yeoman/data/inbound/whatsapp_<chat_id>.jsonl`), last 20 lines.
3. Assess: Are responses on-topic? Is the tone consistent with the persona? Any factual errors?
4. If quality appears degraded, send an alert with specific examples.
5. Write a brief quality summary in your final response.

Do not contact users. Observe and report only.
