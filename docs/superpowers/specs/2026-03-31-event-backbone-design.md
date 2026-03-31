# Event Backbone Design

**Date**: 2026-03-31
**Status**: Draft
**Scope**: Gateway event bus, overseer-gateway IPC bridge, webhook ingestion
**Depends on**: Existing MessageBus, FastAPI server, overseer Unix socket
**Enables**: Spec 2 (Autonomous Workflows)

## Problem

The overseer and gateway are two processes that cannot communicate. The overseer
has a Unix socket with `ping` and `get_stats` — nothing else. When a runbook
detects a problem, the overseer sends alerts through its own Telegram comms,
bypassing the gateway entirely. It cannot:

- Send a WhatsApp message through the gateway
- Trigger a full agent turn (with tools) through the gateway
- React to gateway events (new chat, error spike, policy violation)

Meanwhile, nothing from the outside world can trigger the system except a chat
message or a cron timer. GitHub cannot notify yeoman of a PR. A calendar cannot
push a reminder. A smart home sensor cannot fire an alert.

## Solution: Three Layers

```
Layer 1: Gateway Event Bus (internal)
    Typed async pub/sub extending the existing MessageBus

Layer 2: Overseer <-> Gateway Bridge (local IPC)
    Bidirectional JSON-RPC over dual Unix sockets

Layer 3: Webhook Receiver (external -> internal)
    POST /webhooks/{source} on existing FastAPI, HMAC-verified
    Events normalized and published to the event bus
```

Each layer builds on the previous. No new listening surfaces — the FastAPI
server already exists on localhost, the Unix socket already exists. We expand
their protocols, not open new ports.

---

## Layer 1: Gateway Event Bus

### What changes

The current `MessageBus` in `bus/queue.py` has 3 hardcoded queues (`inbound`,
`outbound`, `reaction`) with channel-specific subscribers. We extend it — not
replace it — with a typed event system.

### Event types

New event dataclasses in `yeoman_gateway/bus/events.py` (alongside existing
`OutboundMessage`, `InboundMessage`, `ReactionMessage`):

| Event | Fields | Source |
|-------|--------|--------|
| `WebhookEvent` | `source: str`, `event_type: str`, `payload: dict`, `signature_verified: bool`, `received_at: float` | Webhook receiver |
| `OverseerCommand` | `command: str`, `args: dict`, `correlation_id: str` | Overseer bridge |
| `SystemEvent` | `kind: str`, `detail: dict`, `timestamp: float` | Gateway internals |

Union type: `GatewayEvent = WebhookEvent | OverseerCommand | SystemEvent`

All are frozen dataclasses, same pattern as `core/intents.py`.

### MessageBus additions

New members on the existing `MessageBus` class in `bus/queue.py`:

```python
# Two new queues — separated so webhook floods cannot starve IPC commands
self._event_queue: asyncio.Queue[GatewayEvent]    # WebhookEvent, SystemEvent (bounded)
self._ipc_queue: asyncio.Queue[OverseerCommand]    # OverseerCommand only (unbounded)

# Typed handler registry (keyed by event class name)
self._event_handlers: dict[str, list[Callable[[GatewayEvent], Awaitable[None]]]]

# Public API
def subscribe_event(self, event_type: str, handler: Callable) -> None: ...
async def publish_event(self, event: GatewayEvent) -> None: ...
async def dispatch_events(self) -> None:  # background loop for both queues
```

The existing `inbound`/`outbound`/`reaction` queues stay untouched. Chat
messages flow through them exactly as before. The event system is additive.

**Queue isolation**: `OverseerCommand` gets its own unbounded queue. A burst of
webhook events can fill and drop from `_event_queue` without affecting IPC
commands. This prevents a noisy external source from starving critical internal
communication.

### Subscribers

- `OrchestratorService` subscribes to `WebhookEvent` — normalizes to synthetic
  `InboundEvent`, runs through the middleware pipeline
- `OrchestratorService` subscribes to `OverseerCommand` — dispatches directly
  (send message via bus, trigger agent turn via responder)
- Future: workflow engine subscribes to events that trigger workflow steps

### Wiring

In `bootstrap.py` `OrchestratorService.__init__` (~line 100): receive the bus,
register event handlers. In `GatewayRuntime.run()` (~line 227): start
`dispatch_events()` as a background task alongside existing `dispatch_outbound()`.

### Config

Add to `Config` schema:

```python
class BusConfig(BaseModel):
    inbound_maxsize: int = 0
    outbound_maxsize: int = 0
    event_maxsize: int = 100  # new
```

### Files

| File | Change |
|------|--------|
| `yeoman_gateway/bus/events.py` | Add `WebhookEvent`, `OverseerCommand`, `SystemEvent`, `GatewayEvent` union |
| `yeoman_gateway/bus/queue.py` | Add `_event_queue`, `subscribe_event()`, `publish_event()`, `dispatch_events()` |
| `yeoman_gateway/app/bootstrap.py` | Register event handlers in `OrchestratorService`, start dispatch loop |
| `yeoman_shared/config/schema.py` | Add `event_maxsize` to `BusConfig` |

