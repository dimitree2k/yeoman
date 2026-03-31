# Event Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable typed event pub/sub in the gateway, bidirectional IPC between overseer and gateway via dual Unix sockets, and HMAC-verified webhook ingestion on the existing FastAPI server.

**Architecture:** Three additive layers — (1) extend MessageBus with a typed event queue, (2) add a gateway-side Unix socket server and overseer-side client for IPC, (3) mount a webhook router on the existing FastAPI app. Each layer builds on the previous. No new listening surfaces, no new dependencies.

**Tech Stack:** Python asyncio (stdlib), JSON-RPC over Unix sockets (stdlib), FastAPI (existing), hmac/hashlib (stdlib)

---

### Task 1: Event Type Dataclasses

**Files:**
- Modify: `packages/gateway/yeoman_gateway/bus/events.py`
- Test: `tests/gateway/test_event_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_event_types.py
"""Tests for gateway event types."""

import time

from yeoman_gateway.bus.events import (
    GatewayEvent,
    OverseerCommand,
    SystemEvent,
    WebhookEvent,
)


def test_webhook_event_is_frozen() -> None:
    ev = WebhookEvent(
        source="github",
        event_type="push",
        payload={"ref": "refs/heads/main"},
        signature_verified=True,
        received_at=time.time(),
    )
    assert ev.source == "github"
    assert ev.signature_verified is True
    # Frozen — cannot mutate
    try:
        ev.source = "other"  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass


def test_overseer_command_fields() -> None:
    cmd = OverseerCommand(command="send_message", args={"channel": "whatsapp"}, correlation_id="abc")
    assert cmd.command == "send_message"
    assert cmd.args["channel"] == "whatsapp"


def test_system_event_fields() -> None:
    ev = SystemEvent(kind="channel_connected", detail={"name": "telegram"}, timestamp=time.time())
    assert ev.kind == "channel_connected"


def test_gateway_event_union() -> None:
    ev: GatewayEvent = WebhookEvent(
        source="test", event_type="ping", payload={}, signature_verified=False, received_at=0.0,
    )
    assert isinstance(ev, WebhookEvent)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_event_types.py -v`
Expected: FAIL — `ImportError: cannot import name 'WebhookEvent'`

- [ ] **Step 3: Implement the event dataclasses**

Add to end of `packages/gateway/yeoman_gateway/bus/events.py`:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookEvent:
    """Event received from an external webhook."""

    source: str
    event_type: str
    payload: dict[str, Any]
    signature_verified: bool
    received_at: float


@dataclass(frozen=True, slots=True, kw_only=True)
class OverseerCommand:
    """Command received from the overseer via IPC socket."""

    command: str
    args: dict[str, Any]
    correlation_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemEvent:
    """Internal gateway system event."""

    kind: str
    detail: dict[str, Any]
    timestamp: float


type GatewayEvent = WebhookEvent | OverseerCommand | SystemEvent
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_event_types.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/bus/events.py tests/gateway/test_event_types.py
git commit -m "feat(bus): add WebhookEvent, OverseerCommand, SystemEvent types"
```

---

### Task 2: MessageBus Event Queue Extension

**Files:**
- Modify: `packages/gateway/yeoman_gateway/bus/queue.py`
- Test: `tests/gateway/test_event_bus.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_event_bus.py
"""Tests for MessageBus event pub/sub."""

import asyncio
import time

import pytest

from yeoman_gateway.bus.events import OverseerCommand, SystemEvent, WebhookEvent
from yeoman_gateway.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_publish_and_dispatch_webhook_event() -> None:
    bus = MessageBus(event_maxsize=10)
    received: list[WebhookEvent] = []

    async def handler(ev: WebhookEvent) -> None:
        received.append(ev)

    bus.subscribe_event("WebhookEvent", handler)

    ev = WebhookEvent(
        source="github", event_type="push", payload={}, signature_verified=True, received_at=time.time(),
    )
    await bus.publish_event(ev)

    dispatch_task = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.05)
    bus.stop()
    await dispatch_task

    assert len(received) == 1
    assert received[0].source == "github"


