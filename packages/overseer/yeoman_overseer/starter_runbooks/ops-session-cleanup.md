---
name: ops-session-cleanup
domain: ops
enabled: false
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
The former workspace/memory/session-state/ location is absent and is not to be
recreated. Existing files in data/memory/session-state/ are owner-frozen legacy
copies. Do not write, move, or delete them; deletion requires a later owner
decision.

## Actions
No cleanup is authorized. This runbook stays disabled until an owner decides
what to do with the frozen files.
