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
    from yeoman_shared.config.schema import WebhooksConfig

    from yeoman_gateway.bus.queue import MessageBus


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
            logger.error(
                "Webhook secret env var {} not set for source {}",
                source_config.secret_env,
                source,
            )
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
