---
name: health-bridge
domain: health
enabled: true
version: 4
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: whatsapp_bridge_reachable
    target: default
    operator: "=="
    value: false
escalate_to_llm: false
safety:
  max_actions_per_hour: 3
  rollback: true
  cooldown_s: 300
  manual_reset_after_failures: true
---
# Bridge Health

## Context
The WhatsApp bridge connects to WhatsApp servers via Baileys and owns every WhatsApp
reconnect, within a persisted budget (5 per hour, 15 per day, then one try every 8 h;
none at all after a logout). This runbook restarts the bridge only when the process
does not answer. It never restarts it for being disconnected from WhatsApp, and it
skips a bridge the owner paused (`yeoman overseer pause bridge`).

## Actions
- action: alert
  target: owner
  message: "ACTION The WhatsApp bridge process is not answering. Overseer is restarting yeoman-bridge.service."
- action: restart_service
  target: yeoman-bridge.service

## Escalation
After 3 failed restarts → alert via cascading comms
