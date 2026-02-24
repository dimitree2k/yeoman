"""CLI command registration and compatibility exports."""

from __future__ import annotations

# Import command modules for side-effect registration on the shared `app`.
from . import agent_commands as _agent_commands  # noqa: F401
from . import channel_commands as _channel_commands  # noqa: F401
from . import chat_commands as _chat_commands  # noqa: F401
from . import config_commands as _config_commands  # noqa: F401
from . import cron_commands as _cron_commands  # noqa: F401
from . import memory_commands as _memory_commands  # noqa: F401
from . import policy_commands as _policy_commands  # noqa: F401
from . import status_commands as _status_commands  # noqa: F401
from .core import app, console
from .gateway_commands import _stop_gateway_processes

__all__ = ["app", "console", "_stop_gateway_processes"]

if __name__ == "__main__":
    app()
