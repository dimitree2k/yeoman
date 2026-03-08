---
name: browser
description: "Real browser control via pinchtab — navigate, read, and interact with JS-heavy or authenticated webpages. Use for paywalled content, login-required pages, monitoring pages for changes, form automation, or extracting text from sites that block simple HTTP fetchers."
metadata: {"yeoman":{"emoji":"🌐","requires":{"bins":["bwrap","chromium"]}}}
---

# Browser (pinchtab)

Full Chrome control over a local HTTP API. Returns accessibility-tree text rather than screenshots,
which keeps it cheap and fast. Session-persistent: log in once, and the agent can reuse the session.

## Files

- Starter: `scripts/start.sh`
- Monitor helper: `scripts/monitor.py`

## Starting pinchtab

```bash
# Headless (default)
bash scripts/start.sh &

# Headed — useful for manual login / CAPTCHA solving
BRIDGE_HEADLESS=false bash scripts/start.sh &

# Optional bearer token
mkdir -p state
echo "my-secret" > state/bridge_token.txt

# Health check
curl -s http://localhost:9867/health
```

## Core API

Navigate:

```bash
curl -s -X POST http://localhost:9867/navigate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Read page text:

```bash
curl -s 'http://localhost:9867/text'
```

Read interactive elements only:

```bash
curl -s 'http://localhost:9867/snapshot?filter=interactive&format=text'
```

Detect changes:

```bash
curl -s 'http://localhost:9867/snapshot?diff=true&format=text'
```

Click:

```bash
curl -s -X POST http://localhost:9867/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"click","selector":"button[type=submit]"}'
```

Fill:

```bash
curl -s -X POST http://localhost:9867/action \
  -H 'Content-Type: application/json' \
  -d '{"type":"fill","selector":"#search","value":"query text"}'
```

Evaluate JavaScript:

```bash
curl -s -X POST http://localhost:9867/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"expression":"document.title"}'
```

## Workflows

### Read a page

```bash
curl -s -X POST http://localhost:9867/navigate -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article"}'
curl -s 'http://localhost:9867/text'
```

### Authenticated access

1. Start in headed mode: `BRIDGE_HEADLESS=false bash scripts/start.sh &`
2. Log in manually in the visible browser window.
3. Session state persists in `~/.yeoman/pinchtab/data/`.
4. Restart headless if desired.

### Cron-based change monitoring

Use `scripts/monitor.py` with the cron skill. With `--quiet`, it emits output only when the page
changed.

```bash
python3 scripts/monitor.py \
  --url "https://jobs.example.com" \
  --label "Example job board" \
  --quiet
```

### Form automation

```bash
curl -s -X POST http://localhost:9867/navigate \
  -d '{"url":"https://example.com/contact"}'
curl -s -X POST http://localhost:9867/action \
  -d '{"type":"fill","selector":"#name","value":"Alice"}'
curl -s -X POST http://localhost:9867/action \
  -d '{"type":"fill","selector":"#message","value":"Hello"}'
curl -s -X POST http://localhost:9867/action \
  -d '{"type":"click","selector":"button[type=submit]"}'
curl -s 'http://localhost:9867/text'
```

## Token Budget Tips

| Endpoint | Approx tokens | Use when |
|----------|---------------|----------|
| `/text` | ~800 | Reading content |
| `/snapshot?filter=interactive` | ~200–400 | Mapping buttons / forms |
| `/snapshot?diff=true` | ~50–500 | Change detection |
| `/snapshot` | ~1500–3000 | Need full structure |
| `/screenshot` | ~2000 (vision) | Only when visual layout matters |
