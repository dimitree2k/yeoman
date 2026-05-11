# yeoman-overseer

Autonomous orchestration layer for yeoman. Runs as a separate service alongside the gateway, executing **runbooks** — declarative maintenance scripts that monitor, heal, and clean up your yeoman instance without human intervention.

## Quick Start

```bash
# 1. Sync dependencies (picks up anthropic SDK)
cd ~/Documents/yeoman && uv sync

# 2. Start the overseer
yeoman overseer start

# 3. Check it's running
yeoman overseer status

# 4. List loaded runbooks
yeoman overseer runbooks
```

The overseer copies starter runbooks into `~/.yeoman/data/overseer/runbooks/` on first run. Edit or add `.md` files there to customize.

## How It Works

The overseer runs a **tick loop** (default 1 second) that evaluates runbook triggers:

```
Tick → Evaluate triggers → Safety checks → Execute action → Audit log
                                ↓ (if escalate_to_llm)
                          Agent loop (Anthropic API)
                          → tool calls → budget tracking
```

**Deterministic runbooks** (no LLM): check a condition, send an alert or run a script. Cheap and fast.

**LLM-escalated runbooks**: spin up a Claude agent with scoped tools and a token budget. The agent reads files, queries databases, prunes memory, or patches code — all sandboxed.

### Safety Stack

Every execution passes through multiple gates before any action:

| Gate | Purpose |
|------|---------|
| Circuit breaker | Quarantines runbooks after repeated failures (3 strikes → quarantine, 3 quarantines → permanent disable) |
| Rate limiter | 30 actions/hour, 20 LLM calls/day (configurable) |
| Budget tracker | 500K tokens/day; non-health domains blocked at 80% consumed |
| Cooldown | Per-runbook cooldown period after execution |
| Lock manager | Prevents concurrent execution of the same runbook |
| Maintenance windows | Suppresses execution during declared windows |
| Bubblewrap sandbox | Network-isolated, read-only OS, masked secrets for shell/test tools |
| `requires_tests` gate | Routes file writes through a git worktree; merges only if tests pass |

## Runbooks

Runbooks are Markdown files with YAML frontmatter in `~/.yeoman/data/overseer/runbooks/`.

### Minimal example (deterministic)

```markdown
---
name: health-gateway
domain: health
trigger:
  kind: poll
  interval_s: 30
  condition:
    check: process_alive
    target: gateway.pid
    operator: "=="
    value: true
escalate_to_llm: false
safety:
  max_actions_per_hour: 10
  cooldown_s: 60
---

Alert if the gateway process is not alive.
```

### LLM-escalated example

```markdown
---
name: memory-hygiene
domain: memory
trigger:
  kind: cron
  expr: "0 3 * * *"
escalate_to_llm: true
llm_budget:
  max_tokens: 10000
  max_tool_calls: 20
  llm_profile: overseerDefault
safety:
  max_actions_per_hour: 2
  cooldown_s: 86400
  requires_tests: false
---

Review memory.db for stale, duplicate, or low-salience entries.
Prune anything older than 90 days with salience < 0.3.
Snapshot before any deletions.
```

### Trigger Kinds

| Kind | Fields | Description |
|------|--------|-------------|
| `poll` | `interval_s`, `condition` | Periodic check with operator comparison |
| `cron` | `expr` | Standard cron expression |
| `event` | `event_name` | Fires on named event |

### Domains

Domains control rate-limiting priority:

| Domain | Priority | Description |
|--------|----------|-------------|
| `health` | Critical (always allowed until 100% budget) | Process liveness, disk, connectivity |
| `ops` | Normal | Log rotation, media/session cleanup |
| `memory` | Normal | Memory DB pruning, dedup |
| `comms` | Normal | Digests, summaries |
| `governance` | Normal | Policy audits |
| `quality` | Normal | Response sampling, quality checks |

### Built-in Check Functions (poll triggers)

| Check | Target | Description |
|-------|--------|-------------|
| `process_alive` | PID file name | Checks if process is running |
| `file_age_exceeds` | File path | File older than threshold (e.g. `5m`, `1h`) |
| `disk_usage_above` | Mount point | Partition usage exceeds % threshold |

## Agent Tools

LLM-escalated runbooks get access to scoped tools:

### Read-only (Phase 2)

| Tool | Description |
|------|-------------|
| `read_file` | Read files under `~/.yeoman/` or `~/Documents/yeoman/` |
| `query_db` | SELECT-only SQLite queries |
| `query_memory` | Full-text search on memory.db |
| `check_health` | Run built-in health checks by name |
| `git_log` | Read git history (source repo or internal git) |
| `send_alert` | Send message via cascading comms (Telegram, SMTP) |

