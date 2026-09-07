---
name: health-gateway
domain: health
enabled: true
version: 2
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: gateway_healthy
    target: yeoman-gateway.service
    operator: "=="
    value: false
escalate_to_llm: false
safety:
  max_actions_per_hour: 10
  rollback: true
  cooldown_s: 300
  manual_reset_after_failures: true
---
# Gateway Health

## Context
The gateway is the core message processing service. If it goes down, no messages are processed.
Check the user systemd unit and require a Unix socket pong within five seconds.

## Actions
- action: restart_service
  target: yeoman-gateway.service

## Escalation
After 3 failed restarts, quarantine further attempts until recovery or manual reset.
