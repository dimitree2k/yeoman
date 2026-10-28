---
name: ops-log-digest
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "15 6 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 2
  rollback: false
  cooldown_s: 3600
---
# Log Digest

## Context
Write a content-free digest of the Gateway log and processing.db once a day for
the automatic log review: warning/error counts grouped by signature, failed
tool calls per tool, denied decisions, effect states and reply-sorter outcomes.
The scan resumes from a timestamp checkpoint and reads rotated `.gz` logs, so a
rotation between runs loses nothing. Digests and the incident index live in
`~/.yeoman/data/ops/log-review/` and outlast the logs. The headless log review
reads the newest digest at 06:30.

## Actions
- action: log_digest
  target: ~/.yeoman/var/logs
