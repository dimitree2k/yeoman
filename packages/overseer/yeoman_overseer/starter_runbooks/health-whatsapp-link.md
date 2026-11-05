---
name: health-whatsapp-link
domain: health
enabled: true
version: 1
trigger:
  kind: poll
  interval_s: 60
  condition:
    check: whatsapp_link_held
    target: default
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 1
  rollback: false
  cooldown_s: 28800
---
# WhatsApp Link Hold

## Context
The bridge stopped reconnecting on its own: either WhatsApp logged the device out
(needs a QR relink) or the daily reconnect budget is spent. Tell the owner, at most
once every 8 h. No restart: restarting cannot fix either case.

## Actions
- action: alert
  target: owner
  message: "ACTION {detail}"
