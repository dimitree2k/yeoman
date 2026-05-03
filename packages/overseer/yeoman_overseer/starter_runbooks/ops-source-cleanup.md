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

1. Check disk usage via `check_health` with check `disk_usage_above`, target `/`,
   and threshold `85` (percent). This is a pre-flight sanity check only.
2. Use `shell` to list files older than 14 days in `~/.yeoman/var/cache/` and
   `~/.yeoman/var/media/incoming/`:
   ```sh
   find ~/.yeoman/var/cache/ -type f -mtime +14 2>/dev/null | head -50
   find ~/.yeoman/var/media/incoming/ -type f -mtime +14 2>/dev/null | head -50
   ```
3. Use `shell` to delete them:
   ```sh
   find ~/.yeoman/var/cache/ -type f -mtime +14 -delete 2>/dev/null
   find ~/.yeoman/var/media/incoming/ -type f -mtime +14 -delete 2>/dev/null
   ```
4. Re-check disk usage with `check_health` (same parameters) and send a summary
   alert with files removed and current disk usage.

## Safety

- `requires_tests: false` — no source code is touched.
- Only touches `~/.yeoman/var/` subdirectories (cache and media).
- If shell commands fail (e.g. sandbox unavailable), report the error in the
  alert and skip deletion — do not retry or escalate.
