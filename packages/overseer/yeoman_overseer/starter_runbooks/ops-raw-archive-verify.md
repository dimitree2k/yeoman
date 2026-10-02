---
name: ops-raw-archive-verify
domain: ops
enabled: true
version: 1
trigger:
  kind: cron
  expr: "30 4 * * *"
escalate_to_llm: false
safety:
  max_actions_per_hour: 2
  rollback: false
  cooldown_s: 3600
---
# Raw Archive Integrity

## Context
data/raw/ is the append-only raw message archive and the only source to rebuild memory from
(V1 spec §4.0, R7). Daily: seal finished months, verify checksums and line counts, and alert
on any drop without an owner purge AUDIT entry or on a degraded writer.

## Actions
- action: verify_raw_archive
  target: default
