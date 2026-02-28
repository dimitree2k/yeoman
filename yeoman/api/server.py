"""FastAPI-based Control Plane API for yeoman.

Minimal endpoints for operational visibility and control:
- GET /health - Health check (no auth required)
- GET /status - System status (auth required)
- GET /channels - Channel status (auth required)
- POST /config/reload - Reload configuration (auth required)
- GET /metrics - Prometheus metrics (auth required)

Security:
- Token auth via Bearer header (except /health)
- Rate limiting on all endpoints
- Audit logging for admin operations
"""

from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from yeoman.channels.manager import ChannelManager
    from yeoman.config.loader import Config
    from yeoman.telemetry.base import TelemetryPort


@dataclass
class APIConfig:
    """Configuration for the Control Plane API."""

    enabled: bool = True
    host: str = "127.0.0.1"  # localhost only by default
    port: int = 8080
    auth_token: str | None = None  # Required for all endpoints except /health
    rate_limit_per_minute: int = 60


@dataclass
class AppState:
    """Shared state for the API."""

    config: Config
    channel_manager: ChannelManager | None = None
    telemetry: TelemetryPort | None = None
    start_time: float = 0.0
    request_count: int = 0


# Global state (set during lifespan)
_state: AppState | None = None


def _check_auth(auth_header: str | None, expected_token: str) -> bool:
    """Validate Bearer token auth."""
    if not auth_header:
        return False
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]  # Remove "Bearer " prefix
    return hmac.compare_digest(token, expected_token)


def _rate_limit_key(request: Any) -> str:
    """Extract rate limit key from request."""
    # Use client IP as rate limit key
    if hasattr(request, "client") and request.client:
        return request.client.host
    return "unknown"


# Rate limiting state (simple in-memory, per-IP)
_rate_limit_state: dict[str, list[float]] = {}


def _check_rate_limit(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """Check if request is within rate limit.

    Returns (allowed, remaining_requests).
    """
    now = time.monotonic()
    window_start = now - window_seconds

    # Clean old entries
    if key in _rate_limit_state:
        _rate_limit_state[key] = [t for t in _rate_limit_state[key] if t > window_start]
    else:
        _rate_limit_state[key] = []

    current_count = len(_rate_limit_state[key])
    if current_count >= limit:
        return False, 0

    _rate_limit_state[key].append(now)
    return True, limit - current_count - 1


def create_app(
    config: Config,
    channel_manager: ChannelManager | None = None,
    telemetry: TelemetryPort | None = None,
    api_config: APIConfig | None = None,
) -> "FastAPI":
    """Create the FastAPI application.

    Args:
        config: Nanobot configuration
        channel_manager: Optional channel manager for status
        telemetry: Optional telemetry backend for metrics
        api_config: API-specific configuration

    Returns:
        FastAPI application instance
    """
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    api_config = api_config or APIConfig()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _state
        _state = AppState(
            config=config,
            channel_manager=channel_manager,
            telemetry=telemetry,
            start_time=time.monotonic(),
        )
        logger.info(f"Control Plane API starting on http://{api_config.host}:{api_config.port}")
        yield
        logger.info("Control Plane API shutting down")
        _state = None

    app = FastAPI(
        title="Nanobot Control Plane",
        description="Minimal control plane API for yeoman operations",
        version="0.1.0",
        lifespan=lifespan,
    )

    security = HTTPBearer(auto_error=False)

    def verify_auth(request: Request, credentials: HTTPAuthorizationCredentials | None = None) -> None:
        """Verify authentication for protected endpoints."""
        if not api_config.auth_token:
            # No auth token configured - allow all (development mode)
            return

        auth_header = request.headers.get("Authorization")
        if not _check_auth(auth_header, api_config.auth_token):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def check_rate_limit(request: Request) -> None:
        """Check rate limit for request."""
        key = _rate_limit_key(request)
        allowed, remaining = _check_rate_limit(key, api_config.rate_limit_per_minute)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
            )

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint (no auth required)."""
        return {"status": "ok"}

    @app.get("/status", tags=["status"])
    async def get_status(request: Request) -> dict[str, Any]:
        """Get system status."""
        verify_auth(request)
        check_rate_limit(request)

        if _state is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        uptime = time.monotonic() - _state.start_time

        status = {
            "status": "running",
            "uptime_seconds": round(uptime, 2),
            "version": "0.1.0",
            "timestamp": datetime.now(UTC).isoformat(),
            "channels": {},
        }

        if _state.channel_manager:
            try:
                channels = _state.channel_manager.list_channels()
                status["channels"] = {
                    name: {"status": "active" if ch.is_running else "stopped"}
                    for name, ch in channels.items()
                }
            except Exception as e:
                logger.warning(f"Failed to get channel status: {e}")
                status["channels"] = {"error": str(e)}

        return status

    @app.get("/channels", tags=["channels"])
    async def list_channels(request: Request) -> dict[str, Any]:
        """List all channels and their status."""
        verify_auth(request)
        check_rate_limit(request)

        if _state is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        if not _state.channel_manager:
            return {"channels": {}, "message": "Channel manager not available"}

        try:
            channels = _state.channel_manager.list_channels()
            return {
                "channels": {
                    name: {
                        "status": "active" if ch.is_running else "stopped",
                        "type": type(ch).__name__,
                    }
                    for name, ch in channels.items()
                }
            }
        except Exception as e:
            logger.error(f"Failed to list channels: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/config/reload", tags=["config"])
    async def reload_config(request: Request) -> dict[str, str]:
        """Reload configuration from disk."""
        verify_auth(request)
        check_rate_limit(request)

        if _state is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        # Log the reload attempt
        client_ip = request.client.host if request.client else "unknown"
        logger.info(f"Config reload requested from {client_ip}")

        try:
            # Reload config from disk
            from yeoman.config.loader import load_config

            new_config = load_config()
            _state.config = new_config
            logger.info("Configuration reloaded successfully")
            return {"status": "ok", "message": "Configuration reloaded"}
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to reload config: {e}")

    @app.get("/metrics", tags=["metrics"])
    async def get_metrics(request: Request) -> Response:
        """Get Prometheus metrics."""
        verify_auth(request)
        check_rate_limit(request)

        if _state is None:
            raise HTTPException(status_code=503, detail="Service not ready")

        # If we have a PrometheusTelemetry backend, use it
        if _state.telemetry:
            try:
                from yeoman.telemetry.prometheus import PrometheusTelemetry

                if isinstance(_state.telemetry, PrometheusTelemetry):
                    from prometheus_client import generate_latest

                    return Response(
                        content=generate_latest(),
                        media_type="text/plain; version=0.0.4; charset=utf-8",
                    )
            except ImportError:
                pass

        # Fallback: return basic metrics
        return Response(
            content="# Prometheus client not available\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


def run_server(
    config: Config,
    channel_manager: ChannelManager | None = None,
    telemetry: TelemetryPort | None = None,
    api_config: APIConfig | None = None,
) -> None:
    """Run the Control Plane API server.

    This is a blocking call that runs the server until interrupted.

    Args:
        config: Nanobot configuration
        channel_manager: Optional channel manager
        telemetry: Optional telemetry backend
        api_config: API-specific configuration
    """
    import uvicorn

    api_config = api_config or APIConfig()

    if not api_config.enabled:
        logger.info("Control Plane API disabled")
        return

    app = create_app(config, channel_manager, telemetry, api_config)

    uvicorn.run(
        app,
        host=api_config.host,
        port=api_config.port,
        log_level="info",
    )
