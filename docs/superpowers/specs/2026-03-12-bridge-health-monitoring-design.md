# WhatsApp Bridge Health Monitoring

**Date:** 2026-03-12
**Status:** Approved

## Problem

The WhatsApp bridge (Baileys Node.js process) can silently lose its connection to WhatsApp servers while the gateway↔bridge WebSocket remains alive. When this happens, messages stop flowing with no error or notification — the system appears healthy but is deaf.

Manual detection requires noticing missing messages and manually restarting the bridge.

## Goals

1. Detect bridge disconnections automatically (both sudden and gradual)
2. Recover without human intervention
3. Notify the owner via Telegram when recovery occurs
4. Avoid false positives from quiet chat periods
5. No changes to the bridge TypeScript code

## Non-Goals

- Message staleness detection (too many false positives from legitimately quiet chats)
- Multi-bridge or cluster-aware monitoring
- WhatsApp server-side health checks (risk of looking suspicious to WA)

## Design

### Two-Layer Detection

#### 1. Reactive Layer — Gateway Reconnect Exhaustion

The gateway's `WhatsAppChannel.start()` has an existing reconnect loop (lines 242–314) with exponential backoff. When `self._reconnect_attempts >= self.config.reconnect_max_attempts`, the loop gives up and the channel goes dead.

**Note:** The bridge also emits `reconnect_exhausted` over WebSocket, but this event may never arrive because the bridge process exits immediately after emitting it, killing the WebSocket. Therefore the reactive layer triggers on the **gateway-side** reconnect exhaustion, not the bridge-side event.

**Trigger:** When the gateway's own reconnect loop exhausts its attempts, initiate recovery instead of giving up.

**Location:** `WhatsAppChannel` — modify the existing reconnect loop's exhaustion path.

Additionally, if `reconnect_exhausted` does arrive via WebSocket before the connection drops, handle it in the existing `_handle_bridge_message` status handler (line 585) as a secondary trigger.

#### 2. Periodic Layer — Health Poll

A background `asyncio.Task` polls the bridge health endpoint every 30 minutes using the existing `health` WebSocket command.

**Check:** Only inspects `whatsapp.connected` (boolean). No staleness/timing checks.

- If `connected` is `false` → initiate recovery
- If `connected` is `true` → no action

The health poll **only runs when `self._connected is True`** (gateway↔bridge WebSocket is up). If the WebSocket itself is dead, the existing reconnect loop already handles it — the health poll adds value only for the case where the WebSocket is alive but bridge↔WhatsApp is disconnected.

**Location:** `WhatsAppChannel` — new `_health_monitor_loop` coroutine started on channel init.

### Why No Staleness Check

WhatsApp chats can legitimately go silent for hours. Any `lastMessageAt`-based threshold would produce false positives on quiet days. The `connected` boolean from Baileys is authoritative — if Baileys says it's connected, it is.

### Recovery Flow

When either layer triggers recovery:

```
1. Check cooldown (10 min since last recovery) → skip if too soon
2. Set a cancellation flag so the existing reconnect loop yields control
3. Kill the bridge process via WhatsAppRuntimeManager.stop_bridge()
   (SIGTERM → SIGKILL after 5s), wrapped in asyncio.to_thread()
4. Call WhatsAppRuntimeManager.ensure_runtime() to re-copy bridge files
   (via asyncio.to_thread())
5. Call WhatsAppRuntimeManager.start_bridge() (via asyncio.to_thread())
6. Wait for _connected_event (asyncio.Event) set by the start() loop
   after successful WebSocket + health check (timeout: 30s)
7. Send Telegram notification to owner via notification callback
8. Record recovery timestamp for cooldown
```

#### Reconnect Loop Coordination

Recovery must not race with the existing reconnect loop in `start()`. The mechanism:

- `_trigger_recovery` sets `self._recovery_in_progress = True`
- The reconnect loop checks this flag at the top of each iteration; if set, it breaks out and waits on `self._recovery_complete` (an `asyncio.Event`)
- After recovery finishes (success or failure), `_trigger_recovery` clears the flag and sets the event, allowing `start()` to re-enter its main loop with the fresh bridge

#### Connected Signal

An `asyncio.Event` (`self._connected_event`) is set by the `start()` loop after it successfully connects the WebSocket and verifies bridge health. The recovery flow awaits this event with a 30-second timeout to confirm the bridge is back.

#### Cooldown