@pytest.mark.asyncio
async def test_ipc_queue_not_affected_by_event_overflow() -> None:
    bus = MessageBus(event_maxsize=2)
    ipc_received: list[OverseerCommand] = []

    async def ipc_handler(ev: OverseerCommand) -> None:
        ipc_received.append(ev)

    bus.subscribe_event("OverseerCommand", ipc_handler)

    # Fill the event queue beyond capacity
    for i in range(5):
        await bus.publish_event(
            WebhookEvent(source="flood", event_type=f"ev{i}", payload={}, signature_verified=True, received_at=0.0)
        )

    # IPC command should still go through
    await bus.publish_event(
        OverseerCommand(command="ping", args={}, correlation_id="test")
    )

    dispatch_task = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.05)
    bus.stop()
    await dispatch_task

    assert len(ipc_received) == 1
    assert ipc_received[0].command == "ping"


@pytest.mark.asyncio
async def test_event_dropped_counter() -> None:
    bus = MessageBus(event_maxsize=1)
    await bus.publish_event(
        WebhookEvent(source="a", event_type="t", payload={}, signature_verified=True, received_at=0.0)
    )
    await bus.publish_event(
        WebhookEvent(source="b", event_type="t", payload={}, signature_verified=True, received_at=0.0)
    )
    assert bus.event_dropped >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_event_bus.py -v`
Expected: FAIL — `TypeError: MessageBus.__init__() got an unexpected keyword argument 'event_maxsize'`

- [ ] **Step 3: Implement the event bus extension**

Modify `packages/gateway/yeoman_gateway/bus/queue.py`. Changes:

1. Add import for new event types at top:
```python
from yeoman_gateway.bus.events import (
    GatewayEvent,
    InboundMessage,
    OverseerCommand,
    OutboundMessage,
    ReactionMessage,
)
```

2. Update `__init__` to accept `event_maxsize` and create two new queues:
```python
def __init__(
    self, *, inbound_maxsize: int = 0, outbound_maxsize: int = 0,
    reaction_maxsize: int = 0, event_maxsize: int = 100,
):
    # ... existing queues unchanged ...
    self._event_queue: asyncio.Queue[GatewayEvent] = asyncio.Queue(maxsize=max(0, event_maxsize))
    self._ipc_queue: asyncio.Queue[OverseerCommand] = asyncio.Queue()  # unbounded
    self._event_handlers: dict[str, list[Callable[[GatewayEvent], Awaitable[None]]]] = {}
    self._event_dropped = 0
```

3. Add new methods after existing `subscribe_outbound`:
```python
def subscribe_event(
    self, event_type: str, handler: Callable[[GatewayEvent], Awaitable[None]]
) -> None:
    """Subscribe to a specific event type by class name."""
    if event_type not in self._event_handlers:
        self._event_handlers[event_type] = []
    self._event_handlers[event_type].append(handler)

async def publish_event(self, event: GatewayEvent) -> None:
    """Publish an event. OverseerCommands go to unbounded IPC queue, others to bounded event queue."""
    if isinstance(event, OverseerCommand):
        await self._ipc_queue.put(event)
    else:
        await self._put_bounded(self._event_queue, event, "event")

async def dispatch_events(self) -> None:
    """Dispatch events from both event and IPC queues to handlers."""
    self._running = True
    while self._running:
        # Drain both queues with short timeout
        dispatched = False
        for queue in (self._ipc_queue, self._event_queue):
            try:
                event = queue.get_nowait()
                type_name = type(event).__name__
                for handler in self._event_handlers.get(type_name, []):
                    try:
                        await handler(event)
                    except Exception as e:
                        logger.error(f"Error dispatching {type_name}: {e}")
                dispatched = True
            except asyncio.QueueEmpty:
                continue
        if not dispatched:
            await asyncio.sleep(0.05)
```

4. Update `_put_bounded` to handle event drops:
In the `else` branch of the drop counter, add:
```python
elif channel == "event":
    self._event_dropped += 1
    dropped = self._event_dropped
```

5. Add property:
```python
@property
def event_dropped(self) -> int:
    """Number of dropped events due to queue overflow."""
    return self._event_dropped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_event_bus.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/bus/queue.py tests/gateway/test_event_bus.py
git commit -m "feat(bus): add event queue with pub/sub and IPC isolation"
```

---

### Task 3: Config Schema — IPC and Webhooks

**Files:**
- Modify: `packages/shared/yeoman_shared/config/schema.py`
- Test: `tests/shared/test_config_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shared/test_config_ipc_webhooks.py
"""Tests for IPC and webhook config schema."""

from yeoman_shared.config.schema import Config, IpcConfig, WebhookSourceConfig, WebhooksConfig


def test_ipc_config_defaults() -> None:
    cfg = IpcConfig()
    assert cfg.gateway_socket_path == "~/.yeoman/run/gateway.sock"
    assert cfg.overseer_socket_path == "~/.yeoman/run/overseer.sock"
    assert cfg.command_rate_limit == 10


