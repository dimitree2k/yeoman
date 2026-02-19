"""Policy adapter backed directly by PolicyEngine."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import websockets

from nanobot.config.loader import load_config
from nanobot.core.admin_commands import (
    AdminCommandContext,
    AdminCommandHandler,
    AdminCommandResult,
    AdminCommandRouter,
    AdminMetricEvent,
)
from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import PolicyPort
from nanobot.policy.admin.contracts import (
    PolicyActorContext,
    PolicyCommand,
    PolicyExecutionOptions,
    PolicyExecutionResult,
)
from nanobot.policy.admin.service import PolicyAdminService
from nanobot.policy.engine import ActorContext, PolicyEngine
from nanobot.policy.identity import (
    normalize_identity_token,
    normalize_sender_list,
    resolve_actor_identity,
)
from nanobot.policy.loader import load_policy, save_policy
from nanobot.policy.schema import (
    BlockedSendersPolicyOverride,
    ChatPolicyOverride,
    PolicyConfig,
    VoiceOutputPolicyOverride,
    VoicePolicyOverride,
    WhenToReplyMode,
    WhenToReplyPolicyOverride,
    WhoCanTalkPolicyOverride,
)
from nanobot.utils.helpers import safe_filename

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

_POLICY_ADMIN_USAGE = (
    "Policy commands (owner DM only):\n"
    "/policy help\n"
    "/policy list-groups [query]\n"
    "/policy allow-group <chat_id@g.us>\n"
    "/policy block-group <chat_id@g.us>\n"
    "/policy set-when <chat_id@g.us> <all|mention_only|allowed_senders|owner_only|off>\n"
    "/policy set-persona <chat_id@g.us> <persona_path>\n"
    "/policy clear-persona <chat_id@g.us>\n"
    "/policy block-sender <chat_id@g.us> <sender_id>\n"
    "/policy unblock-sender <chat_id@g.us> <sender_id>\n"
    "/policy list-blocked <chat_id@g.us>\n"
    "/policy status-group <chat_id@g.us>"
)


def _to_actor(event: InboundEvent) -> ActorContext:
    identity = resolve_actor_identity(
        event.channel,
        event.sender_id,
        {
            "sender_id": event.sender_id,
            "sender": event.sender_id,
            "participant": event.participant,
            "participant_jid": event.participant,
        },
    )
    return ActorContext(
        channel=event.channel,
        chat_id=event.chat_id,
        sender_primary=identity.primary,
        sender_aliases=list(identity.aliases),
        is_group=event.is_group,
        mentioned_bot=event.mentioned_bot,
        reply_to_bot=event.reply_to_bot,
        content=event.content,
        is_voice=bool(event.raw_metadata.get("is_voice", False))
        or str(event.raw_metadata.get("media_kind") or "").strip() == "audio",
    )


def _to_admin_context(event: InboundEvent) -> AdminCommandContext:
    return AdminCommandContext(
        channel=event.channel,
        chat_id=event.chat_id,
        sender_id=event.sender_id,
        participant=event.participant,
        is_group=event.is_group,
        raw_text=event.content,
    )


class EnginePolicyAdapter(PolicyPort):
    """PolicyPort implementation using the typed `PolicyEngine` directly."""

    def __init__(
        self,
        *,
        engine: PolicyEngine | None,
        known_tools: set[str],
        policy_path: Path | None = None,
        reload_on_change: bool | None = None,
        reload_check_interval_seconds: float | None = None,
        session_manager: "SessionManager | None" = None,
        workspace: Path | None = None,
        memory_state_dir: str = "memory/session-state",
    ) -> None:
        self._engine = engine
        self._known_tools = set(known_tools)
        self._policy_path = policy_path
        self._session_manager = session_manager
        if workspace is not None:
            self._workspace = workspace.expanduser().resolve()
        elif self._engine is not None:
            self._workspace = self._engine.workspace
        else:
            self._workspace = (Path.home() / ".nanobot" / "workspace").resolve()
        self._memory_state_dir = str(memory_state_dir or "memory/session-state")
        self._policy_admin_service: PolicyAdminService | None = None
        self._admin_router = AdminCommandRouter(
            [
                CommandCatalogCommandHandler(self),
                HelpAliasCommandHandler(self),
                PolicyAdminCommandHandler(self),
                VoiceMessagesCommandHandler(self),
                ResetSessionCommandHandler(self),
            ]
        )
        self._last_reload_check = 0.0
        self._last_mtime_ns = self._stat_mtime_ns()

        if engine is None:
            self._reload_on_change = False
            self._reload_check_interval_seconds = 30.0
        else:
            runtime = engine.policy.runtime
            self._reload_on_change = runtime.reload_on_change if reload_on_change is None else reload_on_change
            self._reload_check_interval_seconds = (
                runtime.reload_check_interval_seconds
                if reload_check_interval_seconds is None
                else reload_check_interval_seconds
            )
        if self._policy_path is not None:
            workspace = self._engine.workspace if self._engine is not None else Path.home() / ".nanobot" / "workspace"
            apply_channels = self._engine.apply_channels if self._engine is not None else {"telegram", "whatsapp"}
            self._policy_admin_service = PolicyAdminService(
                policy_path=self._policy_path,
                workspace=workspace,
                known_tools=self._known_tools,
                apply_channels=apply_channels,
                on_policy_applied=self._on_policy_applied,
                group_subject_resolver=lambda ids: self._list_group_subjects_from_bridge(ids),
            )

    @property
    def known_tools(self) -> frozenset[str]:
        return frozenset(self._known_tools)

    def owner_recipients(self, channel: str) -> list[str]:
        """Return raw owner recipients configured for a channel."""
        if self._engine is None:
            return []
        self._maybe_reload()
        values = self._engine.policy.owners.get(channel, [])
        return [str(v).strip() for v in values if str(v).strip()]

    def _stat_mtime_ns(self) -> int | None:
        if self._policy_path is None:
            return None
        try:
            return self._policy_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _maybe_reload(self) -> None:
        if self._engine is None:
            return
        if not self._reload_on_change or self._policy_path is None:
            return

        now = time.monotonic()
        if now - self._last_reload_check < self._reload_check_interval_seconds:
            return
        self._last_reload_check = now

        current_mtime = self._stat_mtime_ns()
        if current_mtime == self._last_mtime_ns:
            return

        new_policy = load_policy(self._policy_path)
        new_engine = PolicyEngine(
            policy=new_policy,
            workspace=self._engine.workspace,
            apply_channels=self._engine.apply_channels,
        )
        new_engine.validate(self._known_tools)
        self._engine = new_engine
        self._last_mtime_ns = current_mtime

    def _on_policy_applied(self, policy: PolicyConfig) -> None:
        if self._engine is None:
            return
        new_engine = PolicyEngine(
            policy=policy,
            workspace=self._engine.workspace,
            apply_channels=self._engine.apply_channels,
        )
        new_engine.validate(self._known_tools)
        self._engine = new_engine
        self._last_mtime_ns = self._stat_mtime_ns()
        self._last_reload_check = time.monotonic()

    @override
    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        if self._engine is None:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset(self._known_tools),
                reason="policy_disabled",
                notes_enabled=False,
                notes_mode="adaptive",
                notes_allow_blocked_senders=False,
                notes_batch_interval_seconds=1800,
                notes_batch_max_messages=100,
                voice_output_mode="text",
                voice_output_tts_route="tts.speak",
                voice_output_voice="alloy",
                voice_output_format="opus",
                voice_output_max_sentences=2,
                voice_output_max_chars=150,
                source="disabled",
            )

        self._maybe_reload()
        actor = _to_actor(event)
        decision = self._engine.evaluate(actor, self._known_tools)
        is_owner = self._engine.is_owner(actor)
        voice_output_mode = "text"
        voice_output_tts_route = "tts.speak"
        voice_output_voice = "alloy"
        voice_output_format = "opus"
        voice_output_max_sentences = 2
        voice_output_max_chars = 150
        if event.channel in self._engine.apply_channels:
            try:
                effective = self._engine.resolve_policy(event.channel, event.chat_id)
                voice_output_mode = effective.voice_output_mode
                voice_output_tts_route = effective.voice_output_tts_route
                voice_output_voice = effective.voice_output_voice
                voice_output_format = effective.voice_output_format
                voice_output_max_sentences = effective.voice_output_max_sentences
                voice_output_max_chars = effective.voice_output_max_chars
            except Exception:
                # Policy voice output settings are optional and should never break evaluation.
                pass
        notes = self._engine.resolve_memory_notes(
            channel=event.channel,
            chat_id=event.chat_id,
            is_group=event.is_group,
        )
        return PolicyDecision(
            accept_message=decision.accept_message,
            should_respond=decision.should_respond,
            allowed_tools=frozenset(decision.allowed_tools),
            reason=decision.reason,
            persona_file=decision.persona_file,
            persona_text=self._engine.persona_text(decision.persona_file),
            notes_enabled=notes.enabled,
            notes_mode=notes.mode,
            notes_allow_blocked_senders=notes.allow_blocked_senders,
            notes_batch_interval_seconds=notes.batch_interval_seconds,
            notes_batch_max_messages=notes.batch_max_messages,
            voice_output_mode=voice_output_mode,
            voice_output_tts_route=voice_output_tts_route,
            voice_output_voice=voice_output_voice,
            voice_output_format=voice_output_format,
            voice_output_max_sentences=voice_output_max_sentences,
            voice_output_max_chars=voice_output_max_chars,
            is_owner=is_owner,
            source=str(self._policy_path) if self._policy_path else "in-memory",
        )

    def explain(
        self,
        *,
        channel: str,
        chat_id: str,
        sender_id: str,
        is_group: bool = False,
        mentioned_bot: bool = False,
        reply_to_bot: bool = False,
    ) -> dict[str, Any]:
        """Return merged policy and decision snapshot for diagnostics."""
        event = InboundEvent(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            content="policy explain",
            is_group=is_group,
            mentioned_bot=mentioned_bot,
            reply_to_bot=reply_to_bot,
        )
        actor = _to_actor(event)
        decision = self.evaluate(event)

        effective = None
        notes = None
        if self._engine is not None and channel in self._engine.apply_channels:
            effective = self._engine.resolve_policy(channel, chat_id)
        if self._engine is not None:
            notes = self._engine.resolve_memory_notes(
                channel=channel,
                chat_id=chat_id,
                is_group=is_group,
            )

        return {
            "policySource": decision.source,
            "channel": channel,
            "chatId": chat_id,
            "sender": {
                "primary": actor.sender_primary,
                "aliases": actor.sender_aliases,
            },
            "effectivePolicy": (
                {
                    "whoCanTalk": {
                        "mode": effective.who_can_talk_mode,
                        "senders": effective.who_can_talk_senders,
                    },
                    "whenToReply": {
                        "mode": effective.when_to_reply_mode,
                        "senders": effective.when_to_reply_senders,
                    },
                    "blockedSenders": {
                        "senders": effective.blocked_senders,
                    },
                    "allowedTools": {
                        "mode": effective.allowed_tools_mode,
                        "tools": effective.allowed_tools_tools,
                        "deny": effective.allowed_tools_deny,
                    },
                    "toolAccess": effective.tool_access,
                    "personaFile": effective.persona_file,
                    "voice": {
                        "input": {
                            "wakePhrases": effective.voice_input_wake_phrases,
                        },
                        "output": {
                            "mode": effective.voice_output_mode,
                            "ttsRoute": effective.voice_output_tts_route,
                            "voice": effective.voice_output_voice,
                            "format": effective.voice_output_format,
                            "maxSentences": effective.voice_output_max_sentences,
                            "maxChars": effective.voice_output_max_chars,
                        },
                    },
                }
                if effective is not None
                else None
            ),
            "decision": {
                "acceptMessage": decision.accept_message,
                "shouldRespond": decision.should_respond,
                "reason": decision.reason,
                "allowedTools": sorted(decision.allowed_tools),
                "personaFile": decision.persona_file,
                "memoryNotesEnabled": decision.notes_enabled,
                "memoryNotesMode": decision.notes_mode,
                "memoryNotesAllowBlockedSenders": decision.notes_allow_blocked_senders,
            },
            "memoryNotes": (
                {
                    "enabled": notes.enabled,
                    "mode": notes.mode,
                    "allowBlockedSenders": notes.allow_blocked_senders,
                    "batchIntervalSeconds": notes.batch_interval_seconds,
                    "batchMaxMessages": notes.batch_max_messages,
                    "source": notes.source,
                }
                if notes is not None
                else None
            ),
        }

    def maybe_handle_admin_command(self, event: InboundEvent) -> str | None:
        """Backward-compatible helper used by tests and diagnostics."""
        result = self.route_admin_command(event)
        if result is None or not result.intercepts_normal_flow:
            return None
        return result.response

    def route_admin_command(self, event: InboundEvent) -> AdminCommandResult | None:
        """Route one deterministic slash command and return structured outcome."""
        if event.channel != "whatsapp":
            return None
        return self._admin_router.route(_to_admin_context(event))

    def policy_admin_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx)) and not ctx.is_group

    def session_reset_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def command_catalog_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def voice_messages_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def session_reset_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        if argv:
            return AdminCommandResult(status="handled", response="Usage: /reset")

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Session reset unavailable: policy engine is not active.",
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")
        if self._session_manager is None:
            return AdminCommandResult(
                status="handled",
                response="Session reset unavailable: session manager is not configured.",
            )

        session_key = f"{ctx.channel}:{ctx.chat_id}"
        try:
            session = self._session_manager.get_or_create(session_key)
            cleared_messages = len(session.messages)
            session.clear()
            self._session_manager.save(session)
        except Exception as e:
            return AdminCommandResult(status="handled", response=f"Session reset failed: {e}")

        wal_path = self._session_wal_path(session_key)
        wal_cleared = False
        try:
            wal_path.unlink()
            wal_cleared = True
        except FileNotFoundError:
            pass
        except Exception:
            # WAL cleanup is best-effort; session history reset already succeeded.
            wal_cleared = False

        message = f"Conversation history cleared for {ctx.chat_id} ({cleared_messages} messages)."
        if wal_cleared:
            message += " Session state cleared."
        return AdminCommandResult(
            status="handled",
            response=message,
            command_name="reset",
            outcome="applied",
            source="dm",
            metric_events=(
                AdminMetricEvent(name="session_reset_total", labels=(("channel", ctx.channel),)),
            ),
        )

    def command_catalog_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        include_all = False
        if argv:
            normalized = argv[0].strip().lower()
            if len(argv) == 1 and normalized in {"all", "full"}:
                include_all = True
            elif len(argv) == 1 and normalized in {"help", "-h", "--help"}:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /commands [all]",
                    command_name="commands",
                    outcome="applied",
                    source="dm" if not ctx.is_group else "group",
                )
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /commands [all]",
                    command_name="commands",
                    outcome="invalid",
                    source="dm" if not ctx.is_group else "group",
                )

        lines = [
            "Available slash commands for this chat:",
            "- /commands [all] — list available commands",
            "- /reset — clear conversation history for this chat",
            "- /voicemessages <status|on|off|in_kind|always|text|inherit>",
        ]
        if self.policy_admin_is_applicable(ctx):
            lines.append("- /policy help — policy admin commands")
            if include_all and self._policy_admin_service is not None:
                lines.append("")
                lines.extend(self._policy_admin_service.registry.usage_lines())
        else:
            lines.append("In your DM with Nano: /policy help")
        return AdminCommandResult(
            status="handled",
            response="\n".join(lines),
            command_name="commands",
            outcome="applied",
            source="dm" if not ctx.is_group else "group",
        )

    @staticmethod
    def _voice_mode_token(raw: str) -> str | None:
        value = raw.strip().lower().replace("-", "_")
        aliases = {
            "on": "in_kind",
            "off": "off",
            "status": "status",
            "inherit": "inherit",
            "default": "inherit",
            "inkind": "in_kind",
        }
        mode = aliases.get(value, value)
        valid = {"status", "inherit", "text", "in_kind", "always", "off"}
        if mode not in valid:
            return None
        return mode

    @staticmethod
    def _voice_output_override_is_empty(override: VoiceOutputPolicyOverride) -> bool:
        return (
            override.mode is None
            and override.tts_route is None
            and override.voice is None
            and override.format is None
            and override.max_sentences is None
            and override.max_chars is None
        )

    @classmethod
    def _cleanup_voice_override(cls, override: ChatPolicyOverride) -> None:
        voice = override.voice
        if voice is None:
            return
        if voice.input is not None and voice.input.wake_phrases is None:
            voice.input = None
        if voice.output is not None and cls._voice_output_override_is_empty(voice.output):
            voice.output = None
        if voice.input is None and voice.output is None:
            override.voice = None

    def voice_messages_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        if len(argv) > 1:
            return AdminCommandResult(
                status="handled",
                response="Usage: /voicemessages <status|on|off|in_kind|always|text|inherit>",
                command_name="voicemessages",
                outcome="invalid",
                source="dm" if not ctx.is_group else "group",
            )

        mode_token = "status"
        if argv:
            parsed = self._voice_mode_token(argv[0])
            if parsed is None:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /voicemessages <status|on|off|in_kind|always|text|inherit>",
                    command_name="voicemessages",
                    outcome="invalid",
                    source="dm" if not ctx.is_group else "group",
                )
            mode_token = parsed

        if self._engine is None:
            return AdminCommandResult(
                status="handled",
                response="Voice settings unavailable: policy engine is not active.",
                command_name="voicemessages",
                outcome="error",
                source="dm" if not ctx.is_group else "group",
            )

        if mode_token == "status":
            effective = self._engine.resolve_policy("whatsapp", ctx.chat_id)
            return AdminCommandResult(
                status="handled",
                response=f"Voice messages for this chat: {effective.voice_output_mode}.",
                command_name="voicemessages",
                outcome="applied",
                source="dm" if not ctx.is_group else "group",
            )

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Voice settings unavailable: policy is not loaded.",
                command_name="voicemessages",
                outcome="error",
                source="dm" if not ctx.is_group else "group",
            )

        override = self._whatsapp_chat_override(policy, ctx.chat_id)
        if mode_token == "inherit":
            if override.voice is not None and override.voice.output is not None:
                override.voice.output.mode = None
        else:
            if override.voice is None:
                override.voice = VoicePolicyOverride()
            if override.voice.output is None:
                override.voice.output = VoiceOutputPolicyOverride()
            override.voice.output.mode = mode_token  # type: ignore[assignment]
        self._cleanup_voice_override(override)

        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return AdminCommandResult(
                status="handled",
                response=f"Failed to apply voice setting: {e}",
                command_name="voicemessages",
                outcome="error",
                source="dm" if not ctx.is_group else "group",
            )

        effective = self._engine.resolve_policy("whatsapp", ctx.chat_id)
        return AdminCommandResult(
            status="handled",
            response=f"Voice messages updated for this chat: {effective.voice_output_mode}.",
            command_name="voicemessages",
            outcome="applied",
            source="dm" if not ctx.is_group else "group",
            metric_events=(
                AdminMetricEvent(
                    name="voice_messages_set_total",
                    labels=(("channel", ctx.channel), ("mode", effective.voice_output_mode)),
                ),
            ),
        )

    def policy_admin_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(status="handled", response="Policy admin unavailable: policy engine is not active.")
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")
        if self._policy_admin_service is None:
            return AdminCommandResult(status="handled", response="Policy admin service unavailable.")

        subcommand = argv[0] if argv else "help"
        command = PolicyCommand(
            namespace="policy",
            subcommand=subcommand,
            argv=tuple(argv[1:]) if argv else (),
            raw_text=ctx.raw_text.strip() or f"/policy {' '.join(argv)}".strip(),
        )
        execution = self._policy_admin_service.execute(
            command=command,
            actor=PolicyActorContext(
                source="dm",
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                sender_id=ctx.sender_id,
                is_group=ctx.is_group,
                is_owner=True,
            ),
            options=PolicyExecutionOptions(),
        )
        return self._execution_to_admin_result(execution)

    def _load_policy_for_admin(self) -> PolicyConfig | None:
        if self._engine is None or self._policy_path is None:
            return None
        try:
            return load_policy(self._policy_path)
        except Exception:
            return None

    def _owner_policy_for_context(self, ctx: AdminCommandContext) -> PolicyConfig | None:
        if ctx.channel != "whatsapp":
            return None
        policy = self._load_policy_for_admin()
        if policy is None:
            return None
        if not self._is_whatsapp_owner(ctx, policy):
            return None
        return policy

    def _session_wal_path(self, session_key: str) -> Path:
        state_dir = Path(self._memory_state_dir).expanduser()
        if not state_dir.is_absolute():
            state_dir = self._workspace / state_dir
        safe_key = safe_filename(session_key.replace(":", "_"))
        return state_dir / f"{safe_key}.md"

    def _execution_to_admin_result(self, execution: PolicyExecutionResult) -> AdminCommandResult:
        status = "handled"
        if execution.outcome == "denied" and not execution.message.strip():
            status = "ignored"
        elif execution.unknown_command:
            status = "unknown"

        command_name = execution.command_name or "help"
        metrics: list[AdminMetricEvent] = [
            AdminMetricEvent(
                name="policy_admin_execute_total",
                labels=(
                    ("outcome", execution.outcome),
                    ("source", execution.source),
                    ("command", command_name),
                ),
            )
        ]
        if self._policy_admin_service is not None and self._policy_admin_service.registry.is_mutating(command_name):
            metrics.append(
                AdminMetricEvent(
                    name="policy_admin_mutation_total",
                    labels=(
                        ("command", command_name),
                        ("dry_run", "true" if execution.dry_run else "false"),
                    ),
                )
            )
        if execution.is_rollback:
            metrics.append(
                AdminMetricEvent(
                    name="policy_admin_rollback_total",
                    labels=(("outcome", execution.outcome),),
                )
            )
        if execution.audit_write_failed:
            metrics.append(AdminMetricEvent(name="policy_admin_audit_write_fail_total"))

        return AdminCommandResult(
            status=status,  # type: ignore[arg-type]
            response=execution.message if status != "ignored" else None,
            command_name=command_name,
            outcome=execution.outcome,
            source=execution.source,
            dry_run=execution.dry_run,
            metric_events=tuple(metrics),
        )

    def _is_whatsapp_owner(self, ctx: AdminCommandContext, policy: PolicyConfig) -> bool:
        identity = resolve_actor_identity(
            ctx.channel,
            ctx.sender_id,
            {
                "sender_id": ctx.sender_id,
                "sender": ctx.sender_id,
                "participant": ctx.participant,
                "participant_jid": ctx.participant,
            },
        )
        owners = normalize_sender_list("whatsapp", policy.owners.get("whatsapp", []))
        if not owners:
            return False
        if identity.primary in owners:
            return True
        return any(alias in owners for alias in identity.aliases)

    def _parse_group_chat_id(self, value: str) -> str:
        chat_id = value.strip()
        if " " in chat_id or not chat_id.endswith("@g.us"):
            raise ValueError("chat id must be a WhatsApp group id ending in @g.us")
        return chat_id

    def _parse_when_mode(self, value: str) -> WhenToReplyMode:
        mode = value.strip().lower().replace("-", "_")
        aliases = {
            "mention": "mention_only",
            "mentions": "mention_only",
            "mentiononly": "mention_only",
            "allowed": "allowed_senders",
            "owner": "owner_only",
        }
        mode = aliases.get(mode, mode)
        valid = {"all", "mention_only", "allowed_senders", "owner_only", "off"}
        if mode not in valid:
            raise ValueError("mode must be one of: all, mention_only, allowed_senders, owner_only, off")
        return mode  # type: ignore[return-value]

    def _sender_keys(self, senders: list[str]) -> set[str]:
        return {normalize_identity_token(value) for value in senders if normalize_identity_token(value)}

    def _whatsapp_chat_override(self, policy: PolicyConfig, chat_id: str) -> ChatPolicyOverride:
        channel = policy.channels.get("whatsapp")
        if channel is None:
            raise ValueError("whatsapp channel is missing in policy")
        override = channel.chats.get(chat_id)
        if override is None:
            override = ChatPolicyOverride()
            channel.chats[chat_id] = override
        return override

    def _save_policy_and_reload(self, policy: PolicyConfig) -> None:
        if self._engine is None or self._policy_path is None:
            raise RuntimeError("policy adapter is not configured for persistence")
        new_engine = PolicyEngine(
            policy=policy,
            workspace=self._engine.workspace,
            apply_channels=self._engine.apply_channels,
        )
        new_engine.validate(self._known_tools)
        save_policy(policy, self._policy_path)
        self._engine = new_engine
        self._last_mtime_ns = self._stat_mtime_ns()
        self._last_reload_check = time.monotonic()

    def _cmd_allow_group(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 3:
            return "Usage: /policy allow-group <chat_id@g.us>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid allow-group arguments: {e}"

        override = self._whatsapp_chat_override(policy, chat_id)
        override.who_can_talk = WhoCanTalkPolicyOverride(mode="everyone", senders=[])
        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: whoCanTalk=everyone."

    def _cmd_block_group(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 3:
            return "Usage: /policy block-group <chat_id@g.us>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid block-group arguments: {e}"

        owner_senders = list(policy.owners.get("whatsapp", []))
        if not owner_senders:
            return "Cannot block group: owners.whatsapp is empty in policy."
        override = self._whatsapp_chat_override(policy, chat_id)
        override.who_can_talk = WhoCanTalkPolicyOverride(mode="allowlist", senders=owner_senders)
        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: whoCanTalk=allowlist (owners only)."

    def _cmd_status_group(self, tokens: list[str]) -> str:
        if len(tokens) != 3:
            return "Usage: /policy status-group <chat_id@g.us>"
        if self._engine is None:
            return "Policy engine is not active."
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid status-group arguments: {e}"
        effective = self._engine.resolve_policy("whatsapp", chat_id)
        return (
            f"{chat_id}\n"
            f"whoCanTalk={effective.who_can_talk_mode}\n"
            f"whenToReply={effective.when_to_reply_mode}\n"
            f"blockedSenders={','.join(effective.blocked_senders)}\n"
            f"personaFile={effective.persona_file or '-'}\n"
            f"allowedTools.mode={effective.allowed_tools_mode}\n"
            f"allowedTools.tools={','.join(effective.allowed_tools_tools)}\n"
            f"allowedTools.deny={','.join(effective.allowed_tools_deny)}"
        )

    def _cmd_set_when(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 4:
            return "Usage: /policy set-when <chat_id@g.us> <all|mention_only|allowed_senders|owner_only|off>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
            mode = self._parse_when_mode(tokens[3])
        except ValueError as e:
            return f"Invalid set-when arguments: {e}"

        override = self._whatsapp_chat_override(policy, chat_id)
        override.when_to_reply = WhenToReplyPolicyOverride(mode=mode, senders=[])
        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: whenToReply={mode}."

    def _cmd_set_persona(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 4:
            return "Usage: /policy set-persona <chat_id@g.us> <persona_path>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid set-persona arguments: {e}"
        persona_path = tokens[3].strip()
        if not persona_path:
            return "Invalid set-persona arguments: persona_path cannot be empty"

        override = self._whatsapp_chat_override(policy, chat_id)
        override.persona_file = persona_path
        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: personaFile={persona_path}."

    def _cmd_clear_persona(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 3:
            return "Usage: /policy clear-persona <chat_id@g.us>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid clear-persona arguments: {e}"

        override = self._whatsapp_chat_override(policy, chat_id)
        override.persona_file = None
        try:
            self._save_policy_and_reload(policy)
        except Exception as e:
            return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: personaFile cleared (inherits channel/default policy)."

    def _cmd_block_sender(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 4:
            return "Usage: /policy block-sender <chat_id@g.us> <sender_id>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid block-sender arguments: {e}"
        sender = tokens[3].strip()
        if not sender:
            return "Invalid block-sender arguments: sender_id cannot be empty"
        sender_key = normalize_identity_token(sender)
        if not sender_key:
            return "Invalid block-sender arguments: sender_id cannot be empty"

        override = self._whatsapp_chat_override(policy, chat_id)
        current = list(override.blocked_senders.senders) if override.blocked_senders else []
        keys = self._sender_keys(current)
        if sender_key not in keys:
            current.append(sender)
            override.blocked_senders = BlockedSendersPolicyOverride(senders=current)
            try:
                self._save_policy_and_reload(policy)
            except Exception as e:
                return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: blocked sender {sender}."

    def _cmd_unblock_sender(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 4:
            return "Usage: /policy unblock-sender <chat_id@g.us> <sender_id>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid unblock-sender arguments: {e}"
        sender = tokens[3].strip()
        if not sender:
            return "Invalid unblock-sender arguments: sender_id cannot be empty"
        sender_key = normalize_identity_token(sender)
        if not sender_key:
            return "Invalid unblock-sender arguments: sender_id cannot be empty"

        override = self._whatsapp_chat_override(policy, chat_id)
        current = list(override.blocked_senders.senders) if override.blocked_senders else []
        updated = [value for value in current if normalize_identity_token(value) != sender_key]
        if len(updated) != len(current):
            override.blocked_senders = BlockedSendersPolicyOverride(senders=updated)
            try:
                self._save_policy_and_reload(policy)
            except Exception as e:
                return f"Failed to apply policy change: {e}"
        return f"Policy updated for {chat_id}: unblocked sender {sender}."

    def _cmd_list_blocked(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) != 3:
            return "Usage: /policy list-blocked <chat_id@g.us>"
        try:
            chat_id = self._parse_group_chat_id(tokens[2])
        except ValueError as e:
            return f"Invalid list-blocked arguments: {e}"
        override = self._whatsapp_chat_override(policy, chat_id)
        values = list(override.blocked_senders.senders) if override.blocked_senders else []
        if not values:
            return f"{chat_id}: blockedSenders is empty."
        lines = [f"{chat_id}: blockedSenders ({len(values)})"]
        for value in values:
            lines.append(f"- {value}")
        return "\n".join(lines)

    def _cmd_list_groups(self, tokens: list[str], policy: PolicyConfig) -> str:
        if len(tokens) > 3:
            return "Usage: /policy list-groups [query]"
        query = tokens[2].strip().lower() if len(tokens) == 3 else ""

        records: dict[str, dict[str, Any]] = {}

        def ensure(chat_id: str) -> dict[str, Any]:
            rec = records.get(chat_id)
            if rec is None:
                rec = {
                    "chat_id": chat_id,
                    "in_policy": False,
                    "comment": "",
                    "seen_session": False,
                    "seen_log": False,
                    "session_mtime": 0.0,
                }
                records[chat_id] = rec
            return rec

        # Policy-defined groups with optional comments.
        wa = policy.channels.get("whatsapp")
        if wa is not None:
            for chat_id, override in wa.chats.items():
                if not isinstance(chat_id, str) or not chat_id.endswith("@g.us"):
                    continue
                rec = ensure(chat_id)
                rec["in_policy"] = True
                comment = (override.comment or "").strip()
                if comment:
                    rec["comment"] = comment

        base_dir = self._policy_path.parent if self._policy_path is not None else Path.home() / ".nanobot"

        # Session files show groups observed by runtime.
        sessions_dir = base_dir / "sessions"
        if sessions_dir.exists():
            for path in sessions_dir.glob("whatsapp_*@g.us.jsonl"):
                chat_id = path.name[len("whatsapp_") : -len(".jsonl")]
                if not chat_id.endswith("@g.us"):
                    continue
                rec = ensure(chat_id)
                rec["seen_session"] = True
                try:
                    rec["session_mtime"] = max(float(rec["session_mtime"]), path.stat().st_mtime)
                except OSError:
                    pass

        # Gateway log is a fallback source for recently observed group IDs.
        log_path = base_dir / "logs" / "gateway.log"
        if log_path.exists():
            try:
                with open(log_path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        for chat_id in re.findall(r"chat=([0-9a-zA-Z-]+@g\.us)", line):
                            rec = ensure(chat_id)
                            rec["seen_log"] = True
            except OSError:
                pass

        # Bridge lookup can resolve human-readable group subjects even if policy has no comment.
        bridge_names = self._list_group_subjects_from_bridge(list(records.keys()))
        for chat_id, subject in bridge_names.items():
            rec = records.get(chat_id)
            if rec is None:
                rec = ensure(chat_id)
            rec["seen_bridge"] = True
            if not str(rec.get("comment") or "").strip():
                rec["comment"] = subject

        if not records:
            return "No WhatsApp groups discovered yet."

        rows: list[dict[str, Any]] = []
        for rec in records.values():
            chat_id = str(rec["chat_id"])
            comment = str(rec["comment"] or "")
            if query and query not in chat_id.lower() and query not in comment.lower():
                continue
            rows.append(rec)
        if not rows:
            return f"No WhatsApp groups matched '{query}'."

        rows.sort(
            key=lambda r: (
                0 if bool(r["in_policy"]) else 1,
                -float(r["session_mtime"]),
                str(r["chat_id"]),
            )
        )

        max_rows = 40
        shown = rows[:max_rows]
        lines = [f"Known WhatsApp groups: {len(rows)} (showing {len(shown)})"]
        for rec in shown:
            chat_id = str(rec["chat_id"])
            comment = str(rec["comment"] or "")
            sources: list[str] = []
            if rec["in_policy"]:
                sources.append("policy")
            if rec["seen_session"]:
                sources.append("sessions")
            if rec["seen_log"]:
                sources.append("log")
            if rec.get("seen_bridge"):
                sources.append("bridge")
            source_text = "+".join(sources) if sources else "unknown"
            if comment:
                lines.append(f"- {chat_id} | {source_text} | {comment}")
            else:
                lines.append(f"- {chat_id} | {source_text}")

        if len(rows) > max_rows:
            lines.append(f"... and {len(rows) - max_rows} more")
        lines.append("Use: /policy allow-group <chat_id@g.us> or /policy block-group <chat_id@g.us>")
        return "\n".join(lines)

    def _list_group_subjects_from_bridge(self, ids: list[str]) -> dict[str, str]:
        target_ids = [cid for cid in ids if isinstance(cid, str) and cid.endswith("@g.us")]
        if not target_ids:
            return {}

        try:
            config = load_config()
        except Exception:
            return {}
        if not bool(getattr(config.channels.whatsapp, "enabled", False)):
            return {}
        token = str(getattr(config.channels.whatsapp, "bridge_token", "") or "").strip()
        if not token:
            return {}
        bridge_url = str(config.channels.whatsapp.resolved_bridge_url).strip()
        if not bridge_url:
            return {}

        async def _fetch(url: str, chat_ids: list[str], bridge_token: str) -> dict[str, str]:
            request_id = uuid.uuid4().hex
            payload = {
                "version": 2,
                "type": "list_groups",
                "token": bridge_token,
                "requestId": request_id,
                "accountId": "default",
                "payload": {"ids": chat_ids},
            }
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(payload))
                deadline = time.monotonic() + 5.0
                while True:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError("bridge did not reply in time")
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    data = json.loads(raw)
                    if data.get("version") != 2:
                        continue
                    if data.get("type") != "response":
                        continue
                    if data.get("requestId") != request_id:
                        continue
                    response_payload = data.get("payload")
                    if not isinstance(response_payload, dict):
                        raise RuntimeError("bridge response payload malformed")
                    if not bool(response_payload.get("ok")):
                        return {}
                    result = response_payload.get("result")
                    if not isinstance(result, dict):
                        return {}
                    groups = result.get("groups", [])
                    out: dict[str, str] = {}
                    if isinstance(groups, list):
                        for item in groups:
                            if not isinstance(item, dict):
                                continue
                            gid = str(item.get("chatJid", "")).strip()
                            subj = str(item.get("subject", "")).strip()
                            if gid and subj:
                                out[gid] = subj
                    return out

        result_holder: dict[str, str] = {}
        error_holder: dict[str, Exception] = {}

        def _runner() -> None:
            try:
                result_holder.update(asyncio.run(_fetch(bridge_url, target_ids, token)))
            except Exception as e:
                error_holder["error"] = e

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join(timeout=6.0)
        if thread.is_alive():
            return {}
        if error_holder:
            return {}
        return result_holder


class PolicyAdminCommandHandler(AdminCommandHandler):
    """Deterministic `/policy ...` command namespace handler."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "policy"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.policy_admin_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.policy_admin_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/policy help"


class ResetSessionCommandHandler(AdminCommandHandler):
    """Deterministic `/reset` command for clearing chat session context."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "reset"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.session_reset_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.session_reset_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/reset"


class CommandCatalogCommandHandler(AdminCommandHandler):
    """Deterministic `/commands` command for discoverability."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "commands"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.command_catalog_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.command_catalog_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/commands"


class HelpAliasCommandHandler(AdminCommandHandler):
    """Alias `/help` to `/commands` in WhatsApp owner contexts."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "help"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.command_catalog_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.command_catalog_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/help"


class VoiceMessagesCommandHandler(AdminCommandHandler):
    """Deterministic `/voicemessages` command for per-chat voice output mode."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "voicemessages"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.voice_messages_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.voice_messages_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/voicemessages"
