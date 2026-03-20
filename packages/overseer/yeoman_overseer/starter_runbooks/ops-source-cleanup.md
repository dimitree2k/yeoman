---
name: ops-source-cleanup
domain: ops
enabled: true
version: 1
origin: manual
trigger:
  kind: cron
  expr: "0 2 * * 0"
escalate_to_llm: true
llm_budget:
  max_tokens: 8000
  max_tool_calls: 15
  llm_profile: overseerDefault
safety:
  max_actions_per_hour: 5
  requires_tests: false
  shell_timeout_s: 60
---

## Purpose

Remove stale files from `~/.yeoman/var/cache/` and `~/.yeoman/var/media/` weekly
to prevent unbounded disk growth.

## Procedure

1. Check disk usage via `check_health`.
2. Use `shell` to list files older than 14 days in `~/.yeoman/var/cache/` and
   `~/.yeoman/var/media/incoming/`.
3. Use `shell` to delete them (`find ... -mtime +14 -delete`).
4. Re-check disk usage and send a summary alert.

## Safety

- `requires_tests: false` — no source code is touched.
- Only touches `~/.yeoman/var/` subdirectories (cache and media).
- Shell commands run inside bubblewrap — no network, no source code access.