---

## Layer 2: Overseer-Gateway Bridge

### Architecture: Dual Sockets

Each process owns a socket for what it controls:

| Socket | Owner | Clients | Purpose |
|--------|-------|---------|---------|
| `~/.yeoman/run/overseer.sock` | Overseer | Gateway | Gateway queries overseer status |
| `~/.yeoman/run/gateway.sock` | Gateway | Overseer | Overseer sends commands to gateway |

Both use the same protocol: line-delimited JSON-RPC (request/response). No
push, no keep-alive, no streaming. Each socket is dead-simple: receive request,
dispatch, respond.

### Gateway socket commands

New commands handled by the gateway socket server:

| Command | Args | What happens |
|---------|------|--------------|
| `send_message` | `channel`, `chat_id`, `content` | Publish `OutboundMessage` to bus |
| `trigger_agent_turn` | `prompt`, `session_key`, `channel`, `chat_id`, `model_profile?` | Call `responder.process_direct()` — full agent turn with tools |
| `publish_event` | `kind`, `detail` | Publish `SystemEvent` to event bus |
| `get_session_state` | `session_key` | Return session message count, last activity |
| `notify_runbook_result` | `runbook_name`, `status`, `summary`, `deliver_to?` | Format and deliver runbook outcome to owner |

### Overseer socket commands (additions to existing)

| Command | Args | What happens |
|---------|------|--------------|
| `ping` | — | (existing) Returns `pong` |
| `get_stats` | — | (existing) Returns overseer stats |
| `get_runbook_status` | `name?` | New: returns runbook run history and status |

### Protocol

Unchanged from existing overseer socket:

```
Client connects to Unix socket
Client writes: {"cmd": "send_message", "args": {"channel": "whatsapp", ...}}\n
Server reads line, dispatches, writes: {"status": "ok", "response": ...}\n
Client reads response
Client disconnects (or sends another command)
```

### Security

| Concern | Mitigation |
|---------|------------|
| Socket file permissions | `0600` on both sockets (owner-only) |
| Unauthorized process | Same Unix user requirement. Socket file ACL is the auth boundary. |
| Command injection | `_dispatch()` is a fixed match over known commands, never shell-evaluated |
| Dangerous agent turn | `trigger_agent_turn` uses `process_direct()` — same `SecurityPort` pipeline as cron |
| Flood | Per-second rate limit on gateway socket (10 cmd/s). Log and drop excess. |

### Config

Add to `Config` schema:

```python
class IpcConfig(BaseModel):
    gateway_socket_path: str = "~/.yeoman/run/gateway.sock"
    overseer_socket_path: str = "~/.yeoman/run/overseer.sock"
    command_rate_limit: int = 10  # per second
```

### Files

| File | Change |
|------|--------|
| `yeoman_gateway/ipc/__init__.py` | New package |
| `yeoman_gateway/ipc/gateway_socket.py` | New: gateway-side socket server |
| `yeoman_gateway/ipc/overseer_client.py` | New: gateway-side client for querying overseer |
| `yeoman_overseer/gateway/client.py` | New: overseer-side client for sending commands to gateway |
| `yeoman_overseer/socket/server.py` | Add `get_runbook_status` command to `_dispatch()` |
| `yeoman_gateway/app/bootstrap.py` | Start gateway socket in `GatewayRuntime.run()` |
| `yeoman_shared/config/schema.py` | Add `IpcConfig` section |

### Connection lifecycle and resilience

Process startup order is not guaranteed. The overseer may restart while the
gateway is running, or vice versa. Both client implementations
(`overseer_client.py`, `gateway/client.py`) must handle dead sockets gracefully:

- **Connect on first use, not on startup.** Clients open the socket lazily when
  the first command is sent, not during `__init__`.
- **Retry with backoff on connection failure.** If the socket is unavailable,
  retry up to 3 times with 1s/2s/4s delays. If all retries fail, log a warning
  and return an error response to the caller — never crash the host process.
- **No persistent connections.** Each command opens a connection, sends the
  request, reads the response, and closes. This avoids stale connection state
  after a peer restart.
