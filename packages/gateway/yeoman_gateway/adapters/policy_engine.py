"""Policy adapter backed directly by PolicyEngine."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, override

import websockets
from loguru import logger
from yeoman_shared.config.loader import load_config
from yeoman_shared.utils.helpers import get_operational_data_path
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

from yeoman_gateway.core.admin_commands import (
    AdminCommandContext,
    AdminCommandHandler,
    AdminCommandResult,
    AdminCommandRouter,
    AdminMetricEvent,
)
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.ports import PolicyPort
from yeoman_gateway.policy.admin.service import PolicyAdminService
from yeoman_gateway.policy.capabilities import policy_known_tools
from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
from yeoman_gateway.policy.identity import normalize_sender_list, resolve_actor_identity
from yeoman_gateway.policy.loader import load_policy, save_policy
from yeoman_gateway.policy.schema import (
    ChatPolicyOverride,
    PolicyConfig,
    SpontaneityPolicyOverride,
    WhenToReplyPolicyOverride,
    WhoCanTalkPolicyOverride,
)

if TYPE_CHECKING:
    from yeoman_gateway.processing.models import PolicySnapshot
    from yeoman_gateway.session.manager import SessionManager
    from yeoman_gateway.storage.private_handoff import PrivateHandoff, PrivateHandoffStore

_PAUSE_STATE_VERSION = 1
_PAUSE_INDEFINITE = -1
_PAUSE_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
_PAUSE_DURATION_PATTERN = re.compile(r"^(?P<value>\d+)(?P<unit>[a-zA-Z]*)$")
_GROUP_APPROVAL_TARGET_PATTERN = re.compile(
    r"(?:Group approval|🆔 ID):\s*`?([^\s`]+@g\.us)`?",
    re.IGNORECASE,
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
        mentioned_bot=event.mentioned_bot
        or bool(re.match(r"^/voice(?:\s|$)", event.content.strip(), re.IGNORECASE)),
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
        processing_store: Any | None = None,
        private_handoff_store: "PrivateHandoffStore | None" = None,
        workspace: Path | None = None,
    ) -> None:
        self._engine = engine
        self._known_tools = policy_known_tools(known_tools)
        self._policy_path = policy_path
        self._session_manager = session_manager
        self._processing_store = processing_store
        self._private_handoff_store = private_handoff_store
        if workspace is not None:
            self._workspace = workspace.expanduser().resolve()
        elif self._engine is not None:
            self._workspace = self._engine.workspace
        else:
            self._workspace = (Path.home() / ".yeoman" / "workspace").resolve()
        self._policy_admin_service: PolicyAdminService | None = None
        self._admin_router = AdminCommandRouter(
            [
                CommandCatalogCommandHandler(self),
                HelpAliasCommandHandler(self),
                PauseCommandHandler(self),
                PanicCommandHandler(self),
                StartCommandHandler(self),
                StopCommandHandler(self),
                VoiceSendCommandHandler(self),
                NewSessionCommandHandler(self),
            ]
        )
        self._pause_state_path = self._resolve_pause_state_path()
        self._global_pause_until_ms = 0
        self._chat_pause_until_ms: dict[str, int] = {}
        self._load_pause_state()
        self._last_reload_check = 0.0
        self._last_mtime_ns = self._stat_mtime_ns()
        self._policy_file_was_present = self._last_mtime_ns is not None
        self._policy_reload_error: tuple[int | None, str] | None = None
        self._policy_loaded_ms: int = self._now_ms()

        if engine is None:
            self._reload_on_change = False
            self._reload_check_interval_seconds = 30.0
        else:
            runtime = engine.policy.runtime
            self._reload_on_change = (
                runtime.reload_on_change if reload_on_change is None else reload_on_change
            )
            self._reload_check_interval_seconds = (
                runtime.reload_check_interval_seconds
                if reload_check_interval_seconds is None
                else reload_check_interval_seconds
            )
        if self._policy_path is not None:
            workspace = (
                self._engine.workspace
                if self._engine is not None
                else Path.home() / ".yeoman" / "workspace"
            )
            apply_channels = (
                self._engine.apply_channels
                if self._engine is not None
                else {"telegram", "whatsapp"}
            )
            self._policy_admin_service = PolicyAdminService(
                policy_path=self._policy_path,
                workspace=workspace,
                known_tools=self._known_tools,
                apply_channels=apply_channels,
                on_policy_applied=self._on_policy_applied,
                group_subject_resolver=lambda ids: self._list_group_subjects_from_bridge(ids),
            )

    def _active_private_handoff(self, event: InboundEvent) -> "PrivateHandoff | None":
        if self._private_handoff_store is None:
            return None
        if event.is_group:
            return None
        try:
            return self._private_handoff_store.find_active(
                channel=event.channel,
                chat_id=event.chat_id,
                sender_id=event.sender_id,
            )
        except Exception as exc:
            logger.warning("private handoff lookup failed: {}", exc)
            return None

    def _private_handoff_decision(
        self,
        *,
        event: InboundEvent,
        handoff: "PrivateHandoff",
        notes: Any,
        is_owner: bool,
    ) -> PolicyDecision:
        persona_file: str | None = None
        model_profile: str | None = None
        reply_budget: dict[str, object] = {}
        try:
            if self._engine is not None and event.channel in self._engine.apply_channels:
                origin = self._engine.resolve_policy(event.channel, handoff.origin_chat_id)
                persona_file = origin.persona_file
                model_profile = origin.model_profile
                reply_budget = dict(origin.reply_budget)
        except Exception:
            pass
        persona_text = self._engine.persona_text(persona_file) if self._engine is not None else None
        return PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="private_handoff",
            when_to_reply_mode="all",
            persona_file=persona_file,
            persona_text=persona_text,
            notes_enabled=notes.enabled,
            notes_mode=notes.mode,
            notes_allow_blocked_senders=notes.allow_blocked_senders,
            notes_batch_interval_seconds=notes.batch_interval_seconds,
            notes_batch_max_messages=notes.batch_max_messages,
            reply_budget=reply_budget,
            model_profile=model_profile,
            is_owner=is_owner,
            private_handoff_active=True,
            private_handoff_id=handoff.id,
            private_handoff_origin_chat_id=handoff.origin_chat_id,
            private_handoff_origin_label=handoff.origin_label,
            private_handoff_remaining_replies=handoff.remaining_replies,
            source=str(self._policy_path) if self._policy_path else "private_handoff",
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

    def resolve_whatsapp_group(self, reference: str) -> tuple[str | None, str | None]:
        """Resolve one WhatsApp group reference (alias/name/chat id) to chat id."""
        target = str(reference or "").strip()
        if not target:
            return None, "group reference cannot be empty"
        if self._policy_admin_service is None:
            return None, "group resolver unavailable: policy admin service is not configured"

        policy = self._load_policy_for_admin()
        if policy is None:
            return None, "group resolver unavailable: policy is not loaded"
        return self._policy_admin_service.resolve_group_reference(target, policy=policy)

    def _stat_mtime_ns(self) -> int | None:
        if self._policy_path is None:
            return None
        try:
            return self._policy_path.stat().st_mtime_ns
        except OSError:
            return None

    def _record_policy_reload_error(self, current_mtime: int | None, exc: Exception) -> None:
        error_state = (current_mtime, type(exc).__name__)
        previous_error_version = (
            self._policy_reload_error[0] if self._policy_reload_error is not None else None
        )
        if self._policy_reload_error is None or current_mtime != previous_error_version:
            logger.error(
                "policy reload failed version={} error_type={} error={}",
                current_mtime,
                type(exc).__name__,
                str(exc)[:500],
            )
        self._policy_reload_error = error_state

    def _resolve_pause_state_path(self) -> Path:
        if self._policy_path is not None:
            base_dir = self._policy_path.parent
            return base_dir / "data" / "policy" / "response_pauses.json"
        return get_operational_data_path() / "policy" / "response_pauses.json"

    @staticmethod
    def _normalize_pause_until(value: object) -> int:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return 0
        if parsed == _PAUSE_INDEFINITE:
            return _PAUSE_INDEFINITE
        if parsed <= 0:
            return 0
        return parsed

    def _load_pause_state(self) -> None:
        path = self._pause_state_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return

        global_until = data.get("global_until_ms")
        if global_until is not None:
            self._global_pause_until_ms = self._normalize_pause_until(global_until)

        chat_payload = data.get("chat_until_ms")
        if isinstance(chat_payload, dict):
            parsed: dict[str, int] = {}
            for raw_key, raw_until in chat_payload.items():
                key = str(raw_key).strip()
                if not key:
                    continue
                until = self._normalize_pause_until(raw_until)
                if until == 0:
                    continue
                parsed[key] = until
            self._chat_pause_until_ms = parsed

    def _save_pause_state(self) -> None:
        payload = {
            "version": _PAUSE_STATE_VERSION,
            "global_until_ms": int(self._global_pause_until_ms),
            "chat_until_ms": {k: int(v) for k, v in sorted(self._chat_pause_until_ms.items())},
        }
        self._pause_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._pause_state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _pause_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _is_pause_until_active(until_ms: int, now_ms: int) -> bool:
        return until_ms == _PAUSE_INDEFINITE or until_ms > now_ms

    def _is_global_pause_active(self, *, now_ms: int | None = None) -> bool:
        now = self._now_ms() if now_ms is None else now_ms
        return self._is_pause_until_active(self._global_pause_until_ms, now)

    def _prune_expired_pauses(self, *, persist: bool = True, now_ms: int | None = None) -> bool:
        now = self._now_ms() if now_ms is None else now_ms
        changed = False

        if self._global_pause_until_ms > 0 and self._global_pause_until_ms <= now:
            self._global_pause_until_ms = 0
            changed = True

        expired = [
            key for key, until in self._chat_pause_until_ms.items() if until > 0 and until <= now
        ]
        if expired:
            changed = True
            for key in expired:
                self._chat_pause_until_ms.pop(key, None)

        if changed and persist:
            self._save_pause_state()
        return changed

    def _pause_reason_for_chat(self, channel: str, chat_id: str) -> str | None:
        now = self._now_ms()
        changed = self._prune_expired_pauses(persist=False, now_ms=now)
        if changed:
            try:
                self._save_pause_state()
            except Exception:
                pass
        if self._is_pause_until_active(self._global_pause_until_ms, now):
            return "paused_global"
        pause_until = self._chat_pause_until_ms.get(self._pause_key(channel, chat_id), 0)
        if self._is_pause_until_active(pause_until, now):
            return "paused_chat"
        return None

    @staticmethod
    def _format_duration_seconds(seconds: int) -> str:
        if seconds % 86_400 == 0:
            days = seconds // 86_400
            return f"{days}d"
        if seconds % 3_600 == 0:
            hours = seconds // 3_600
            return f"{hours}h"
        if seconds % 60 == 0:
            minutes = seconds // 60
            return f"{minutes}m"
        return f"{seconds}s"

    @classmethod
    def _parse_pause_duration_ms(cls, raw: str, *, max_seconds: int | None = None) -> int:
        token = "".join(part.strip() for part in raw.split()).lower()
        if not token:
            raise ValueError("duration is required (example: 30min, 1h)")
        match = _PAUSE_DURATION_PATTERN.match(token)
        if match is None:
            raise ValueError("invalid duration format (example: 30min, 1h)")
        value = int(match.group("value"))
        unit = match.group("unit") or "m"
        multiplier = _PAUSE_DURATION_UNITS.get(unit)
        if multiplier is None:
            raise ValueError("unsupported duration unit (use s, min, h, or d)")
        total_seconds = value * multiplier
        if total_seconds < 1:
            raise ValueError("duration must be at least 1 second")
        if max_seconds is not None and total_seconds > max_seconds:
            raise ValueError("duration must be 120 minutes or less")
        return total_seconds * 1000

    def _set_chat_pause(self, *, channel: str, chat_id: str, until_ms: int) -> None:
        normalized = self._normalize_pause_until(until_ms)
        if normalized == 0:
            self._chat_pause_until_ms.pop(self._pause_key(channel, chat_id), None)
        else:
            self._chat_pause_until_ms[self._pause_key(channel, chat_id)] = normalized
        self._save_pause_state()

    def _clear_chat_pause(self, *, channel: str, chat_id: str) -> bool:
        removed = self._chat_pause_until_ms.pop(self._pause_key(channel, chat_id), None)
        if removed is None:
            return False
        self._save_pause_state()
        return True

    def _set_global_pause(self, until_ms: int) -> None:
        self._global_pause_until_ms = self._normalize_pause_until(until_ms)
        self._save_pause_state()

    def _clear_all_pauses(self) -> bool:
        changed = self._global_pause_until_ms != 0 or bool(self._chat_pause_until_ms)
        if not changed:
            return False
        self._global_pause_until_ms = 0
        self._chat_pause_until_ms.clear()
        self._save_pause_state()
        return True

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
        if (
            current_mtime is None
            and not self._policy_file_was_present
            and self._policy_reload_error is None
        ):
            return
        if self._policy_reload_error is None and current_mtime == self._last_mtime_ns:
            return

        try:
            if current_mtime is None and self._policy_file_was_present:
                raise FileNotFoundError(f"policy file missing: {self._policy_path}")
            new_policy = load_policy(self._policy_path)
            if self._stat_mtime_ns() is None:
                raise FileNotFoundError(f"policy file missing: {self._policy_path}")
            new_engine = PolicyEngine(
                policy=new_policy,
                workspace=self._engine.workspace,
                apply_channels=self._engine.apply_channels,
            )
            new_engine.validate(self._known_tools)
        except Exception as exc:
            self._record_policy_reload_error(current_mtime, exc)
            return

        had_error = self._policy_reload_error is not None
        self._engine = new_engine
        self._last_mtime_ns = current_mtime
        self._policy_file_was_present = True
        self._policy_reload_error = None
        self._policy_loaded_ms = self._now_ms()
        if had_error:
            logger.info("policy reload recovered version={}", current_mtime)

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
        self._policy_file_was_present = True
        self._last_reload_check = time.monotonic()
        self._policy_reload_error = None
        self._policy_loaded_ms = self._now_ms()

    def policy_engine(self) -> "PolicyEngine | None":
        """Currently loaded engine. Callers must treat it as read-only."""
        return self._engine

    def policy_snapshot(self) -> "PolicySnapshot":
        """Immutable snapshot of the loaded policy plus the identity it was loaded as.

        Version and content hash are derived from the engine that decides - not from the
        file on disk, which may already be a newer, not-yet-reloaded version (spec R02).
        A known reload failure is reported as unhealthy so new effect paths block.
        """
        from yeoman_gateway.processing.models import PolicySnapshot, canonical_hash

        source = str(self._policy_path) if self._policy_path is not None else "in-memory"
        error = self._policy_reload_error[1] if self._policy_reload_error is not None else None
        if self._engine is None:
            return PolicySnapshot(
                version="unloaded",
                policy_hash="",
                policy=None,
                loaded_ms=self._policy_loaded_ms,
                healthy=False,
                source=source,
                error=error or "policy engine not loaded",
            )
        policy = self._engine.policy
        policy_hash = canonical_hash(policy.model_dump(mode="json"))
        if self._policy_path is not None and self._last_mtime_ns is not None:
            version = f"{self._policy_path.name}@{self._last_mtime_ns}"
        else:
            version = f"memory:{policy_hash[:16]}"
        return PolicySnapshot(
            version=version,
            policy_hash=policy_hash,
            policy=policy.model_copy(deep=True),
            loaded_ms=self._policy_loaded_ms,
            healthy=self._policy_reload_error is None,
            source=source,
            error=error,
        )

    def current_policy_snapshot(self) -> "PolicySnapshot":
        """Refresh the existing provider before a protected execution claim."""
        self._maybe_reload()
        return self.policy_snapshot()

    @override
    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        if self._engine is None:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset(self._known_tools),
                reason="policy_disabled",
                when_to_reply_mode="all",
                notes_enabled=False,
                notes_mode="adaptive",
                notes_allow_blocked_senders=False,
                notes_batch_interval_seconds=1800,
                notes_batch_max_messages=100,
                voice_output_mode="text",
                voice_output_tts_route="tts.speak",
                voice_output_voice="alloy",
                voice_output_format="opus",
                voice_output_max_sentences=3,
                voice_output_max_chars=500,
                talkative_cooldown_enabled=False,
                talkative_cooldown_streak_threshold=7,
                talkative_cooldown_topic_overlap_threshold=0.34,
                talkative_cooldown_cooldown_seconds=900,
                talkative_cooldown_delay_seconds=2.5,
                talkative_cooldown_use_llm_message=False,
                reply_budget={},
                session_history_limit=None,
                source="disabled",
            )

        self._maybe_reload()
        if self._policy_reload_error is not None:
            return PolicyDecision(
                accept_message=False,
                should_respond=False,
                allowed_tools=frozenset(),
                reason="policy_reload_failed",
                when_to_reply_mode="off",
                source=str(self._policy_path) if self._policy_path else "policy_reload_failed",
            )
        actor = _to_actor(event)
        decision = self._engine.evaluate(actor, self._known_tools)
        is_owner = self._engine.is_owner(actor)
        if event.channel == "whatsapp" and bool(event.raw_metadata.get("lid_conflict")):
            is_owner = False
        voice_output_mode = "text"
        voice_output_tts_route = "tts.speak"
        voice_output_voice = "alloy"
        voice_output_format = "opus"
        voice_output_max_sentences = 3
        voice_output_max_chars = 500
        talkative_cooldown_enabled = False
        talkative_cooldown_streak_threshold = 7
        talkative_cooldown_topic_overlap_threshold = 0.34
        talkative_cooldown_cooldown_seconds = 900
        talkative_cooldown_delay_seconds = 2.5
        talkative_cooldown_use_llm_message = False
        contacts_disclosure = False
        session_history_limit: int | None = None
        reply_budget: dict[str, object] = {}
        model_profile: str | None = None
        when_to_reply_mode: Literal[
            "all", "mention_only", "allowed_senders", "owner_only", "off"
        ] = "all"
        if event.channel in self._engine.apply_channels:
            try:
                effective = self._engine.resolve_policy(event.channel, event.chat_id)
                when_to_reply_mode = effective.when_to_reply_mode  # type: ignore[assignment]
                voice_output_mode = effective.voice_output_mode
                voice_output_tts_route = effective.voice_output_tts_route
                voice_output_voice = effective.voice_output_voice
                voice_output_format = effective.voice_output_format
                voice_output_max_sentences = effective.voice_output_max_sentences
                voice_output_max_chars = effective.voice_output_max_chars
                talkative_cooldown_enabled = effective.talkative_cooldown_enabled
                talkative_cooldown_streak_threshold = effective.talkative_cooldown_streak_threshold
                talkative_cooldown_topic_overlap_threshold = (
                    effective.talkative_cooldown_topic_overlap_threshold
                )
                talkative_cooldown_cooldown_seconds = effective.talkative_cooldown_cooldown_seconds
                talkative_cooldown_delay_seconds = effective.talkative_cooldown_delay_seconds
                talkative_cooldown_use_llm_message = effective.talkative_cooldown_use_llm_message
                contacts_disclosure = effective.contacts_disclosure
                session_history_limit = effective.session_history_limit
                reply_budget = dict(effective.reply_budget)
                model_profile = effective.model_profile
            except Exception:
                # Policy voice output settings are optional and should never break evaluation.
                pass
        notes = self._engine.resolve_memory_notes(
            channel=event.channel,
            chat_id=event.chat_id,
            is_group=event.is_group,
        )
        pause_reason = self._pause_reason_for_chat(event.channel, event.chat_id)
        should_respond = decision.should_respond
        reason = decision.reason
        if pause_reason is not None and decision.accept_message:
            should_respond = False
            reason = pause_reason
        handoff = self._active_private_handoff(event)
        if (
            handoff is not None
            and pause_reason is None
            and decision.reason != "blocked_sender"
        ):
            return self._private_handoff_decision(
                event=event,
                handoff=handoff,
                notes=notes,
                is_owner=is_owner,
            )
        return PolicyDecision(
            accept_message=decision.accept_message,
            should_respond=should_respond,
            allowed_tools=frozenset(decision.allowed_tools),
            reason=reason,
            when_to_reply_mode=when_to_reply_mode,
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
            talkative_cooldown_enabled=talkative_cooldown_enabled,
            talkative_cooldown_streak_threshold=talkative_cooldown_streak_threshold,
            talkative_cooldown_topic_overlap_threshold=talkative_cooldown_topic_overlap_threshold,
            talkative_cooldown_cooldown_seconds=talkative_cooldown_cooldown_seconds,
            talkative_cooldown_delay_seconds=talkative_cooldown_delay_seconds,
            talkative_cooldown_use_llm_message=talkative_cooldown_use_llm_message,
            reply_budget=reply_budget,
            model_profile=model_profile,
            contacts_disclosure=contacts_disclosure,
            session_history_limit=session_history_limit,
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
                    "talkativeCooldown": {
                        "enabled": effective.talkative_cooldown_enabled,
                        "streakThreshold": effective.talkative_cooldown_streak_threshold,
                        "topicOverlapThreshold": effective.talkative_cooldown_topic_overlap_threshold,
                        "cooldownSeconds": effective.talkative_cooldown_cooldown_seconds,
                        "delaySeconds": effective.talkative_cooldown_delay_seconds,
                        "useLlmMessage": effective.talkative_cooldown_use_llm_message,
                    },
                    "replyBudget": effective.reply_budget,
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
        approval = self._handle_group_approval_reply(event)
        if approval is not None:
            return approval
        return self._admin_router.route(_to_admin_context(event))

    def session_boundary_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def command_catalog_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def voice_send_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def response_control_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx))

    def _get_group_name(self, chat_id: str) -> str | None:
        """Get group name from chat_registry or bridge."""
        # Try chat_registry first
        try:
            from yeoman_gateway.storage.chat_registry import ChatRegistry

            registry = ChatRegistry()
            try:
                chat_info = registry.get_chat("whatsapp", chat_id)
                if chat_info:
                    name = chat_info.get("readable_name")
                    if name:
                        return name
            finally:
                registry.close()
        except Exception:
            pass

        # Try bridge lookup
        try:
            names = self._list_group_subjects_from_bridge([chat_id])
            if names.get(chat_id):
                return names[chat_id]
        except Exception:
            pass

        return None

    def _handle_group_approval_reply(self, event: InboundEvent) -> AdminCommandResult | None:
        choice = event.content.strip().casefold()
        if choice not in {"yes", "ja", "no", "nein"} or event.is_group:
            return None
        ctx = _to_admin_context(event)
        policy = self._owner_policy_for_context(ctx)
        if policy is None:
            return None
        match = _GROUP_APPROVAL_TARGET_PATTERN.search(event.reply_to_text or "")
        if match is None:
            return None
        chat_id = self._parse_group_chat_id(match.group(1))
        approved = choice in {"yes", "ja"}
        override = self._whatsapp_chat_override(policy, chat_id)
        override.who_can_talk = WhoCanTalkPolicyOverride(
            mode="everyone" if approved else "allowlist",
            senders=[] if approved else list(policy.owners.get("whatsapp", [])),
        )
        override.when_to_reply = WhenToReplyPolicyOverride(
            mode="mention_only" if approved else "off",
            senders=[],
        )
        override.spontaneity = SpontaneityPolicyOverride(enabled=False)
        group_name = self._get_group_name(chat_id)
        if group_name and not override.comment:
            override.comment = group_name
        try:
            self._save_policy_and_reload(policy)
        except Exception as exc:
            return AdminCommandResult(
                status="handled",
                response=f"Failed to apply group decision: {exc}",
                command_name="group-approval",
                outcome="error",
                source="dm",
            )
        action = "Approved" if approved else "Blocked"
        detail = "mention-only, spontaneity off" if approved else "replies off"
        return AdminCommandResult(
            status="handled",
            response=f"{action} {chat_id}: {detail}.",
            command_name="group-approval",
            outcome="applied",
            source="dm",
        )

    def panic_is_applicable(self, ctx: AdminCommandContext) -> bool:
        return bool(self._owner_policy_for_context(ctx)) and not ctx.is_group

    def panic_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        delay_s = 1.0
        if argv:
            if len(argv) == 1 and argv[0].strip().lower() in {"now", "--now"}:
                delay_s = 0.0
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /panic [now]",
                    command_name="panic",
                    outcome="invalid",
                    source="dm",
                )

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Panic unavailable: policy engine is not active.",
                command_name="panic",
                outcome="error",
                source="dm",
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")

        self._trigger_panic_shutdown(delay_s=delay_s)
        suffix = "" if delay_s <= 0 else " (after ack)"
        return AdminCommandResult(
            status="handled",
            response=f"Panic switch engaged. Stopping gateway and WhatsApp bridge{suffix}.",
            command_name="panic",
            outcome="applied",
            source="dm",
            metric_events=(
                AdminMetricEvent(name="panic_switch_total", labels=(("channel", ctx.channel),)),
            ),
        )

    def stop_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        source = "dm" if not ctx.is_group else "group"
        scope = "chat"
        if argv:
            if len(argv) == 1 and argv[0].strip().lower() == "all":
                scope = "all"
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /stop or /stop all",
                    command_name="stop",
                    outcome="invalid",
                    source=source,
                )

        if scope == "all" and ctx.is_group:
            return AdminCommandResult(
                status="handled",
                response="Global response controls are available only in the owner DM.",
                command_name="stop",
                outcome="invalid",
                source=source,
            )

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Stop unavailable: policy engine is not active.",
                command_name="stop",
                outcome="error",
                source=source,
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")

        self._prune_expired_pauses()
        try:
            if scope == "all":
                self._set_global_pause(_PAUSE_INDEFINITE)
                response = "⏸️ Responses paused for all chats until /start all."
            else:
                self._set_chat_pause(
                    channel=ctx.channel, chat_id=ctx.chat_id, until_ms=_PAUSE_INDEFINITE
                )
                if self._is_global_pause_active():
                    response = (
                        "⏸️ Responses paused for this chat. "
                        "Global pause is active too; use /start all to resume everywhere."
                    )
                else:
                    response = "⏸️ Responses paused for this chat until /start."
        except Exception as e:
            return AdminCommandResult(
                status="handled",
                response=f"Failed to apply stop command: {e}",
                command_name="stop",
                outcome="error",
                source=source,
            )

        return AdminCommandResult(
            status="handled",
            response=response,
            command_name="stop",
            outcome="applied",
            source=source,
            metric_events=(
                AdminMetricEvent(
                    name="response_pause_set_total",
                    labels=(("channel", ctx.channel), ("scope", scope), ("mode", "indefinite")),
                ),
            ),
        )

    def pause_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        source = "dm" if not ctx.is_group else "group"
        scope = "chat"
        duration_parts = argv
        if argv and argv[0].strip().lower() == "all":
            scope = "all"
            duration_parts = argv[1:]

        if scope == "all" and ctx.is_group:
            return AdminCommandResult(
                status="handled",
                response="Global response controls are available only in the owner DM.",
                command_name="pause",
                outcome="invalid",
                source=source,
            )
        if not duration_parts:
            if scope == "all":
                duration_ms = _PAUSE_INDEFINITE
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /pause <duration> or /pause all [duration]",
                    command_name="pause",
                    outcome="invalid",
                    source=source,
                )
        else:
            duration_expr = "".join(part.strip() for part in duration_parts)
            try:
                duration_ms = self._parse_pause_duration_ms(
                    duration_expr,
                    max_seconds=2 * 60 * 60 if scope == "chat" else None,
                )
            except ValueError as e:
                return AdminCommandResult(
                    status="handled",
                    response=f"Invalid pause duration: {e}",
                    command_name="pause",
                    outcome="invalid",
                    source=source,
                )

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Pause unavailable: policy engine is not active.",
                command_name="pause",
                outcome="error",
                source=source,
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")

        self._prune_expired_pauses()
        until_ms = (
            _PAUSE_INDEFINITE if duration_ms == _PAUSE_INDEFINITE else self._now_ms() + duration_ms
        )
        duration_text = (
            "until /start all"
            if duration_ms == _PAUSE_INDEFINITE
            else f"for {self._format_duration_seconds(duration_ms // 1000)}"
        )
        try:
            if scope == "all":
                self._set_global_pause(until_ms)
                response = f"⏸️ Responses paused for all chats {duration_text}."
            else:
                self._set_chat_pause(channel=ctx.channel, chat_id=ctx.chat_id, until_ms=until_ms)
                if self._is_global_pause_active():
                    response = (
                        f"⏸️ Responses paused for this chat {duration_text}. "
                        "Global pause is active too; use /start all to resume everywhere."
                    )
                else:
                    response = f"⏸️ Responses paused for this chat {duration_text}. Use /start to resume sooner."
        except Exception as e:
            return AdminCommandResult(
                status="handled",
                response=f"Failed to apply pause command: {e}",
                command_name="pause",
                outcome="error",
                source=source,
            )

        return AdminCommandResult(
            status="handled",
            response=response,
            command_name="pause",
            outcome="applied",
            source=source,
            metric_events=(
                AdminMetricEvent(
                    name="response_pause_set_total",
                    labels=(("channel", ctx.channel), ("scope", scope), ("mode", "timed")),
                ),
            ),
        )

    def start_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        source = "dm" if not ctx.is_group else "group"
        scope = "chat"
        if argv:
            if len(argv) == 1 and argv[0].strip().lower() == "all":
                scope = "all"
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /start or /start all",
                    command_name="start",
                    outcome="invalid",
                    source=source,
                )

        if scope == "all" and ctx.is_group:
            return AdminCommandResult(
                status="handled",
                response="Global response controls are available only in the owner DM.",
                command_name="start",
                outcome="invalid",
                source=source,
            )

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Start unavailable: policy engine is not active.",
                command_name="start",
                outcome="error",
                source=source,
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")

        self._prune_expired_pauses()
        try:
            if scope == "all":
                changed = self._clear_all_pauses()
                response = (
                    "✅ Responses resumed for all chats."
                    if changed
                    else "Responses are already active everywhere."
                )
            else:
                cleared = self._clear_chat_pause(channel=ctx.channel, chat_id=ctx.chat_id)
                if self._is_global_pause_active():
                    if cleared:
                        response = (
                            "Cleared chat-specific pause for this chat. "
                            "Global pause is still active; use /start all to resume everywhere."
                        )
                    else:
                        response = (
                            "Global pause is still active; use /start all to resume everywhere."
                        )
                else:
                    response = (
                        "✅ Responses resumed for this chat."
                        if cleared
                        else "Responses are already active for this chat."
                    )
        except Exception as e:
            return AdminCommandResult(
                status="handled",
                response=f"Failed to apply start command: {e}",
                command_name="start",
                outcome="error",
                source=source,
            )

        return AdminCommandResult(
            status="handled",
            response=response,
            command_name="start",
            outcome="applied",
            source=source,
            metric_events=(
                AdminMetricEvent(
                    name="response_pause_cleared_total",
                    labels=(("channel", ctx.channel), ("scope", scope)),
                ),
            ),
        )

    def new_session_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(status="ignored")
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")
        if self._session_manager is None:
            return AdminCommandResult(status="ignored")

        session_key = f"{ctx.channel}:{ctx.chat_id}"
        try:
            session = self._session_manager.get_or_create(session_key)
            session.add_boundary()
            self._session_manager.save(session)
        except Exception as e:
            return AdminCommandResult(status="handled", response=f"Session boundary failed: {e}")

        if self._processing_store is not None:
            try:
                self._processing_store.close_chat_threads(
                    channel=ctx.channel,
                    chat_id=ctx.chat_id,
                    now_ms=self._now_ms(),
                )
            except Exception as e:
                return AdminCommandResult(status="handled", response=f"Thread boundary failed: {e}")

        return AdminCommandResult(
            status="handled",
            response=None,
            reaction_emoji="\U0001f44d",
            command_name="new",
            outcome="applied",
            source="dm" if not ctx.is_group else "group",
        )

    def command_catalog_handle(
        self, ctx: AdminCommandContext, argv: list[str]
    ) -> AdminCommandResult:
        if argv:
            normalized = argv[0].strip().lower()
            if len(argv) == 1 and normalized in {"help", "-h", "--help"}:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /commands",
                    command_name="commands",
                    outcome="applied",
                    source="dm" if not ctx.is_group else "group",
                )
            else:
                return AdminCommandResult(
                    status="handled",
                    response="Usage: /commands",
                    command_name="commands",
                    outcome="invalid",
                    source="dm" if not ctx.is_group else "group",
                )

        lines = [
            "Available slash commands for this chat:",
            "- /commands — list available commands",
            "- /help — alias for /commands",
            "- /new — close current threads and start fresh context",
            "- /stop — pause this chat until /start",
            "- /pause <duration> — pause this chat for up to 120 minutes",
            "- /start — resume this chat",
            '- /voice "group" "message" — send a voice note (owner)',
        ]
        if not ctx.is_group:
            lines.extend(
                [
                    "- /stop all or /pause all — pause all chats until /start all",
                    "- /pause all <duration> — pause all chats for any duration",
                    "- /start all — resume all chats",
                ]
            )
        if self.panic_is_applicable(ctx):
            lines.append("- /panic [now] — emergency stop gateway + WhatsApp bridge")
        return AdminCommandResult(
            status="handled",
            response="\n".join(lines),
            command_name="commands",
            outcome="applied",
            source="dm" if not ctx.is_group else "group",
        )

    # ── /voice ad-hoc send ────────────────────────────────────────────────

    _voice_send_callback: Any | None = None
    _admin_notify_callback: Any | None = None  # async (channel, chat_id, text) -> None

    def set_voice_send_callback(
        self,
        callback: Any,
    ) -> None:
        """Set async callback for an owner-requested voice delivery."""
        self._voice_send_callback = callback

    def set_admin_notify_callback(
        self,
        callback: Any,
    ) -> None:
        """Set async callback: (channel: str, chat_id: str, text: str) -> None."""
        self._admin_notify_callback = callback

    def voice_send_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        if len(argv) < 2:
            return AdminCommandResult(
                status="handled",
                response='Usage: /voice "group name or slug" "message to speak"',
                command_name="voice",
                outcome="invalid",
            )

        group_ref = argv[0]
        message = " ".join(argv[1:])

        # Resolve group to verify it exists.
        chat_id, err = self.resolve_whatsapp_group(group_ref)
        if err or not chat_id:
            return AdminCommandResult(
                status="handled",
                response=f"Could not resolve group: {err or 'unknown'}",
                command_name="voice",
                outcome="error",
            )

        if self._voice_send_callback is None:
            return AdminCommandResult(
                status="handled",
                response="Voice sending is not configured.",
                command_name="voice",
                outcome="error",
            )

        # Fire-and-forget: schedule async TTS + send, report result to source chat.
        loop = asyncio.get_running_loop()
        callback = self._voice_send_callback
        notify = self._admin_notify_callback
        source_channel = ctx.channel
        source_chat_id = ctx.chat_id

        async def _do_send() -> None:
            from loguru import logger

            try:
                result = await callback(message, chat_id, source_chat_id, ctx.sender_id)
            except Exception as exc:
                result = f"Error: /voice exception ({type(exc).__name__}: {exc})"

            result = str(result or "").strip()
            is_error = result.lower().startswith("error")

            if is_error:
                logger.error("/voice send failed for {}: {}", chat_id, result)
            else:
                logger.info("/voice send ok for {}: {}", chat_id, result)

            if notify is not None:
                try:
                    await notify(source_channel, source_chat_id, result)
                except Exception as exc:
                    logger.error("/voice notify failed: {}", exc)

        loop.create_task(_do_send())

        short_group = group_ref if len(group_ref) <= 30 else group_ref[:27] + "..."
        return AdminCommandResult(
            status="handled",
            response=f"Sending voice to {short_group}...",
            command_name="voice",
            outcome="sent",
            metric_events=(
                AdminMetricEvent(
                    name="voice_send_command_total",
                    labels=(("channel", ctx.channel),),
                ),
            ),
        )

    def _load_policy_for_admin(self) -> PolicyConfig | None:
        if self._engine is None or self._policy_path is None:
            return None
        self._maybe_reload()
        try:
            return load_policy(self._policy_path)
        except Exception as exc:
            if self._policy_file_was_present or self._policy_reload_error is not None:
                self._record_policy_reload_error(self._stat_mtime_ns(), exc)
                return self._engine.policy
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

    @staticmethod
    def _panic_shutdown_worker(delay_s: float) -> None:
        if delay_s > 0:
            time.sleep(delay_s)

        config = load_config()
        try:
            from yeoman_gateway.cli.commands import _stop_gateway_processes

            _stop_gateway_processes(config.gateway.port)
        except Exception:
            pass

        try:
            from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager

            runtime = WhatsAppRuntimeManager(config=config)
            runtime.stop_bridge()
        except Exception:
            pass

    def _trigger_panic_shutdown(self, *, delay_s: float) -> None:
        worker = threading.Thread(
            target=self._panic_shutdown_worker,
            args=(max(0.0, float(delay_s)),),
            daemon=True,
            name="yeoman-panic-shutdown",
        )
        worker.start()

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
        try:
            save_policy(policy, self._policy_path)
        except Exception as exc:
            self._record_policy_reload_error(self._stat_mtime_ns(), exc)
            raise
        self._engine = new_engine
        self._last_mtime_ns = self._stat_mtime_ns()
        self._policy_file_was_present = True
        self._last_reload_check = time.monotonic()
        self._policy_reload_error = None


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
                "version": PROTOCOL_VERSION,
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
                    if data.get("version") != PROTOCOL_VERSION:
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


class NewSessionCommandHandler(AdminCommandHandler):
    """Deterministic `/new` command for inserting a session boundary."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "new"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.session_boundary_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.new_session_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/new"


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


class StopCommandHandler(AdminCommandHandler):
    """Deterministic `/stop` command for silencing chat replies."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "stop"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.response_control_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.stop_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/stop [all]"


class PauseCommandHandler(AdminCommandHandler):
    """Deterministic `/pause` command for timed silencing."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "pause"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.response_control_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.pause_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/pause <duration>"


class StartCommandHandler(AdminCommandHandler):
    """Deterministic `/start` command for resuming silenced replies."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "start"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.response_control_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.start_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/start [all]"


class PanicCommandHandler(AdminCommandHandler):
    """Deterministic `/panic` command for emergency process shutdown."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "panic"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.panic_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.panic_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/panic"


class VoiceSendCommandHandler(AdminCommandHandler):
    """Ad-hoc `/voice` command: synthesize TTS and send to a WhatsApp group.

    Usage: /voice "group name or slug" "message to speak"
    """

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "voice"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.voice_send_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.voice_send_handle(ctx, argv)

    def help_hint(self) -> str:
        return '/voice "group" "message"'
