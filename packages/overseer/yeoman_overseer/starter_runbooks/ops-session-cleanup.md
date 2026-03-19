---
name: ops-session-cleanup
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "0 5 * * 0"
escalate_to_llm: false
safety:
  max_actions_per_hour: 5
  rollback: false
  cooldown_s: 3600
---
# Session Cleanup

## Context
Session state files accumulate in workspace/memory/session-state/. Remove stale ones weekly.

## Actions
1. Prune session-state WAL files older than 30 days
2. Compact session metadata snapshots older than 14 days
