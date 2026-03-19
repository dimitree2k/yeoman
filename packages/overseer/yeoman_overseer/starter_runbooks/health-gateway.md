---
name: health-gateway
domain: health
enabled: true
version: 1
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: process_alive
    target: yeoman-gateway
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 10
  rollback: true
  cooldown_s: 300
---
# Gateway Health

## Context
The gateway is the core message processing service. If it goes down, no messages are processed.

## Actions
1. Check if process is alive via PID file + process table
2. If dead: restart via `systemctl --user restart yeoman-gateway`
3. If restart fails 3 times in 1 hour: stop retrying, alert owner
4. If alive but unresponsive on Unix socket within 5s: force restart

## Escalation
After 3 failed restarts → alert via cascading comms
