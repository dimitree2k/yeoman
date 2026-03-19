---
name: ops-media-cleanup
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "0 4 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 5
  rollback: false
  cooldown_s: 3600
---
# Media Cleanup

## Context
Incoming media files accumulate. Remove files older than 7 days.

## Actions
1. Prune files in var/media/incoming/ older than 7 days
2. Prune files in var/media/outgoing/ older than 3 days
