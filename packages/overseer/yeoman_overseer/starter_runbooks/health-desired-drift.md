---
name: health-desired-drift
domain: health
enabled: true
version: 1
auto_install: true
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: desired_state_drift
    target: default
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 1
  rollback: false
  cooldown_s: 28800
---
# Desired State Drift

## Context
A service the owner paused or stopped is running anyway (started by hand or by another tool). The Overseer never fights the owner: it only reports the difference, at most once every 8 h.

## Actions
- action: alert
  target: owner
  message: "ACTION {detail}"
