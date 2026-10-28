---
name: ops-log-review
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "30 6 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 1
  rollback: false
  cooldown_s: 3600
---
# Log Review

## Context
Fifteen minutes after `ops-log-digest`, start `ops/log-review/run.sh`: headless
Claude Code reads the digest and fixes up to three issues as PRs. The
`~/.yeoman/data/ops/log-review/autonomy` file sets how far it goes (`pr-only` or
`auto-merge`). The run is started as a transient systemd unit because an
auto-merge deploy restarts the Overseer. The script sends its own Telegram
summary; the Overseer alerts only when the run cannot start.

## Actions
- action: log_review
  target: ~/Documents/yeoman/ops/log-review/run.sh
