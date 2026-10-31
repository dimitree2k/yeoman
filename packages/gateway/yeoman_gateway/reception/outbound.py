"""Best-effort outbound metadata for reception measurement.

This runs after a managed effect has a provider receipt. It must never block, delay or
fail a send: every failure is swallowed and logged at debug.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from yeoman_gateway.processing.models import EffectEnvelope
from yeoman_gateway.reception.store import SentimentStore

OWNER_COMMAND_PREFIXES = ("admin:", "ipc:", "owner:", "approval:")


def _provider_id(result: Any) -> str:
    transport = getattr(result, "transport_receipt", None)
    return str(getattr(transport, "provider_message_id", "") or "")


def _path_for(envelope: EffectEnvelope, admission: Any) -> str:
    if admission is not None:
        return "participation"
    trace = str(getattr(envelope, "trace_id", "") or "")
    operation = str(getattr(envelope, "operation_key", "") or "")
    if trace.startswith(OWNER_COMMAND_PREFIXES) or operation.startswith(OWNER_COMMAND_PREFIXES):
        return "owner_command"
    payload = getattr(envelope, "payload", None)
    if getattr(payload, "reply_to", None):
        return "direct_reply"
    return "other"


class ReceptionOutboundRecorder:
    """Record one sent Arvid message for an enabled chat, best effort."""

    def __init__(
        self,
        *,
        store: SentimentStore,
        enabled: Callable[[str], bool],
        persona_sha: Callable[[str], str],
    ) -> None:
        self._store = store
        self._enabled = enabled
        self._persona_sha = persona_sha

    def __call__(
        self, envelope: EffectEnvelope, admission: Any, result: Any, now_ms: int
    ) -> None:
        try:
            chat_id = str(getattr(getattr(envelope, "target", None), "chat_id", "") or "")
            if not chat_id or not self._enabled(chat_id):
                return
            if str(getattr(getattr(envelope, "payload", None), "kind", "")) != "text":
                return
            provider_id = _provider_id(result)
            if not provider_id:
                return
            action = "unknown"
            if admission is not None:
                action = str(
                    getattr(admission, "contribution_type", "")
                    or getattr(admission, "action", "")
                    or "unknown"
                )
            try:
                sha = str(self._persona_sha(chat_id) or "unknown")
            except Exception:  # noqa: BLE001 - metadata is optional
                sha = "unknown"
            self._store.record_outbound(
                chat_id=chat_id,
                message_id=provider_id,
                sent_ms=int(now_ms),
                path=_path_for(envelope, admission),
                action_type=action,
                persona_sha=sha,
            )
            if sha != "unknown":
                self._store.note_persona(
                    chat_id=chat_id, persona_sha=sha, now_ms=int(now_ms)
                )
        except Exception as exc:  # noqa: BLE001 - must never affect the send
            logger.debug(
                "reception_outbound_meta_skipped error_type={}", type(exc).__name__
            )


__all__ = ["ReceptionOutboundRecorder"]
