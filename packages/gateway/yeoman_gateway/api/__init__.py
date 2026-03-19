"""Control Plane API for yeoman.

Provides a minimal FastAPI-based control plane with:
- /health - Health check endpoint
- /status - System status
- /channels - Channel management
- /config/reload - Configuration reload
- /metrics - Prometheus metrics (if enabled)

Security: Token auth required from day one. Rate limiting on all endpoints.
"""

from __future__ import annotations

__all__ = ["create_app", "run_server"]