def test_webhooks_config_disabled_by_default() -> None:
    cfg = WebhooksConfig()
    assert cfg.enabled is False
    assert cfg.sources == {}


def test_webhook_source_config() -> None:
    src = WebhookSourceConfig(
        secret_env="GITHUB_WEBHOOK_SECRET",
        deliver_to={"channel": "whatsapp", "chat_id": "owner"},
    )
    assert src.rate_limit == 30
    assert src.allowed_events is None  # None = allow all


def test_config_has_ipc_and_webhooks() -> None:
    cfg = Config()
    assert isinstance(cfg.ipc, IpcConfig)
    assert isinstance(cfg.webhooks, WebhooksConfig)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/shared/test_config_ipc_webhooks.py -v`
Expected: FAIL — `ImportError: cannot import name 'IpcConfig'`

- [ ] **Step 3: Add config models**

In `packages/shared/yeoman_shared/config/schema.py`, add before `BusConfig` (around line 463):

```python
class IpcConfig(BaseModel):
    """Inter-process communication configuration."""

    gateway_socket_path: str = "~/.yeoman/run/gateway.sock"
    overseer_socket_path: str = "~/.yeoman/run/overseer.sock"
    command_rate_limit: int = 10  # max commands per second


class WebhookSourceConfig(BaseModel):
    """Configuration for a single webhook source."""

    secret_env: str  # env var name holding HMAC secret
    deliver_to: dict[str, str]  # {"channel": "whatsapp", "chat_id": "..."}
    allowed_events: list[str] | None = None  # None = allow all, [] = block all
    rate_limit: int = 30  # requests per minute


class WebhooksConfig(BaseModel):
    """Webhook ingestion configuration."""

    enabled: bool = False
    sources: dict[str, WebhookSourceConfig] = Field(default_factory=dict)
```

Add `event_maxsize` to `BusConfig`:
```python
class BusConfig(BaseModel):
    """Message bus configuration."""

    inbound_maxsize: int = 2000
    outbound_maxsize: int = 2000
    event_maxsize: int = 100
