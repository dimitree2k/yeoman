---
name: health-bridge
domain: health
enabled: true
version: 3
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: whatsapp_bridge_connected
    target: default
    operator: "=="
    value: false
escalate_to_llm: false
safety:
  max_actions_per_hour: 10
  rollback: true
  cooldown_s: 300
  manual_reset_after_failures: true
---
# Bridge Health

## Context
The WhatsApp bridge connects to WhatsApp servers via Baileys.

## Actions
- action: alert
  target: owner
  message: "ACTION WhatsApp bridge is disconnected from WhatsApp. Overseer is restarting yeoman-bridge.service; if this repeats, relink WhatsApp via QR."
- action: restart_service
  target: yeoman-bridge.service

## Escalation
After 3 failed restarts → alert via cascading comms
