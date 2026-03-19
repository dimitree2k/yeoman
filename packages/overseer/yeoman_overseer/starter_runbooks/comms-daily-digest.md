---
name: comms-daily-digest
domain: comms
enabled: true
version: 1
trigger:
  kind: cron
  expr: "0 8 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 2
  rollback: false
  cooldown_s: 3600
---
# Daily Digest

## Context
Compile and send a daily status digest to the owner.

## Actions
1. Gather health status (gateway, bridge, disk)
2. Summarize audit log from last 24h
3. Report budget usage
4. List any quarantined runbooks
5. Send via cascading comms
