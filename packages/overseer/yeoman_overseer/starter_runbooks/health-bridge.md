---
name: health-bridge
domain: health
enabled: true
version: 1
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: process_alive
    target: yeoman-bridge
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 10
  rollback: true
  cooldown_s: 300
---
# Bridge Health

## Context
The WhatsApp bridge connects to WhatsApp servers via Baileys.

## Actions
1. Check if bridge process is alive via PID file
2. If dead: restart via `systemctl --user restart yeoman-bridge`
3. If restart fails 3 times: alert owner

## Escalation
After 3 failed restarts → alert via cascading comms
