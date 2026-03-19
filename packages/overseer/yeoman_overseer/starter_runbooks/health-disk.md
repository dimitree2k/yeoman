---
name: health-disk
domain: health
enabled: true
version: 1
trigger:
  kind: poll
  interval_s: 3600
  condition:
    check: disk_usage_above
    target: /home
    operator: ">="
    value: 80
escalate_to_llm: false
safety:
  max_actions_per_hour: 2
  rollback: false
  cooldown_s: 3600
---
# Disk Health

## Context
Monitor disk usage to prevent full disk conditions.

## Actions
1. Check disk usage on /home partition
2. If above 80%: alert owner with usage details
3. If above 95%: emergency alert

## Escalation
Alert owner with disk usage percentage
