---
name: health-gateway-link
domain: health
enabled: true
version: 1
auto_install: true
trigger:
  kind: poll
  interval_s: 60
  condition:
    check: whatsapp_gateway_detached
    target: default
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 1
  rollback: false
  cooldown_s: 28800
---
# Gateway Link

## Context
The gateway lost its connection to the WhatsApp bridge and has not come back for 10 minutes, so messages are not flowing. The gateway retries the local link every 60 s on its own; this only tells the owner.

## Actions
- action: alert
  target: owner
  message: "ACTION {detail}"
