---
name: health-bridge
domain: health
enabled: true
version: 1
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: systemd_active
    target: yeoman-bridge.service
    operator: "=="
    value: false
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
- action: restart_service
  target: yeoman-bridge.service

## Escalation
After 3 failed restarts → alert via cascading comms