### Write (Phase 3)

| Tool | Description |
|------|-------------|
| `write_file` | Create/overwrite files (deny: `.env`, `secrets/`, `.git/`, `systemd/`, `runbooks/`) |
| `edit_file` | String replacement in existing files (same deny list) |
| `prune_memory` | Delete memory entries by age/salience/domain (snapshots first) |
| `run_tests` | Execute pytest in bubblewrap sandbox (no network) |
| `git_revert` | Revert a commit by SHA in internal git |
| `dry_run_runbook` | Validate a runbook file without executing |
| `shell` | Run shell command in bubblewrap (no network, isolated /tmp, masked secrets) |

## CLI Reference

```
yeoman overseer start [--foreground|-f]   Start the service
yeoman overseer stop                      Send SIGTERM
yeoman overseer status                    PID, heartbeat, budget snapshot
yeoman overseer runbooks                  List loaded runbooks
yeoman overseer install-units             Install systemd user units
```

## Systemd

For persistent operation (survives reboots, auto-restarts on crash):

```bash
yeoman overseer install-units
systemctl --user daemon-reload
systemctl --user enable --now yeoman-overseer
```

The `yeoman-overseer-alert.service` fires on overseer crash and sends a Telegram message to the owner.

Includes units for all three services:
- `yeoman-overseer.service` — overseer (512M memory cap, 80% CPU quota)
- `yeoman-gateway.service` — gateway
- `yeoman-bridge.service` — WhatsApp bridge (256M memory cap)

## File Layout

```
~/.yeoman/
├── data/overseer/
│   ├── runbooks/           Active runbook .md files
│   ├── audit/
│   │   ├── YYYY-MM-DD.jsonl    Daily audit entries
│   │   └── tombstones.jsonl    Policy exclusion records
│   └── state.json          Persisted state (heartbeat, budget, locks, circuit breakers)
├── var/
│   ├── run/overseer.pid    Process ID
│   ├── run/overseer.sock   Unix domain socket (JSON protocol)
│   └── logs/overseer.log   Service log
```

## Configuration

The overseer reads model profiles from `~/.yeoman/config.json`:

```json
{
  "models": {
    "profiles": {
      "overseerDefault": {
        "kind": "chat",
        "model": "claude-haiku-4-5-20251001",
        "provider": "anthropic",
        "maxTokens": 4096,
        "temperature": 0.3,
        "timeoutMs": 30000
      }
    }
  }
}
```

Runbooks reference profiles by name via `llm_budget.llm_profile`. Fallback if profile missing: `claude-haiku-4-5-20251001`.

Requires `ANTHROPIC_API_KEY` in `~/.yeoman/.env` for LLM-escalated runbooks.

## Break Glass

```bash
# Force kill
cat ~/.yeoman/var/run/overseer.pid | xargs kill -9
rm ~/.yeoman/var/run/overseer.pid

# Reset daily budget
python3 -c "
import json; p='$HOME/.yeoman/data/overseer/state.json'
s=json.load(open(p)); s['budget'].update(tokens_daily=0, llm_daily=0, budget_reset_date='')
json.dump(s, open(p,'w'), indent=2); print('budget reset')
"

# Check audit trail
tail -20 ~/.yeoman/data/overseer/audit/$(date -u +%Y-%m-%d).jsonl

# Tail logs
tail -f ~/.yeoman/var/logs/overseer.log
```

## Starter Runbooks

Shipped with the package, copied to `~/.yeoman/data/overseer/runbooks/` on first run:

| Runbook | Domain | Trigger | LLM |
|---------|--------|---------|-----|
| `health-gateway` | health | poll 30s | no |
| `health-bridge` | health | poll 30s | no |
| `health-disk` | health | poll 1h | no |
| `ops-session-cleanup` | ops | cron Sun 5am | no |
| `ops-media-cleanup` | ops | cron daily 4am | no |
| `ops-log-rotation` | ops | cron daily 3am | no |
| `ops-stale-agent-session-cleanup` | ops | cron daily 4am | no |
| `ops-source-cleanup` | ops | cron Sun 2am | yes (8K tokens) |
| `ops-memory-prune` | ops | cron Sun 3am | yes (8K tokens) |
| `memory-hygiene` | memory | cron daily 3am | yes (10K tokens) |
| `comms-daily-digest` | comms | cron daily 8am | no |
| `governance-policy-audit` | governance | cron Sun 4am | yes (12K tokens) |
| `quality-response-sample` | quality | cron Sun 5am | yes (15K tokens) |
