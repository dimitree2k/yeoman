"""One-shot request/response client for the local WhatsApp bridge (CLI use only)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION


def bridge_command(
    command_type: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_s: float = 10.0,
    account_id: str = "default",
) -> dict[str, Any]:
    """Send one command to the bridge and return its ``result``; raises on any failure."""
    from websockets.sync.client import connect
    from yeoman_shared.config.loader import load_config

    wa = load_config().channels.whatsapp
    token = (wa.bridge_token or "").strip()
    if not token:
        raise RuntimeError("channels.whatsapp.bridgeToken is not set")
    request_id = uuid.uuid4().hex
    envelope = {
        "version": PROTOCOL_VERSION,
        "type": command_type,
        "token": token,
        "requestId": request_id,
        "accountId": account_id,
        "payload": payload or {},
    }
    with connect(wa.resolved_bridge_url, open_timeout=5.0, max_size=wa.max_payload_bytes) as ws:
        ws.send(json.dumps(envelope))
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"bridge {command_type} timed out")
            data = json.loads(ws.recv(timeout=left))
            if not isinstance(data, dict) or data.get("type") != "response":
                continue
            if data.get("requestId") != request_id:
                continue
            response = data.get("payload")
            if not isinstance(response, dict) or not response.get("ok"):
                error = response.get("error") if isinstance(response, dict) else response
                raise RuntimeError(f"bridge {command_type} failed: {error}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}
