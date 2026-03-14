---
name: ops
description: System operations — check logs, service health, system stats, and manage services.
always: true
---

# Ops

You have two operational tools for monitoring and managing the system.

## `ops` — read-only monitoring

### Check for errors
```
ops(action="log_scan", service="gateway", level="error", since="1h")
```

### Search logs by keyword
```
ops(action="log_scan", service="gateway", keyword="connection refused", since="2h")
```

### Check if services are running
```
ops(action="service_status", service="all")
```

### System stats (CPU, memory, disk, temperature)
```
ops(action="system_stats")
```

## `ops_manage` — service management (requires confirmation)

### Restart bridge
```
ops_manage(action="restart", service="bridge")
```
This returns a 4-digit code. Wait for the user to reply with the code, then:
```
ops_manage(action="confirm", code="<the code>")
```

### Stop gateway
```
ops_manage(action="stop", service="gateway")
```
Same confirmation flow applies.

## Parameters

### log_scan parameters
- `service` (required): `"gateway"` or `"bridge"`
- `level`: `"debug"`, `"info"`, `"warning"`, `"error"`, `"critical"` — minimum level
- `since`: time range start — `"1h"`, `"30m"`, `"2d"`, or `"2026-03-13 10:00"`
- `until`: time range end (defaults to now)
- `keyword`: case-insensitive text search
- `limit`: max lines (1–100, default 50)

## Architecture Ground Truth

These are facts about how you run. NEVER contradict or fabricate alternatives.

- **Gateway**: Python process, managed via PID file (`~/.yeoman/var/run/gateway.pid`).
- **Bridge**: Node.js process (`node dist/index.js`), holds a live WebSocket to `web.whatsapp.com`.
  Managed via PID file (`~/.yeoman/var/run/whatsapp-bridge.pid`).
- **Gateway ↔ Bridge**: connected via `ws://localhost:3001` (protocol v2).
- **No systemd/systemctl units exist.** Do not suggest `systemctl restart yeoman-*`.
- **No webhooks, no cached relay, no inbound proxy.** Messages flow:
  `WhatsApp servers → Bridge (WebSocket) → Gateway (local WS) → Orchestrator pipeline → LLM → reply back`.
- **DNS errors** (`getaddrinfo EAI_AGAIN`) = transient network issue on the host, not a code bug.
  The bridge retries automatically.
- **"Opening handshake has timed out"** = WhatsApp's WebSocket server unreachable. Same category — network, not code.
- **Disconnected → reconnecting → connected** sequences are normal keepalive resets.
  Only escalate if reconnection fails repeatedly (>5 min).

## Guidance

- **ALWAYS call tools first.** When asked about status, errors, or connectivity:
  1. `ops(action="service_status", service="all")`
  2. `ops(action="log_scan", service="gateway", level="error", since="1h")` if needed
  Never answer infrastructure questions from memory or guesswork.
- Summarize results conversationally. Don't dump raw log output — pick out the key findings.
- If a service is down, proactively suggest what to do (check logs, restart via `ops_manage`).
- When the user asks "what can you do?" or "help" about ops:
  > I can help you with:
  > - **Log scanning** — search logs by level, keyword, time range (gateway & bridge)
  > - **Service status** — check if gateway/bridge are running, uptime, health
  > - **System stats** — CPU, memory, disk, temperature, top processes
  > - **Service management** — restart or stop gateway/bridge (requires confirmation code)
  >
  > Just ask naturally, e.g. "any errors in the last hour?" or "is the bridge running?"
