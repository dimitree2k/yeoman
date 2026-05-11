---
name: ops-stale-agent-session-cleanup
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
# Stale Agent Session Cleanup

## Context
Interactive Codex and Claude sessions can survive as old mosh trees after the
operator disconnects. Clean them up daily, but preserve anything started in the
last hour as the current-session guard.

## Actions
- action: cleanup_stale_agent_sessions
  target: mosh-agent-sessions
  min_age_seconds: "3600"
