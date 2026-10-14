---
name: ops-agent-cli-update
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "30 4 * * 1,4"
escalate_to_llm: false
safety:
  max_actions_per_hour: 2
  rollback: false
  cooldown_s: 3600
---
# Agent CLI Update

## Context
Claude Code and Codex are installed with their native installers in
~/.local/bin (not Homebrew). Update both twice a week with their own `update`
commands, after the 04:00 stale session cleanup. The native installers switch
a symlink to the new version, so running sessions keep working. The audit log
records the versions; the owner is alerted only when an update fails.

## Actions
- action: update_agent_clis
  target: claude,codex
