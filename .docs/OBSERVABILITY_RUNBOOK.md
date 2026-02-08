# nanobot Observability and Debug Runbook

Use this runbook to debug configuration, policy behavior, channel connectivity, and runtime load.

## 1. Fast health checks

```bash
nanobot status
nanobot channels status
nanobot cron list
```

What to look for:
- Missing config/policy/workspace files
- Expected channel enabled/disabled state
- Scheduled jobs and next run times

## 2. Policy decision debugging (why no reply?)

Print active policy path:

```bash
nanobot policy path
```

Explain one concrete message decision:

```bash
nanobot policy explain \
  --channel whatsapp \
  --chat '491786127564-1611913127@g.us' \
  --sender '491786127564' \
  --group
```

Add flags as needed:
- `--mentioned`
- `--reply-to-bot`

Check these fields in output:
- `decision.acceptMessage`
- `decision.shouldRespond`
- `decision.reason`
- `decision.allowedTools`
- `decision.personaFile`

## 3. Live runtime logs

Run gateway with verbose logs and save to file:

```bash
LOGURU_LEVEL=DEBUG nanobot gateway --verbose 2>&1 | tee ~/.nanobot/gateway.log
```

For WhatsApp, keep bridge logs open in a second terminal:

```bash
nanobot channels login
```

## 4. Inspect persisted state on disk

Core files:
- `~/.nanobot/config.json`
- `~/.nanobot/policy.json`
- `~/.nanobot/cron/jobs.json`
- `~/.nanobot/sessions/*.jsonl`
- `~/.nanobot/workspace/HEARTBEAT.md`
- `~/.nanobot/workspace/memory/MEMORY.md`

Useful quick checks:

```bash
ls -lah ~/.nanobot
ls -lah ~/.nanobot/sessions | tail -n 20
tail -n 120 ~/.nanobot/gateway.log
```

## 5. Host load and process checks

CPU and memory:

```bash
top
# or
htop
```

nanobot process footprint:

```bash
ps -o pid,pcpu,pmem,rss,etime,cmd -p "$(pgrep -f 'nanobot gateway')"
```

Bridge process footprint (if WhatsApp is used):

```bash
ps -o pid,pcpu,pmem,rss,etime,cmd -p "$(pgrep -f 'node.*bridge')"
```

## 6. Known warning: legacy allowFrom fields

If you see warnings about removed `channels.*.allowFrom`, inspect migration first:

```bash
nanobot policy migrate-allowfrom --dry-run
```

Apply migration (auto-backup):

```bash
nanobot policy migrate-allowfrom
```

## 7. Session-level debugging workflow

1. Run fast checks (`status`, `channels status`, `cron list`).
2. Start live logs (`gateway --verbose` with `tee`).
3. Reproduce one message.
4. Run `policy explain` for that exact channel/chat/sender shape.
5. Inspect relevant `~/.nanobot/sessions/*.jsonl` entries.
6. Capture findings under "Change Log" below.

## 8. Change Log (keep evolving this runbook)

Append entries in this format:

```text
YYYY-MM-DD - issue summary
- Symptoms:
- Root cause:
- Signals/commands that confirmed it:
- Fix:
- Follow-up guardrail:
```