- **Socket file cleanup on startup.** Each server removes its own stale `.sock`
  file before binding (standard Unix socket pattern — prevents "address already
  in use" after unclean shutdown).

### Dependencies

Zero. `asyncio.start_unix_server` / `asyncio.open_unix_connection` (stdlib).
JSON serialization (stdlib). No new libraries.

---

## Layer 3: Webhook Receiver

### What changes

Add `POST /webhooks/{source}` to the existing FastAPI server. External services
push events that get normalized and routed through the pipeline.

### Flow

```
External service (GitHub, CalDAV, etc.)
  -> POST /webhooks/github  (with X-Hub-Signature-256 header)
  -> HMAC verification against per-source secret
  -> Timestamp freshness check (reject >5 min old)
  -> Rate limit (per-source, reuse existing _check_rate_limit)
  -> Normalize payload to WebhookEvent
  -> bus.publish_event(WebhookEvent(...))
  -> OrchestratorService handler picks it up
  -> Normalizes to InboundEvent (synthetic message)
  -> Pipeline runs: Policy -> Security -> Responder -> Outbound
  -> Agent decides what to do
```

### Config

```python
class WebhookSourceConfig(BaseModel):
    secret_env: str                          # env var name holding HMAC secret
    deliver_to: dict[str, str]               # {"channel": "whatsapp", "chat_id": "..."}
    allowed_events: list[str] | None = None   # None = allow all, [] = allow none
    rate_limit: int = 30                     # requests per minute

class WebhooksConfig(BaseModel):
    enabled: bool = False                    # off by default
    sources: dict[str, WebhookSourceConfig] = {}
```

Key design choices:

- **`enabled: false` by default** — no new attack surface unless opted in
- **`secret_env`** references an environment variable, never inline in config
- **`deliver_to`** is mandatory — every source declares its destination
- **`allowed_events`** is an allowlist — `None` allows all events, `[]` blocks all, specific list filters to those types only

### Normalization

Minimal, source-specific extractors. No external libraries for payload parsing:

```python
def _normalize_webhook(source: str, event_type: str, payload: dict) -> str:
    if source == "github":
        return _normalize_github(event_type, payload)
    # Generic fallback: pretty-printed JSON truncated to 1000 chars.
    # The LLM is smart enough to read raw JSON from unknown sources
    # (smart home sensors, calendar events, etc.) without a dedicated normalizer.
    raw = json.dumps(payload, indent=2, default=str)
    if len(raw) > 1000:
        raw = raw[:1000] + "\n...[truncated]"
    return f"[Webhook: {source}] event={event_type}\n{raw}"

def _normalize_github(event_type: str, payload: dict) -> str:
    repo = payload.get("repository", {}).get("full_name", "unknown")
    action = payload.get("action", "")
    # Source-specific one-liners per event type
    if event_type == "push":
        ref = payload.get("ref", "").replace("refs/heads/", "")
        count = len(payload.get("commits", []))
        return f"[GitHub] {repo}: {count} commit(s) pushed to {ref}"
    if event_type.startswith("pull_request"):
        pr = payload.get("pull_request", {})
        return f"[GitHub] {repo}: PR #{pr.get('number')} {action}: {pr.get('title', '')}"
    return f"[GitHub] {repo}: {event_type} {action}"
```

The agent receives this as a synthetic inbound message. It decides (via policy
and judgment) whether to act, inform, or ignore.

### Security

| Concern | Mitigation |
|---------|------------|
| Internet exposure | FastAPI binds `127.0.0.1` (existing default). External access requires explicit reverse proxy config. |
| Forged payloads | HMAC-SHA256 per source. No secret configured -> 404 (not 401, avoids confirming endpoint existence). |
| Replay attacks | Timestamp header checked. Reject if >5 min old. |
| Prompt injection | Content enters as synthetic `InboundEvent` wrapped in `_wrap_untrusted_message()`. Same `InputSecurityMiddleware`. |
| Flood | Per-source rate limit + bus backpressure (`_put_bounded()` drops on overflow). |
| Secret leakage | `secret_env` points to env var, not inline value. |
| Unknown source | Source not in config -> 404. No dynamic registration. |

### Files

| File | Change |
|------|--------|
| `yeoman_gateway/api/webhooks.py` | New: FastAPI router with webhook endpoint |
| `yeoman_gateway/api/server.py` | Mount webhook router in `create_app()` (~line 170) |
| `yeoman_shared/config/schema.py` | Add `WebhooksConfig`, `WebhookSourceConfig` |
| `yeoman_gateway/app/bootstrap.py` | Pass `bus` to API server for webhook event publishing |

### Dependencies

Zero. HMAC is stdlib (`hmac`, `hashlib`). FastAPI already exists.

---

## Out of scope

- Outgoing webhooks (yeoman calling external APIs on events) — future scope
- MCP client/server — deferred to connectivity phase
- Plugin/hook system — deferred to connectivity phase
- Gateway-to-overseer event push (gateway notifying overseer of events) — not
  needed until overseer needs to react to gateway state changes
- SSE/WebSocket transport for webhooks — stdout is sufficient for v1
- Webhook management UI — config file is the interface
- Per-webhook policy overrides — all webhooks use default policy for now

## Testing strategy

- Unit: event bus pub/sub with mock handlers
- Unit: HMAC verification (valid, invalid, missing, expired)
- Unit: webhook normalization (GitHub push, PR, unknown event, generic fallback)
- Unit: gateway socket command dispatch (each command type)
- Integration: webhook -> event bus -> orchestrator -> synthetic inbound
- Integration: overseer client -> gateway socket -> bus publish -> outbound
- Integration: cycle through all three layers end-to-end
