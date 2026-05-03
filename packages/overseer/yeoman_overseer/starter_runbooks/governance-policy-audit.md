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

1. Read `~/.yeoman/policy.json` to understand the current policy configuration.
2. Check recent overseer audit entries by reading the latest dated JSONL audit
   log in `~/.yeoman/data/overseer/audit/`:
   ```sh
   latest=$(ls -1 ~/.yeoman/data/overseer/audit/*.jsonl 2>/dev/null | sort | tail -1)
   test -n "$latest" && tail -30 "$latest" || true
   ```
   If there is no dated audit file yet, report that no audit entries are
   available; do not alert as an error.
3. Check for newly detected chats by reading `~/.yeoman/data/seen_chats.json`.
4. If any chat IDs are present in seen_chats but absent from policy, flag them
   in an alert. If none are missing, report that the cross-reference is clean.
5. Summarize governance health in your final response.

Do not modify policy. Observe and report only.
