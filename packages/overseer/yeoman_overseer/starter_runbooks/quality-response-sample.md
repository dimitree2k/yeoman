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

1. Use the owner's configured persona/chat scope; do not discover conversations through legacy databases or JSONL.
2. Outside a selected agent turn, use `yeoman history read --chat <authorized-chat> --after-ms <window-start-ms> --limit 20` for aggregate coverage. Content needs the explicit `--content` flag and that same authorized scope. Inside a selected turn use native `history_read` only; subprocess acquisition is forbidden.
3. Assess the authorized current messages for relevance, persona consistency and supported factual claims. Respect edit/delete/purge withholding and derived-media labels.
4. Stop on paused, disabled or unavailable reads. Never substitute frozen files or direct SQL.
5. Report an aggregate quality summary. Use existing owner alerting only if a specific degradation warrants it; do not contact chat participants or copy private conversation content into unrelated alerts.
