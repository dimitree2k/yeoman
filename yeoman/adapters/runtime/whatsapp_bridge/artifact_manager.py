"""Artifact lifecycle adapter for WhatsApp bridge runtime."""

from __future__ import annotations

from pathlib import Path

from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager


class BridgeArtifactManager:
    """Thin artifact manager split from process supervision concerns."""

    def __init__(self, manager: WhatsAppRuntimeManager) -> None:
        self._manager = manager

    def ensure_runtime(self) -> Path:
        return self._manager.ensure_runtime()