A 10-minute cooldown prevents recovery loops when the underlying issue is persistent (e.g., WhatsApp account banned, network down). If recovery triggers again within cooldown, log a warning but take no action.

#### Telegram Notification

On recovery (successful or failed):

```
✅ WhatsApp bridge recovered automatically
   Trigger: reconnect_exhausted | health_poll_disconnected
   Downtime: ~Xm (estimated from last known-good timestamp)
```

Or on failure:

```
❌ WhatsApp bridge recovery failed
   Trigger: <trigger>
   Action needed: manual restart
```

Notification is sent via a callback injected at bootstrap time. Bootstrap resolves the owner Telegram chat ID from `policy.json` → `owners.telegram[0]` and creates a callback that publishes an `OutboundMessage` to the `MessageBus` (following the existing `_admin_notify` pattern in bootstrap.py).

### Architecture

```
WhatsAppChannel
├── _handle_bridge_message(msg)      # existing: add reconnect_exhausted case in status handler
├── start()                          # existing: add recovery flag check in reconnect loop,
│                                    #   set _connected_event on successful connection
├── _health_monitor_loop()           # new: 30-min poll loop (only when WebSocket is up)
├── _trigger_recovery(reason)        # new: coordinates with reconnect loop, delegates to runtime
├── _recovery_in_progress: bool      # new: flag to pause reconnect loop
├── _recovery_complete: Event        # new: signal reconnect loop to resume
├── _connected_event: Event          # new: signal recovery that bridge is back
├── _last_recovery_at: float         # new: cooldown tracking
├── _recovery_cooldown_s: int = 600  # new: 10-min cooldown
└── _notify: callback | None         # new: injected Telegram notification callback

WhatsAppRuntimeManager (whatsapp_runtime.py)
├── stop_bridge()                    # existing: SIGTERM/SIGKILL bridge process
├── ensure_runtime()                 # existing: copy bridge files from package to cache
└── start_bridge()                   # existing: spawn bridge Node.js process

bootstrap.py
└── Create notification callback (OutboundMessage via MessageBus)
    and inject into WhatsAppChannel
```

### What Already Exists (No Changes Needed)

| Component | Current State |
|-----------|--------------|
| Bridge `health` command | Returns `{ whatsapp: { connected, lastMessageAt, reconnectAttempts } }` |
| Bridge status events | Emits `connected`, `disconnected`, `reconnecting`, `reconnect_exhausted` |
| `_handle_bridge_message` status handler | Logs status events (line 585) |
| `WhatsAppRuntimeManager` | `stop_bridge()`, `start_bridge()`, `ensure_runtime()` |
| `start()` reconnect loop | Exponential backoff reconnection (lines 242–314) |
| `_admin_notify` pattern in bootstrap | `OutboundMessage` via `MessageBus` |

### Files to Modify

1. **`yeoman/channels/whatsapp.py`**
   - Add `reconnect_exhausted` case in `_handle_bridge_message` status handler
   - Modify reconnect loop in `start()` to check `_recovery_in_progress` flag
   - Add `_connected_event` signaling after successful connection
   - Add `_health_monitor_loop()` coroutine (runs only when WebSocket connected)
   - Add `_trigger_recovery(reason)` method (delegates to `self._runtime` via `asyncio.to_thread()`)
   - Add state: `_recovery_in_progress`, `_recovery_complete`, `_connected_event`, `_last_recovery_at`, `_recovery_cooldown_s`, `_notify`

2. **`yeoman/app/bootstrap.py`**
   - Create notification callback using existing `_admin_notify` / `OutboundMessage` pattern
   - Resolve owner Telegram chat ID from policy
   - Inject callback into `WhatsAppChannel`

### Configuration

No new config fields. Uses existing:
- `policy.json` → `owners.telegram[0]` for notification target
- Bridge health endpoint (already available via WebSocket)
- Hardcoded: 30-min poll interval, 10-min cooldown, 30s connect timeout, 5s SIGKILL grace

These can be promoted to `config.json` later if tuning is needed.

## Testing Strategy

- Unit test `_trigger_recovery` with mocked `WhatsAppRuntimeManager` and notification callback
- Unit test cooldown logic (second trigger within 10 min is skipped)
- Unit test health poll loop (connected=true → no action, connected=false → recovery)
- Unit test reactive handler (reconnect_exhausted in `_handle_bridge_message` → recovery)
- Unit test reconnect loop coordination (recovery flag pauses loop, event resumes it)
- Integration: manual bridge kill → verify auto-restart + Telegram notification