```

Add to `Config` class fields (around line 487):
```python
    ipc: IpcConfig = Field(default_factory=IpcConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/shared/test_config_ipc_webhooks.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/shared/yeoman_shared/config/schema.py tests/shared/test_config_ipc_webhooks.py
git commit -m "feat(config): add IpcConfig and WebhooksConfig schemas"
```

---

### Task 4: Gateway IPC Socket Server

**Files:**
- Create: `packages/gateway/yeoman_gateway/ipc/__init__.py`
- Create: `packages/gateway/yeoman_gateway/ipc/gateway_socket.py`
- Test: `tests/gateway/test_gateway_socket.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_gateway_socket.py
"""Tests for the gateway IPC socket server."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from yeoman_gateway.ipc.gateway_socket import GatewaySocket


@pytest.mark.asyncio
async def test_send_message_command() -> None:
    sent: list[dict] = []

    async def mock_send(channel: str, chat_id: str, content: str) -> dict:
        sent.append({"channel": channel, "chat_id": chat_id, "content": content})
        return {"status": "ok"}

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(
            path=sock_path,
            send_message_handler=mock_send,
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            request = {"cmd": "send_message", "args": {"channel": "whatsapp", "chat_id": "123", "content": "hello"}}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "ok"
    assert len(sent) == 1
    assert sent[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_unknown_command_returns_error() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(path=sock_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(json.dumps({"cmd": "bogus"}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "error"
    assert "Unknown command" in response["message"]


@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(path=sock_path, rate_limit=2)
        await server.start()
        try:
            responses = []
            for _ in range(4):
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                writer.write(json.dumps({"cmd": "ping"}).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
                responses.append(json.loads(line))
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    ok_count = sum(1 for r in responses if r["status"] == "ok")
    limited_count = sum(1 for r in responses if r["status"] == "error" and "rate" in r.get("message", "").lower())
    assert ok_count == 2
    assert limited_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_gateway_socket.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.ipc'`

- [ ] **Step 3: Create the IPC package and gateway socket server**

Create `packages/gateway/yeoman_gateway/ipc/__init__.py`:
```python
"""Inter-process communication between gateway and overseer."""
```

Create `packages/gateway/yeoman_gateway/ipc/gateway_socket.py`:
```python
"""Unix domain socket server — receives commands from the overseer."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class GatewaySocket:
    """Gateway-side IPC socket server.

    Receives commands from the overseer process via a Unix domain socket.
    Protocol: line-delimited JSON-RPC (same as overseer socket).
    """

    path: Path
    send_message_handler: Callable[..., Awaitable[dict]] | None = None
    trigger_agent_turn_handler: Callable[..., Awaitable[dict]] | None = None
    publish_event_handler: Callable[..., Awaitable[dict]] | None = None
    get_session_state_handler: Callable[..., Awaitable[dict]] | None = None
    rate_limit: int = 10  # commands per second
    _server: asyncio.Server | None = field(default=None, init=False)
    _request_timestamps: list[float] = field(default_factory=list, init=False)

    async def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.path)
        )
        self.path.chmod(0o600)
        logger.info("Gateway IPC socket listening on {}", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.path.exists():
            self.path.unlink()

    def _check_rate_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - 1.0
        self._request_timestamps = [t for t in self._request_timestamps if t > cutoff]
        if len(self._request_timestamps) >= self.rate_limit:
            return False
        self._request_timestamps.append(now)
        return True

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                    if not self._check_rate_limit():
                        response = {"status": "error", "message": "Rate limit exceeded"}
                    else:
                        response = await self._dispatch(request)
                except json.JSONDecodeError:
                    response = {"status": "error", "message": "Invalid JSON"}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd", "")
        args = request.get("args", {})

        if cmd == "ping":
            return {"status": "ok", "response": "pong"}

        if cmd == "send_message" and self.send_message_handler:
            try:
                result = await self.send_message_handler(
                    channel=args.get("channel", ""),
                    chat_id=args.get("chat_id", ""),
                    content=args.get("content", ""),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "trigger_agent_turn" and self.trigger_agent_turn_handler:
            try:
                result = await self.trigger_agent_turn_handler(
                    prompt=args.get("prompt", ""),
                    session_key=args.get("session_key", "overseer:direct"),
                    channel=args.get("channel", "cli"),
                    chat_id=args.get("chat_id", "direct"),
                    model_profile=args.get("model_profile"),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "publish_event" and self.publish_event_handler:
            try:
                result = await self.publish_event_handler(
                    kind=args.get("kind", ""),
                    detail=args.get("detail", {}),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "get_session_state" and self.get_session_state_handler:
            try:
                result = await self.get_session_state_handler(
                    session_key=args.get("session_key", ""),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        return {"status": "error", "message": f"Unknown command: {cmd}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_gateway_socket.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/ipc/ tests/gateway/test_gateway_socket.py
git commit -m "feat(ipc): add gateway socket server for overseer commands"
```

---

### Task 5: IPC Client (Gateway → Overseer)

**Files:**
- Create: `packages/gateway/yeoman_gateway/ipc/overseer_client.py`
- Test: `tests/gateway/test_overseer_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_overseer_client.py
"""Tests for the gateway-side overseer client."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from yeoman_gateway.ipc.overseer_client import OverseerClient


@pytest.mark.asyncio
async def test_ping_succeeds() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "overseer.sock"

        # Spin up a mock overseer socket
        async def mock_handler(reader, writer):
            line = await reader.readline()
            req = json.loads(line)
            if req.get("cmd") == "ping":
                writer.write(json.dumps({"status": "ok", "response": "pong"}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = OverseerClient(socket_path=sock_path)
            result = await client.ping()
            assert result == "pong"
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_connection_failure_returns_error() -> None:
    client = OverseerClient(socket_path=Path("/tmp/nonexistent.sock"), max_retries=1, base_delay=0.01)
    result = await client.send("ping")
    assert result["status"] == "error"
    assert "connect" in result["message"].lower() or "no such file" in result["message"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_overseer_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.ipc.overseer_client'`

- [ ] **Step 3: Implement the client**

Create `packages/gateway/yeoman_gateway/ipc/overseer_client.py`:

```python
"""Client for querying the overseer via its Unix domain socket."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger


class OverseerClient:
    """Gateway-side client for the overseer Unix socket.

    Connects lazily on first use, retries with backoff on failure,
    no persistent connections (connect-per-command).
    """

    def __init__(
        self,
        socket_path: Path,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._path = socket_path
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a command to the overseer and return the response."""
        request = json.dumps({"cmd": cmd, "args": args or {}}).encode() + b"\n"

        for attempt in range(self._max_retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._path))
                try:
                    writer.write(request)
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                    if not line:
                        return {"status": "error", "message": "Empty response from overseer"}
                    return json.loads(line)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._base_delay * (2 ** attempt)
                    logger.debug("Overseer socket attempt {} failed: {}, retrying in {}s", attempt + 1, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Overseer socket unavailable after {} attempts: {}", self._max_retries, e)
                    return {"status": "error", "message": str(e)}

        return {"status": "error", "message": "Unreachable"}

    async def ping(self) -> str | None:
        """Ping the overseer. Returns 'pong' on success, None on failure."""
        result = await self.send("ping")
        if result.get("status") == "ok":
            return result.get("response")
        return None

    async def get_stats(self) -> dict[str, Any]:
        """Get overseer statistics."""
        return await self.send("get_stats")

    async def get_runbook_status(self, name: str | None = None) -> dict[str, Any]:
        """Get runbook status from the overseer."""
        args = {"name": name} if name else {}
        return await self.send("get_runbook_status", args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_overseer_client.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/ipc/overseer_client.py tests/gateway/test_overseer_client.py
git commit -m "feat(ipc): add overseer client with retry and backoff"
```

---

### Task 6: Overseer-Side Gateway Client

**Files:**
- Create: `packages/overseer/yeoman_overseer/gateway/__init__.py`
- Create: `packages/overseer/yeoman_overseer/gateway/client.py`
- Test: `tests/overseer/test_gateway_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/overseer/test_gateway_client.py
"""Tests for the overseer-side gateway client."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from yeoman_overseer.gateway.client import GatewayClient


@pytest.mark.asyncio
async def test_send_message() -> None:
    received_cmds: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"

        async def mock_handler(reader, writer):
            line = await reader.readline()
            req = json.loads(line)
            received_cmds.append(req)
            writer.write(json.dumps({"status": "ok", "response": {}}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = GatewayClient(socket_path=sock_path)
            result = await client.send_message(channel="whatsapp", chat_id="owner", content="Alert!")
            assert result["status"] == "ok"
            assert received_cmds[0]["cmd"] == "send_message"
            assert received_cmds[0]["args"]["content"] == "Alert!"
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_trigger_agent_turn() -> None:
    received_cmds: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"

        async def mock_handler(reader, writer):
            line = await reader.readline()
            received_cmds.append(json.loads(line))
            writer.write(json.dumps({"status": "ok", "response": "done"}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = GatewayClient(socket_path=sock_path)
            result = await client.trigger_agent_turn(prompt="Check disk", session_key="overseer:ops")
            assert result["status"] == "ok"
            assert received_cmds[0]["cmd"] == "trigger_agent_turn"
        finally:
            server.close()
            await server.wait_closed()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/overseer/test_gateway_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_overseer.gateway'`

- [ ] **Step 3: Implement the gateway client**

Create `packages/overseer/yeoman_overseer/gateway/__init__.py`:
```python
"""Gateway communication for overseer."""
```

Create `packages/overseer/yeoman_overseer/gateway/client.py`:

```python
"""Client for sending commands to the gateway via its Unix domain socket."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GatewayClient:
    """Overseer-side client for the gateway Unix socket.

    Connects lazily on first use, retries with backoff, no persistent connections.
    """

    def __init__(
        self,
        socket_path: Path,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._path = socket_path
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def _send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        request = json.dumps({"cmd": cmd, "args": args or {}}).encode() + b"\n"
        for attempt in range(self._max_retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._path))
                try:
                    writer.write(request)
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                    if not line:
                        return {"status": "error", "message": "Empty response"}
                    return json.loads(line)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._base_delay * (2 ** attempt)
                    logger.debug("Gateway socket attempt %d failed: %s, retrying in %ss", attempt + 1, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.warning("Gateway socket unavailable after %d attempts: %s", self._max_retries, e)
                    return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Unreachable"}

    async def send_message(self, *, channel: str, chat_id: str, content: str) -> dict[str, Any]:
        return await self._send("send_message", {"channel": channel, "chat_id": chat_id, "content": content})

    async def trigger_agent_turn(
        self, *, prompt: str, session_key: str = "overseer:direct",
        channel: str = "cli", chat_id: str = "direct", model_profile: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"prompt": prompt, "session_key": session_key, "channel": channel, "chat_id": chat_id}
        if model_profile:
            args["model_profile"] = model_profile
        return await self._send("trigger_agent_turn", args)

    async def publish_event(self, *, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
        return await self._send("publish_event", {"kind": kind, "detail": detail})

    async def notify_runbook_result(
        self, *, runbook_name: str, status: str, summary: str, deliver_to: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"runbook_name": runbook_name, "status": status, "summary": summary}
        if deliver_to:
            args["deliver_to"] = deliver_to
        return await self._send("notify_runbook_result", args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/overseer/test_gateway_client.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add packages/overseer/yeoman_overseer/gateway/ tests/overseer/test_gateway_client.py
git commit -m "feat(overseer): add gateway client for IPC commands"
```

---

### Task 7: Webhook Router

**Files:**
- Create: `packages/gateway/yeoman_gateway/api/webhooks.py`
- Test: `tests/gateway/test_webhooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/gateway/test_webhooks.py
"""Tests for webhook HMAC verification and normalization."""

import hashlib
import hmac
import json
import os
import time

import pytest

from yeoman_gateway.api.webhooks import verify_hmac_signature, normalize_webhook


def test_hmac_valid_signature() -> None:
    secret = "test-secret-123"
    body = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_signature(body, sig, secret) is True


def test_hmac_invalid_signature() -> None:
    assert verify_hmac_signature(b"body", "sha256=bad", "secret") is False


def test_hmac_missing_prefix() -> None:
    assert verify_hmac_signature(b"body", "nope", "secret") is False


def test_normalize_github_push() -> None:
    payload = {
        "repository": {"full_name": "user/repo"},
        "ref": "refs/heads/main",
        "commits": [{"id": "abc"}, {"id": "def"}],
    }
    result = normalize_webhook("github", "push", payload)
    assert "[GitHub]" in result
    assert "user/repo" in result
    assert "2 commit(s)" in result
    assert "main" in result


def test_normalize_github_pull_request() -> None:
    payload = {
        "repository": {"full_name": "user/repo"},
        "action": "opened",
        "pull_request": {"number": 42, "title": "Fix bug"},
    }
    result = normalize_webhook("github", "pull_request.opened", payload)
    assert "PR #42" in result
    assert "opened" in result


def test_normalize_unknown_source_truncates() -> None:
    payload = {"data": "x" * 2000}
    result = normalize_webhook("custom", "event", payload)
    assert "[Webhook: custom]" in result
    assert "...[truncated]" in result
    assert len(result) < 1200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/gateway/test_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'yeoman_gateway.api.webhooks'`

- [ ] **Step 3: Implement the webhook module**

Create `packages/gateway/yeoman_gateway/api/webhooks.py`:

```python
"""Webhook ingestion for external event sources."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from fastapi import APIRouter

    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_shared.config.schema import WebhooksConfig


def verify_hmac_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature. Returns False on any mismatch."""
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def normalize_webhook(source: str, event_type: str, payload: dict[str, Any]) -> str:
    """Normalize a webhook payload to a human-readable string for the LLM."""
    if source == "github":
        return _normalize_github(event_type, payload)
    raw = json.dumps(payload, indent=2, default=str)
    if len(raw) > 1000:
        raw = raw[:1000] + "\n...[truncated]"
    return f"[Webhook: {source}] event={event_type}\n{raw}"


def _normalize_github(event_type: str, payload: dict[str, Any]) -> str:
    repo = payload.get("repository", {}).get("full_name", "unknown")
    action = payload.get("action", "")
    if event_type == "push":
        ref = payload.get("ref", "").replace("refs/heads/", "")
        count = len(payload.get("commits", []))
        return f"[GitHub] {repo}: {count} commit(s) pushed to {ref}"
    if event_type.startswith("pull_request"):
        pr = payload.get("pull_request", {})
        return f"[GitHub] {repo}: PR #{pr.get('number')} {action}: {pr.get('title', '')}"
    return f"[GitHub] {repo}: {event_type} {action}"


def create_webhook_router(
    webhooks_config: "WebhooksConfig",
    bus: "MessageBus",
) -> "APIRouter":
    """Create the webhook FastAPI router."""
    from fastapi import APIRouter, HTTPException, Request

    from yeoman_gateway.api.server import _check_rate_limit
    from yeoman_gateway.bus.events import WebhookEvent

    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/{source}")
    async def receive_webhook(source: str, request: Request) -> dict[str, str]:
        if not webhooks_config.enabled:
            raise HTTPException(status_code=404)

        source_config = webhooks_config.sources.get(source)
        if not source_config:
            raise HTTPException(status_code=404)

        # Rate limit per source
        rate_key = f"webhook:{source}"
        allowed, _ = _check_rate_limit(rate_key, source_config.rate_limit)
        if not allowed:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        # HMAC verification
        secret = os.environ.get(source_config.secret_env, "")
        if not secret:
            logger.error("Webhook secret env var {} not set for source {}", source_config.secret_env, source)
            raise HTTPException(status_code=404)

        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_hmac_signature(body, signature, secret):
            logger.warning("Webhook HMAC verification failed for source={}", source)
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse payload
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        # Event type filtering
        event_type = request.headers.get("X-GitHub-Event", payload.get("event_type", "unknown"))
        if source_config.allowed_events is not None:
            if event_type not in source_config.allowed_events:
                return {"status": "filtered"}

        # Publish to event bus
        event = WebhookEvent(
            source=source,
            event_type=event_type,
            payload=payload,
            signature_verified=True,
            received_at=time.time(),
        )
        await bus.publish_event(event)
        return {"status": "accepted"}

    return router
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_webhooks.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/api/webhooks.py tests/gateway/test_webhooks.py
git commit -m "feat(api): add webhook router with HMAC verification and normalization"
```

---

### Task 8: Wire Everything in Bootstrap

**Files:**
- Modify: `packages/gateway/yeoman_gateway/app/bootstrap.py`
- Modify: `packages/gateway/yeoman_gateway/api/server.py`

- [ ] **Step 1: Mount webhook router in server.py**

In `packages/gateway/yeoman_gateway/api/server.py`, update `create_app` signature to accept `bus` and `webhooks_config`:

```python
def create_app(
    config: Config,
    channel_manager: ChannelManager | None = None,
    telemetry: TelemetryPort | None = None,
    api_config: APIConfig | None = None,
    bus: "MessageBus | None" = None,
) -> "FastAPI":
```

Add after `app = FastAPI(...)` block (after line 146):

```python
    # Mount webhook router if configured and bus available
    if bus and config.webhooks.enabled:
        from yeoman_gateway.api.webhooks import create_webhook_router

        webhook_router = create_webhook_router(config.webhooks, bus)
        app.include_router(webhook_router)
        logger.info("Webhook router mounted with {} source(s)", len(config.webhooks.sources))
```

- [ ] **Step 2: Wire event bus and IPC socket in bootstrap.py**

In `build_gateway_runtime()`, pass `event_maxsize` to `MessageBus` (find where `bus` is constructed by the caller — it's passed in, so this is done at the call site in `cli/gateway_commands.py` or similar).

In `GatewayRuntime`, add the gateway socket field and update `run()`:

```python
@dataclass(slots=True)
class GatewayRuntime:
    orchestrator: OrchestratorService
    channels: ChannelManager
    cron: CronService
    heartbeat: HeartbeatService
    inbound_archive: InboundArchive
    responder: LLMResponder
    memory: MemoryService
    contacts: ContactsService
    gateway_socket: "GatewaySocket | None" = None

    async def run(self) -> None:
        tracing.init()
        try:
            await self.cron.start()
            await self.heartbeat.start()
            if self.gateway_socket:
                await self.gateway_socket.start()
            await asyncio.gather(
                self.orchestrator.run(),
                self.channels.start_all(),
                self._run_bus_dispatch(),
            )
        finally:
            if self.gateway_socket:
                await self.gateway_socket.stop()
            self.heartbeat.stop()
            self.cron.stop()
            self.orchestrator.stop()
            await self.channels.stop_all()
            await self.responder.aclose()
            self.inbound_archive.close()
            self.contacts.close()
            self.memory.close()
            await tracing.shutdown()
```

At the bottom of `build_gateway_runtime()`, create the gateway socket:

```python
    # IPC socket for overseer commands
    from yeoman_gateway.ipc.gateway_socket import GatewaySocket

    ipc_config = config.ipc
    socket_path = Path(ipc_config.gateway_socket_path).expanduser()

    async def ipc_send_message(channel: str, chat_id: str, content: str) -> dict:
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=content))
        return {"delivered": True}

    async def ipc_trigger_agent_turn(prompt: str, session_key: str, channel: str, chat_id: str, model_profile: str | None = None) -> dict:
        response = await responder.process_direct(prompt, session_key=session_key, channel=channel, chat_id=chat_id, model_profile=model_profile)
        return {"response": response}

    async def ipc_publish_event(kind: str, detail: dict) -> dict:
        from yeoman_gateway.bus.events import SystemEvent
        await bus.publish_event(SystemEvent(kind=kind, detail=detail, timestamp=time.time()))
        return {"published": True}

    gateway_socket = GatewaySocket(
        path=socket_path,
        send_message_handler=ipc_send_message,
        trigger_agent_turn_handler=ipc_trigger_agent_turn,
        publish_event_handler=ipc_publish_event,
        rate_limit=ipc_config.command_rate_limit,
    )
```

Pass `gateway_socket` to `GatewayRuntime`:

```python
    return GatewayRuntime(
        ...,
        gateway_socket=gateway_socket,
    )
```

- [ ] **Step 3: Run all tests to verify nothing broke**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -x -q`
Expected: All previously passing tests still pass

- [ ] **Step 4: Commit**

```bash
git add packages/gateway/yeoman_gateway/app/bootstrap.py packages/gateway/yeoman_gateway/api/server.py
git commit -m "feat: wire event bus, IPC socket, and webhooks into gateway runtime"
```

---

### Task 9: Add get_runbook_status to Overseer Socket

**Files:**
- Modify: `packages/overseer/yeoman_overseer/socket/server.py`
- Test: `tests/overseer/test_overseer_socket.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/overseer/test_overseer_socket.py
"""Tests for expanded overseer socket commands."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from yeoman_overseer.socket.server import OverseerSocket


@pytest.mark.asyncio
async def test_get_runbook_status() -> None:
    def mock_stats() -> dict:
        return {"uptime": 100}

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "overseer.sock"
        server = OverseerSocket(
            path=sock_path,
            stats_callback=mock_stats,
            runbook_status_callback=lambda name: {"runbooks": [{"name": "test", "last_status": "ok"}]},
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(json.dumps({"cmd": "get_runbook_status", "args": {}}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "ok"
    assert "runbooks" in response["response"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/overseer/test_overseer_socket.py::test_get_runbook_status -v`
Expected: FAIL — `TypeError: OverseerSocket.__init__() got an unexpected keyword argument 'runbook_status_callback'`

- [ ] **Step 3: Add runbook_status_callback to OverseerSocket**

In `packages/overseer/yeoman_overseer/socket/server.py`, add the callback field and dispatch:

```python
@dataclass
class OverseerSocket:
    path: Path
    stats_callback: Callable[[], dict[str, Any]]
    runbook_status_callback: Callable[[str | None], dict[str, Any]] | None = None
    _server: asyncio.Server | None = field(default=None, init=False)
```

Update `_dispatch`:

```python
    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd", "")
        args = request.get("args", {})
        if cmd == "ping":
            return {"status": "ok", "response": "pong"}
        elif cmd == "get_stats":
            return {"status": "ok", "response": self.stats_callback()}
        elif cmd == "get_runbook_status" and self.runbook_status_callback:
            name = args.get("name") if isinstance(args, dict) else None
            return {"status": "ok", "response": self.runbook_status_callback(name)}
        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/overseer/test_overseer_socket.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add packages/overseer/yeoman_overseer/socket/server.py tests/overseer/test_overseer_socket.py
git commit -m "feat(overseer): add get_runbook_status command to socket server"
```

---

### Task 10: Integration Test — End-to-End

**Files:**
- Test: `tests/gateway/test_event_backbone_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/gateway/test_event_backbone_integration.py
"""Integration test: webhook -> event bus -> handler."""

import asyncio
import time

import pytest

from yeoman_gateway.bus.events import WebhookEvent
from yeoman_gateway.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_webhook_event_through_bus() -> None:
    """Simulate: webhook publishes event -> bus dispatches -> handler receives."""
    bus = MessageBus(event_maxsize=10)
    received: list[WebhookEvent] = []

    async def webhook_handler(ev: WebhookEvent) -> None:
        received.append(ev)

    bus.subscribe_event("WebhookEvent", webhook_handler)

    # Simulate webhook publishing an event
    event = WebhookEvent(
        source="github",
        event_type="push",
        payload={"repository": {"full_name": "user/repo"}, "ref": "refs/heads/main", "commits": [{}]},
        signature_verified=True,
        received_at=time.time(),
    )
    await bus.publish_event(event)

    # Start dispatch loop briefly
    dispatch = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.1)
    bus.stop()
    await dispatch

    assert len(received) == 1
    assert received[0].source == "github"
    assert received[0].event_type == "push"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run python -m pytest tests/gateway/test_event_backbone_integration.py -v`
Expected: 1 passed

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/shared/ tests/gateway/ tests/overseer/ -q`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/gateway/test_event_backbone_integration.py
git commit -m "test: add event backbone integration test"
```
