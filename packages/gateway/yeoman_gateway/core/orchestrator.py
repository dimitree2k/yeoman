"""Typed vNext orchestrator pipeline.

Thin wrapper that constructs a :class:`Pipeline` from middleware classes
and delegates :meth:`handle` to it.  WhatsApp readers require the projected History scope supplied by their caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from yeoman_gateway.core.admin_commands import AdminCommandResult
from yeoman_gateway.core.intents import OrchestratorIntent
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import Middleware, Pipeline
from yeoman_gateway.core.ports import PolicyPort, ResponderPort, SecurityPort
from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
from yeoman_gateway.pipeline.admin import AdminCommandMiddleware
from yeoman_gateway.pipeline.approval import ApprovalMiddleware
from yeoman_gateway.pipeline.contacts import ContactsMiddleware
from yeoman_gateway.pipeline.dedup import DeduplicationMiddleware
from yeoman_gateway.pipeline.forward import (
    ForwardCommandMiddleware,
    ForwardSourceLookup,
    ForwardTargetResolver,
)
from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware
from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
from yeoman_gateway.pipeline.normalize import NormalizationMiddleware
from yeoman_gateway.pipeline.outbound import OutboundMiddleware
from yeoman_gateway.pipeline.persona_evolution_approval import PersonaEvolutionApprovalMiddleware
from yeoman_gateway.pipeline.policy import PolicyMiddleware
from yeoman_gateway.pipeline.reply_budget import ReplyBudgetMiddleware
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.pipeline.responder import ResponderMiddleware
from yeoman_gateway.pipeline.security_input import InputSecurityMiddleware
from yeoman_gateway.pipeline.speakup_approval import SpeakupApprovalMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.core.control_ack import OwnerControlAcknowledgements
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_gateway.media.tts import TTSSynthesizer
    from yeoman_gateway.security.classifier import InputClassifier
    from yeoman_gateway.session.manager import SessionManager


def _global_pause_from_policy(policy: object) -> "Callable[[str, str], str | None] | None":
    """Derive the owner global-pause probe from the mandatory policy port.

    The first-contact notifier is constructed here, so the fence cannot depend on a
    call site remembering to pass it: any composition whose policy port exposes the
    existing pause read API is fenced by default. Only the global member is promoted;
    chat-scoped pauses stay an admission concern.
    """
    probe = getattr(policy, "participation_pause_reason", None)
    if not callable(probe):
        return None

    def _probe(channel: str, chat_id: str) -> str | None:
        reason = probe(channel, chat_id)
        return reason if reason == "paused_global" else None

    return _probe


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
        knowledge: object | None = None,
        reply_context_window_limit: int,
        reply_context_line_max_chars: int,
        ambient_window_limit: int = 30,
        dedupe_ttl_seconds: int = 20 * 60,
        typing_notifier: "Callable[[str, str, bool], Awaitable[None]] | None" = None,
        reply_admission: "Callable[[InboundEvent], bool] | None" = None,
        report_lookup: "Callable[[InboundEvent], str | None] | None" = None,
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
        allowed_reaction_emojis: Sequence[str] | None = None,
        workflow_state: "WorkflowState | None" = None,
        approval_trigger: "Callable[[PendingApproval], Awaitable[None]] | None" = None,
        bus: "MessageBus | None" = None,
        speakup_approval_store: "SpeakupApprovalStore | None" = None,
        speakup_log: "SpeakupLog | None" = None,
        speakup_tools: "ConsciousnessTools | None" = None,
        persona_evolution_workspace: Path | None = None,
        persona_evolution_state_db_path: Path | None = None,
        session_manager: "SessionManager | None" = None,
        service_effects: object | None = None,
        forward_target_resolver: ForwardTargetResolver | None = None,
        forward_source_lookup: ForwardSourceLookup | None = None,
        short_reply_reactor: object | None = None,
        global_pause_probe: "Callable[[str, str], str | None] | None" = None,
        control_acknowledgements: "OwnerControlAcknowledgements | None" = None,
    ) -> None:
        if global_pause_probe is None:
            global_pause_probe = _global_pause_from_policy(policy)
        layers: list[Middleware] = [
            NormalizationMiddleware(),
            DeduplicationMiddleware(ttl_seconds=dedupe_ttl_seconds),
        ]
        layers.append(ContactsMiddleware())
        layers.extend([
            ReplyContextMiddleware(
                reply_context_window_limit=reply_context_window_limit,
                reply_context_line_max_chars=reply_context_line_max_chars,
                ambient_window_limit=ambient_window_limit,
            ),
            AdminCommandMiddleware(
                handler=policy_admin_handler,
                acknowledgements=control_acknowledgements,
            ),
            PolicyMiddleware(policy=policy),
            ForwardCommandMiddleware(
                target_resolver=forward_target_resolver,
                source_lookup=forward_source_lookup,
            ),
            ImplicitBotAddressMiddleware(
                session_manager=session_manager,
                short_reply_reactor=short_reply_reactor,  # type: ignore[arg-type]
            ),
            ReplyBudgetMiddleware(),
        ])
        if bus is not None and speakup_approval_store is not None and speakup_log is not None and security is not None:
            layers.append(
                SpeakupApprovalMiddleware(
                    approval_store=speakup_approval_store,
                    bus=bus,
                    log=speakup_log,
                    security=security,
                    service_effects=service_effects,
                    tools=speakup_tools,
                )
            )
        if (
            bus is not None
            and persona_evolution_workspace is not None
            and persona_evolution_state_db_path is not None
        ):
            layers.append(
                PersonaEvolutionApprovalMiddleware(
                    workspace=persona_evolution_workspace,
                    state_db_path=persona_evolution_state_db_path,
                    bus=bus,
                    service_effects=service_effects,
                )
            )
        if workflow_state and approval_trigger:
            layers.append(ApprovalMiddleware(workflow_state=workflow_state, trigger_callback=approval_trigger))
        layers.extend([
            AccessControlMiddleware(),
            NewChatNotifyMiddleware(
                owner_alert_resolver=owner_alert_resolver,
                global_pause=global_pause_probe,
            ),
            NoReplyFilterMiddleware(),
            InputSecurityMiddleware(security=security, classifier=security_classifier, block_message=security_block_message),
            ResponderMiddleware(
                responder=responder,
                typing_notifier=typing_notifier,
                reply_admission=reply_admission,
                report_lookup=report_lookup,
            ),
            OutboundMiddleware(
                knowledge=knowledge,
                security=security,
                security_block_message=security_block_message,
                tts=tts,
                whatsapp_tts_outgoing_dir=whatsapp_tts_outgoing_dir,
                whatsapp_tts_max_raw_bytes=whatsapp_tts_max_raw_bytes,
                model_router=model_router,
                owner_alert_resolver=owner_alert_resolver,
                owner_alert_cooldown_seconds=owner_alert_cooldown_seconds,
                allowed_reaction_emojis=allowed_reaction_emojis,
            ),
        ])
        self._pipeline = Pipeline(layers)

    async def handle(self, event: InboundEvent) -> list[OrchestratorIntent]:
        """Process one inbound event and return executable intents."""
        if event.channel == 'whatsapp':
            from yeoman_gateway.history.context import current_history_snapshot
            from yeoman_gateway.history.live import HistoryPaused
            if current_history_snapshot() is None:
                raise HistoryPaused('history_scope_required')
        return await self._pipeline.run(event)
