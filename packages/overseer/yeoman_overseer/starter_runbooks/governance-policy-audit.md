---
name: governance-policy-audit
domain: governance
escalate_to_llm: true
llm_budget:
  llm_profile: overseerDefault
  max_tool_calls: 15
  max_tokens: 12000
trigger:
  kind: cron
  expr: "0 4 * * 0"
safety:
  max_actions_per_hour: 1
  cooldown_s: 86400
---

## Governance Policy Audit

Review the current policy configuration for anomalies or drift.

### Your task

1. Read `~/.yeoman/policy.example.json` to understand the expected schema.
2. Use `query_db` on the audit log to check for recent policy changes:
   ```sql
   SELECT runbook, action, result, ts FROM audit_entries WHERE domain = 'governance' ORDER BY ts DESC LIMIT 20
   ```
3. Check for newly detected chats by reading `~/.yeoman/data/seen_chats.json`.
4. If any chat IDs are present in seen_chats but absent from policy, flag them in an alert.
5. Summarize governance health in your final response.

Do not modify policy. Observe and report only.
