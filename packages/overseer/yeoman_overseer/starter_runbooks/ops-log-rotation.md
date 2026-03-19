---
name: ops-log-rotation
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "0 3 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 5
  rollback: false
  cooldown_s: 3600
---
# Log Rotation

## Context
Rotate gateway and bridge logs daily to prevent unbounded growth.

## Actions
1. Rotate gateway.log → gateway-YYYYMMDD.log
2. Rotate whatsapp-bridge.log → bridge-YYYYMMDD.log
3. Prune rotated logs older than 14 days
