"""Typed vNext orchestrator pipeline.

Thin wrapper that constructs a :class:`Pipeline` from middleware classes
and delegates :meth:`handle` to it.  The constructor signature is unchanged
from the monolithic version so all existing callers (bootstrap, tests) work
without modification.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yeoman_gateway.core.admin_commands import AdminCommandResult
from yeoman_gateway.core.intents import OrchestratorIntent
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import Pipeline
from yeoman_gateway.core.ports import PolicyPort, ReplyArchivePort, ResponderPort, SecurityPort
from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
from yeoman_gateway.pipeline.admin import AdminCommandMiddleware
from yeoman_gateway.pipeline.archive import ArchiveMiddleware
from yeoman_gateway.pipeline.contacts import ContactsMiddleware
from yeoman_gateway.pipeline.dedup import DeduplicationMiddleware
from yeoman_gateway.pipeline.idea_capture import IdeaCaptureMiddleware
from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
from yeoman_gateway.pipeline.normalize import NormalizationMiddleware
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_gateway.pipeline.policy import PolicyMiddleware
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.pipeline.responder import ResponderMiddleware
from yeoman_gateway.pipeline.security_input import InputSecurityMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from yeoman_gateway.contacts.service import ContactsService
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_gateway.media.tts import TTSSynthesizer
    from yeoman_gateway.security.classifier import InputClassifier


class Orchestrator:
    """Deterministic pipeline for inbound processing.

    Constructs a :class:`Pipeline` from middleware classes internally.
    The public interface is unchanged: call :meth:`handle` with an
    :class:`InboundEvent` and receive a list of :class:`OrchestratorIntent`.
    """

    def __init__(
        self,
        *,
        policy: PolicyPort,
        responder: ResponderPort,
        reply_archive: ReplyArchivePort | None = None,
        contacts: "ContactsService | None" = None,
        reply_context_window_limit: int,
        reply_context_line_max_chars: int,
        ambient_window_limit: int = 8,
        dedupe_ttl_seconds: int = 20 * 60,
        typing_notifier: "Callable[[str, str, bool], Awaitable[None]] | None" = None,
        security: SecurityPort | None = None,
        security_classifier: "InputClassifier | None" = None,
        security_block_message: str = "😂",
        policy_admin_handler: "Callable[[InboundEvent], AdminCommandResult | str | None] | None" = None,
        model_router: "ModelRouter | None" = None,
        tts: "TTSSynthesizer | None" = None,
        whatsapp_tts_outgoing_dir: Path | None = None,
        whatsapp_tts_max_raw_bytes: int = 160 * 1024,
        owner_alert_resolver: "Callable[[str], list[str]] | None" = None,
        owner_alert_cooldown_seconds: int = 300,
    ) -> None:
        layers: list = [
            NormalizationMiddleware(),
            DeduplicationMiddleware(ttl_seconds=dedupe_ttl_seconds),
            ArchiveMiddleware(archive=reply_archive),
        ]
        if contacts is not None:
            layers.append(ContactsMiddleware(contacts=contacts))
        layers.extend([
            ReplyContextMiddleware(
                archive=reply_archive,
                contacts=contacts,
                reply_context_window_limit=reply_context_window_limit,
                reply_context_line_max_chars=reply_context_line_max_chars,
                ambient_window_limit=ambient_window_limit,
            ),
            AdminCommandMiddleware(handler=policy_admin_handler),
            PolicyMiddleware(policy=policy),
            IdeaCaptureMiddleware(security=security),
            AccessControlMiddleware(security=security),
            NewChatNotifyMiddleware(owner_alert_resolver=owner_alert_resolver),
            NoReplyFilterMiddleware(security=security),
            InputSecurityMiddleware(security=security, classifier=security_classifier, block_message=security_block_message),
            ResponderMiddleware(responder=responder, typing_notifier=typing_notifier),
            OutboundMiddleware(
                contacts=contacts,
                security=security,
                security_block_message=security_block_message,
                tts=tts,
                whatsapp_tts_outgoing_dir=whatsapp_tts_outgoing_dir,
                whatsapp_tts_max_raw_bytes=whatsapp_tts_max_raw_bytes,
                model_router=model_router,
                owner_alert_resolver=owner_alert_resolver,
                owner_alert_cooldown_seconds=owner_alert_cooldown_seconds,
            ),
        ])
        self._pipeline = Pipeline(layers)

    async def handle(self, event: InboundEvent) -> list[OrchestratorIntent]:
        """Process one inbound event and return executable intents."""
        return await self._pipeline.run(event)
