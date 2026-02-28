"""Process and health supervision adapter for WhatsApp bridge runtime."""

from __future__ import annotations

from nanobot.channels.whatsapp_runtime import (
    BridgeReadyReport,
    BridgeStatus,
    WhatsAppRuntimeManager,
)


class BridgeProcessSupervisor:
    """Thin process supervisor split from artifact lifecycle concerns."""

    def __init__(self, manager: WhatsAppRuntimeManager) -> None:
        self._manager = manager

    def status(self) -> BridgeStatus:
        return self._manager.status_bridge()

    def start(self) -> BridgeStatus:
        return self._manager.start_bridge()

    def stop(self) -> int:
        return self._manager.stop_bridge()

    def restart(self) -> BridgeStatus:
        return self._manager.restart_bridge()

    def health(self, timeout_s: float) -> dict[str, object]:
        return self._manager.health_check(timeout_s)

    def ensure_ready(self, *, auto_repair: bool, start_if_needed: bool) -> BridgeReadyReport:
        return self._manager.ensure_ready(
            auto_repair=auto_repair,
            start_if_needed=start_if_needed,
        )
