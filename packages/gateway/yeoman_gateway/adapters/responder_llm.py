"""Typed responder that runs LLM + tools without legacy AgentLoop."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, override

from loguru import logger
from yeoman_shared.telemetry import tracing as lf

from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.agent.subagent import SubagentManager
from yeoman_gateway.agent.tools.contacts import ContactsTool
from yeoman_gateway.agent.tools.cron import CronTool
from yeoman_gateway.agent.tools.exec_isolation import SandboxMount
from yeoman_gateway.agent.tools.file_access import FileAccessResolver, enable_grants
from yeoman_gateway.agent.tools.filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from yeoman_gateway.agent.tools.market_data import MarketIntelligenceTool, MarketQuoteTool
from yeoman_gateway.agent.tools.message import MessageTool
from yeoman_gateway.agent.tools.ops import OpsTool
from yeoman_gateway.agent.tools.ops_manage import OpsManageTool
from yeoman_gateway.agent.tools.registry import ToolRegistry
from yeoman_gateway.agent.tools.send_voice import SendVoiceTool, VoiceSendRequest
from yeoman_gateway.agent.tools.shell import ExecTool
from yeoman_gateway.agent.tools.spawn import SpawnTool
from yeoman_gateway.agent.tools.web import (
    DeepResearchTool,
    WebCrawlTool,
    WebFetchTool,
    WebMapTool,
    WebSearchTool,
    YoutubeTranscriptTool,
)
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.ports import ResponderPort, SecurityPort, TelemetryPort
from yeoman_gateway.media.tts import (
    strip_markdown_for_tts,
    truncate_for_voice,
    write_tts_audio_file,
)
from yeoman_gateway.policy.identity import normalize_sender_list
from yeoman_gateway.providers.base import LLMProvider, ToolCallRequest
from yeoman_gateway.session.manager import SessionManager

if TYPE_CHECKING:
    from yeoman_shared.config.schema import ExecToolConfig, WebToolsConfig

    from yeoman_gateway.caldav.service import CalDAVService
    from yeoman_gateway.contacts.service import ContactsService
    from yeoman_gateway.cron.service import CronService
    from yeoman_gateway.media.lazy_resolver import LazyMediaResolver
    from yeoman_gateway.media.router import ModelRouter
    from yeoman_gateway.media.tts import TTSSynthesizer
    from yeoman_gateway.memory.service import MemoryService
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    from yeoman_gateway.storage.private_handoff import PrivateHandoffStore


_BACKWARD_REF_RE = re.compile(
    r"\b(?:"
    r"as (?:we|i|you) (?:discussed|said|mentioned|talked)"
    r"|you (?:said|mentioned|told me|suggested)"
    r"|remember when|go back to"
    r"|we (?:discussed|agreed|decided|talked about)"
    r")\b",
    re.IGNORECASE,
)
_BANTER_MARKER_RE = re.compile(
    r"\b(?:"
    r"cringe|glitzer|bro|bruder|junge|haha|lol|lmao|witz|joke|roast|meme|"
    r"lost|cope|based|skill issue|killer|mutter"
    r")\b",
    re.IGNORECASE,
)
_NEW_VALUE_REQUEST_RE = re.compile(
    r"\b(?:"
    r"erkl(?:ä|ae)r\w*|check|such|fass|analys|warum|wieso|weshalb|quelle|quellen|"
    r"zahl(?:en)?|news|chart|preis|aktuell|strategie|risk|risiko|bewert|"
    r"rechne|vergleich|was ist|wie genau|wie findest|wann aussteigen|"
    r"explain|why|how|source|sources|search|look up|summari[sz]e|calculate|compare"
    r")\b",
    re.IGNORECASE,
)
_RETRY_REFINEMENT_REQUEST_RE = re.compile(
    r"\b(?:"
    r"noch\s*mal|nochmal|erneut|retry|again|try again|versuch\w*|"
    r"mach\w*|jetzt aber mit|das war nicht|war nicht|not right"
    r")\b",
    re.IGNORECASE,
)
_VOICE_REFINEMENT_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"sprachnachricht|voice|audio|melodie|melody|sing|singen|gesungen|sung|"
    r"lied|song|tts|stimme"
    r")\b",
    re.IGNORECASE,
)
_CLARIFYING_QUESTION_START_RE = re.compile(
    r"^\s*(?:"
    r"was|wer|wie|warum|wieso|weshalb|wo|wann|welch(?:e|er|es|en|em)?|"
    r"kannst|kann|soll|sollen|meinst|brauchst|"
    r"what|who|how|why|where|when|which|can|could|should|do|does|did"
    r")\b",
    re.IGNORECASE,
)
_TEXTUAL_TOOL_COERCION_SAFE_TOOLS = frozenset(
    {
        "browse",
        "deep_research",
        "fact_check",
        "market_intelligence",
        "market_quote",
        "media_history",
        "ops",
        "recall_conversation",
        "summarize_history",
        "web_fetch",
        "web_search",
        "youtube_transcript",
    }
)
_DEFERRED_WORK_PROMISE_RE = re.compile(
    r"\b(?:"
    r"ich\s+(?:schau(?:e)?|checke|pr(?:ü|ue)fe|suche|recherchiere|gucke)\b"
    r"|ich\s+muss\s+(?:erst\s+)?[^.!?]{0,90}\b"
    r"(?:checken|pr(?:ü|ue)fen|suchen|nachschauen|recherchieren)\b"
    r"|(?:let me|i(?:'ll| will| need to| have to))\s+[^.!?]{0,90}\b"
    r"(?:check|look up|search|verify|fetch)\b"
    r"|(?:einen?\s+moment|moment\s+(?:kurz|bitte)|one moment|give me a moment)"
    r")\b",
    re.IGNORECASE,
)
_DELIVERY_TOOLS = frozenset({"message", "send_voice", "send_media"})
_DELIVERY_TARGET_STOPWORDS = frozenset(
    {
        "da",
        "das",
        "dem",
        "den",
        "der",
        "die",
        "dich",
        "einen",
        "eine",
        "euch",
        "hier",
        "me",
        "mich",
        "mir",
        "the",
        "this",
        "uns",
    }
)


def _looks_like_deferred_work_promise(content: str | None) -> bool:
    if not content:
        return False
    compact = " ".join(content.split()).strip()
    if not compact or len(compact) > 220:
        return False
    if "?" in compact:
        return False
    return bool(_DEFERRED_WORK_PROMISE_RE.search(compact))


_DELIVERY_TARGET_PREPOSITION_RE = re.compile(
    r"(?iu)\b(?:an|to|für|fuer|zu)\s+(?:(?:die|den|der|das|dem|the)\s+)?([@\w+][\w.+-]*)"
)
_DELIVERY_TARGET_AFTER_VERB_RE = re.compile(
    r"(?iu)\b(?:schick|schicke|sende|send|schreib|schreibe)\s+([@\w+][\w.+-]*)"
)
_PRIVATE_DELIVERY_INTENT_RE = re.compile(
    r"(?iu)\b(?:privat|private|dm|pn|pm|direktnachricht|direct\s+message|"
    r"off[-\s]?chat|pers(?:ö|oe)nlich)\b"
)
_DELIVERY_SUCCESS_RE = re.compile(
    r"^(?:Voice message|Message) delivered to (?P<channel>[^:]+):"
    r"(?P<chat_id>.+?)(?:\. Delivery complete\b.*|\.$)",
    re.IGNORECASE,
)
_PHONE_CHARS_RE = re.compile(r"[\s().-]+")


def _has_backward_reference(text: str) -> bool:
    """Return True if the message appears to reference earlier conversation."""
    return bool(_BACKWARD_REF_RE.search(text))


def _delivery_args_have_explicit_target(arguments: dict[str, Any]) -> bool:
    for key in ("chat_id", "group"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _normalize_whatsapp_chat_id_for_delivery(chat_id: str) -> str:
    token = str(chat_id or "").strip()
    if not token or "@" in token:
        return token
    phone = _PHONE_CHARS_RE.sub("", token.removeprefix("+"))
    if phone.isdigit():
        return f"{phone}@s.whatsapp.net"
    return token


def _delivery_arg_chat_id(arguments: dict[str, Any]) -> str:
    value = arguments.get("chat_id")
    return str(value or "").strip() if isinstance(value, str) else ""


def _delivery_targets_private_chat(
    *,
    arguments: dict[str, Any],
    current_channel: str,
    current_chat_id: str,
) -> bool:
    if current_channel != "whatsapp" or not current_chat_id.endswith("@g.us"):
        return False
    if str(arguments.get("group") or "").strip():
        return False
    target = _delivery_arg_chat_id(arguments)
    if not target:
        return False
    normalized = _normalize_whatsapp_chat_id_for_delivery(target)
    if not normalized or normalized == current_chat_id:
        return False
    return not normalized.endswith("@g.us")


def _private_delivery_intent_present(text: str) -> bool:
    return bool(_PRIVATE_DELIVERY_INTENT_RE.search(str(text or "")))


def _whatsapp_aliases(*values: str) -> set[str]:
    return set(normalize_sender_list("whatsapp", [v for v in values if str(v or "").strip()]))


def _delivery_request_names_target(text: str) -> bool:
    compact = " ".join(str(text or "").strip().split())
    if not compact:
        return False

    for pattern in (_DELIVERY_TARGET_PREPOSITION_RE, _DELIVERY_TARGET_AFTER_VERB_RE):
        for match in pattern.finditer(compact):
            token = str(match.group(1) or "").strip(" ,.:;!?()[]{}\"'")
            if not token:
                continue
            if token.lower() in _DELIVERY_TARGET_STOPWORDS:
                continue
            return True
    return False


def _delivery_repair_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    current_user_message: str,
    current_channel: str = "",
    current_chat_id: str = "",
) -> str | None:
    if tool_name not in _DELIVERY_TOOLS:
        return None
    if (
        _delivery_targets_private_chat(
            arguments=arguments,
            current_channel=current_channel,
            current_chat_id=current_chat_id,
        )
        and not _private_delivery_intent_present(current_user_message)
    ):
        return (
            "Repair required: this group-chat request does not explicitly ask for "
            "private/off-chat delivery, but the delivery tool targets a personal chat. "
            "Do not send it privately. Ask one short clarification question or use "
            "the current group if that is clearly intended."
        )
    if _delivery_args_have_explicit_target(arguments):
        return None
    if not _delivery_request_names_target(current_user_message):
        return None
    return (
        "Repair required: the user named a recipient or target, but this delivery "
        "tool call has no explicit `chat_id` or `group`. "
        "Do not default to the current chat. Ask one short clarification question "
        "for the missing target and do not ask any engagement question."
    )


def _last_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[-1].strip() if parts else ""


def _strip_single_code_fence(text: str) -> str:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[0].lstrip().startswith("```"):
        return stripped
    if lines[-1].strip() != "```":
        return stripped

    first_payload = lines[0].lstrip()[3:].strip()
    body = lines[1:-1]
    if first_payload and not re.fullmatch(r"[A-Za-z0-9_+-]+", first_payload):
        body = [first_payload, *body]
    return "\n".join(body).strip()


def _literal_tool_args_from_ast(call: ast.Call) -> dict[str, Any] | None:
    args: dict[str, Any] = {}
    if call.args:
        if len(call.args) != 1 or call.keywords:
            return None
        try:
            only_arg = ast.literal_eval(call.args[0])
        except (ValueError, SyntaxError):
            return None
        if not isinstance(only_arg, dict):
            return None
        return dict(only_arg)

    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        try:
            args[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError):
            return None
    return args


@dataclass
class _TalkativeCooldownState:
    sender_id: str = ""
    topic_tokens: set[str] = field(default_factory=set)
    streak: int = 0
    cooldown_until: float = 0.0


class LLMResponder(ResponderPort):
    """ResponderPort implementation using provider chat-completions + tool loop."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        subagent_model: str | None = None,
        max_iterations: int = 20,
        tavily_api_key: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        contacts_service: "ContactsService | None" = None,
        caldav_service: "CalDAVService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        memory_service: "MemoryService | None" = None,
        telemetry: TelemetryPort | None = None,
        security: SecurityPort | None = None,
        owner_alert_resolver: "Callable[[str], list[str]] | None" = None,
        file_access_resolver: FileAccessResolver | None = None,
        group_resolver: "Callable[[str], tuple[str | None, str | None]] | None" = None,
        model_router: "ModelRouter | None" = None,
        routed_provider_factory: "Callable[[str, str | None], LLMProvider] | None" = None,
        tts: "TTSSynthesizer | None" = None,
        whatsapp_tts_outgoing_dir: Path | None = None,
        whatsapp_tts_max_raw_bytes: int = 160 * 1024,
        recording_notifier: "Callable[[str, str], Awaitable[None]] | None" = None,
        inbound_archive: "InboundArchive | None" = None,
        private_handoff_store: "PrivateHandoffStore | None" = None,
        lazy_media_resolver: "LazyMediaResolver | None" = None,
        whatsapp_session_history_limit: int = 15,
        whatsapp_session_history_limit_group: int = 20,
    ) -> None:
        from yeoman_shared.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.max_iterations = max(1, int(max_iterations))
        self.tavily_api_key = tavily_api_key
        self.web_config = web_config
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.contacts_service = contacts_service
        self.caldav_service = caldav_service
        self.memory = memory_service
        self.telemetry = telemetry
        self.security = security
        self.owner_alert_resolver = owner_alert_resolver
        self.file_access_resolver = file_access_resolver
        self.group_resolver = group_resolver
        self._model_router = model_router
        self._routed_provider_factory = routed_provider_factory
        self._tts = tts
        self._whatsapp_tts_outgoing_dir = whatsapp_tts_outgoing_dir
        self._whatsapp_tts_max_raw_bytes = max(1, int(whatsapp_tts_max_raw_bytes))
        self._recording_notifier = recording_notifier
        self.inbound_archive = inbound_archive
        self._private_handoff_store = private_handoff_store
        self._lazy_media_resolver = lazy_media_resolver
        self._session_history_limit = whatsapp_session_history_limit
        self._session_history_limit_group = whatsapp_session_history_limit_group
        self._talkative_state: dict[str, _TalkativeCooldownState] = {}
        self._pending_hidden_assistant_messages: list[str] = []

        self.effective_restrict_to_workspace = restrict_to_workspace or (
            self.exec_config.isolation.enabled
            and self.exec_config.isolation.force_workspace_restriction
        )

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace, sessions_dir=workspace / "sessions")
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.tools = ToolRegistry()  # type: ignore[no-untyped-call]  # boundary-any
        subagent_model_to_use = subagent_model or self.model
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=subagent_model_to_use,
            tavily_api_key=tavily_api_key,
            web_config=web_config,
            exec_config=self.exec_config,
            restrict_to_workspace=self.effective_restrict_to_workspace,
            file_access_resolver=file_access_resolver,
        )
        self._register_default_tools()

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self.tools.tool_names)

    def _register_default_tools(self) -> None:
        if self.file_access_resolver is not None:
            self.tools.register(ReadFileTool(resolver=self.file_access_resolver))
            self.tools.register(WriteFileTool(resolver=self.file_access_resolver))
            self.tools.register(EditFileTool(resolver=self.file_access_resolver))
            self.tools.register(ListDirTool(resolver=self.file_access_resolver))
        else:
            allowed_dir = self.workspace if self.effective_restrict_to_workspace else None
            self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
            self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
            self.tools.register(EditFileTool(allowed_dir=allowed_dir))
            self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        grant_mounts: list[SandboxMount] = []
        grant_container_prefixes: list[str] = []
        if self.file_access_resolver is not None and self.file_access_resolver.has_grants:
            for (
                host_path,
                container_path,
                readonly,
            ) in self.file_access_resolver.iter_grant_mounts():
                grant_mounts.append(
                    SandboxMount(
                        host_path=host_path,
                        container_path=container_path,
                        readonly=readonly,
                    )
                )
                grant_container_prefixes.append(container_path)

        exec_tool = ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.effective_restrict_to_workspace,
            allow_host_execution=self.exec_config.allow_host_execution,
            isolation_config=self.exec_config.isolation,
            extra_mounts=grant_mounts,
            grant_container_prefixes=grant_container_prefixes,
        )
        self.tools.register(exec_tool)
        self.tools.register(OpsTool())
        self.tools.register(OpsManageTool())

        self.tools.register(WebSearchTool(api_key=self.tavily_api_key, web_config=self.web_config))
        self.tools.register(WebFetchTool(api_key=self.tavily_api_key, web_config=self.web_config))
        self.tools.register(WebMapTool(api_key=self.tavily_api_key, web_config=self.web_config))
        self.tools.register(WebCrawlTool(api_key=self.tavily_api_key, web_config=self.web_config))
        self.tools.register(DeepResearchTool(api_key=self.tavily_api_key, web_config=self.web_config))
        self.tools.register(MarketQuoteTool())
        self.tools.register(MarketIntelligenceTool())
        self.tools.register(YoutubeTranscriptTool())

        from yeoman_gateway.agent.tools.browse import BrowseTool

        self.tools.register(BrowseTool())

        message_tool = MessageTool(
            send_callback=self.bus.publish_outbound,
            group_resolver=self._resolve_group_reference,
        )
        self.tools.register(message_tool)
        self.tools.register(
            SendVoiceTool(
                send_callback=self._send_voice_message,
                group_resolver=self._resolve_group_reference,
            )
        )

        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        from yeoman_gateway.agent.tools.spawn_sync import SpawnSyncTool
        self.tools.register(SpawnSyncTool(manager=self.subagents))

        from yeoman_gateway.agent.tools.fact_check import FactCheckTool
        self.tools.register(FactCheckTool(manager=self.subagents))

        if self.cron_service is not None:
            cron_tool = CronTool(self.cron_service)
            self.tools.register(cron_tool)

        if self.contacts_service is not None:
            contacts_tool = ContactsTool(self.contacts_service)
            self.tools.register(contacts_tool)

        # Calendar — only if CalDAV credentials are configured
        if self.caldav_service is not None:
            from yeoman_gateway.agent.tools.calendar import CalendarTool
            self.tools.register(CalendarTool(self.caldav_service))

        # Summarize history — only if archive is available
        if self.inbound_archive is not None:
            from yeoman_gateway.agent.tools.summarize_history import SummarizeHistoryTool

            self.tools.register(
                SummarizeHistoryTool(
                    self.inbound_archive,
                    self.contacts_service,
                    group_resolver=self._resolve_group_reference,
                )
            )

        # Recall conversation — search session history on demand
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool

        self._recall_tool = RecallConversationTool(session_manager=self.sessions)
        self.tools.register(self._recall_tool)

        if self._lazy_media_resolver is not None:
            from yeoman_gateway.agent.tools.media_history import MediaHistoryTool

            self.tools.register(
                MediaHistoryTool(
                    cache=self._lazy_media_resolver.cache,
                    processor=self._lazy_media_resolver.processor,
                    group_resolver=self._resolve_group_reference,
                )
            )

    def _metric(
        self,
        name: str,
        value: int = 1,
        labels: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.incr(name, value, labels)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("telemetry incr failed {}={}: {}", name, value, exc)

    def _set_tool_context(
        self, *, channel: str, chat_id: str, session_key: str, is_owner: bool = False,
    ) -> None:
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(channel, chat_id)

        send_voice_tool = self.tools.get("send_voice")
        if isinstance(send_voice_tool, SendVoiceTool):
            send_voice_tool.set_context(channel, chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(channel, chat_id)

        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.set_session_context(session_key)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(channel, chat_id)

        contacts_tool = self.tools.get("contacts")
        if isinstance(contacts_tool, ContactsTool):
            contacts_tool.set_context(channel, chat_id)

        ops_manage_tool = self.tools.get("ops_manage")
        if isinstance(ops_manage_tool, OpsManageTool):
            ops_manage_tool.set_context(channel, chat_id)

        from yeoman_gateway.agent.tools.summarize_history import SummarizeHistoryTool

        summarize_tool = self.tools.get("summarize_history")
        if isinstance(summarize_tool, SummarizeHistoryTool):
            summarize_tool.set_context(channel, chat_id, is_owner=is_owner)

        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool

        recall_tool = self.tools.get("recall_conversation")
        if isinstance(recall_tool, RecallConversationTool):
            recall_tool.set_context(channel, chat_id)

        from yeoman_gateway.agent.tools.media_history import MediaHistoryTool

        media_history_tool = self.tools.get("media_history")
        if isinstance(media_history_tool, MediaHistoryTool):
            media_history_tool.set_context(channel, chat_id, is_owner=is_owner)

    def _resolve_history_limit(
        self, chat_id: str, session_history_limit: int | None, content: str = "",
    ) -> int:
        """Resolve session history limit: per-chat policy > heuristic > global config default."""
        if session_history_limit is not None:
            base = int(session_history_limit)
        elif chat_id.endswith("@g.us"):
            base = self._session_history_limit_group
        else:
            base = self._session_history_limit

        # Expand window when message references earlier conversation,
        # but only when no explicit per-chat policy override is set.
        if session_history_limit is None and content and _has_backward_reference(content):
            return min(base * 3, 50)
        return base

    @staticmethod
    def _parse_owner_raw_voice_command(content: str) -> tuple[str, str] | None:
        compact = str(content or "").strip()
        if not compact:
            return None
        lowered = compact.lower()
        if not (lowered.startswith("!voice-send") or lowered.startswith("!voice_send")):
            return None
        try:
            tokens = shlex.split(compact)
        except ValueError:
            return "", ""
        if len(tokens) < 3:
            return "", ""
        target = str(tokens[1] or "").strip()
        text = " ".join(tokens[2:]).strip()
        if not target or not text:
            return "", ""
        return target, text

    async def _maybe_handle_owner_raw_voice_command(
        self,
        *,
        channel: str,
        content: str,
        is_owner: bool,
    ) -> str | None:
        if not is_owner:
            return None
        parsed = self._parse_owner_raw_voice_command(content)
        if parsed is None:
            return None
        target, text = parsed
        if not target or not text:
            return "Usage: !voice-send <here|chat_id|group_alias> <text>"
        if channel != "whatsapp":
            return "Error: !voice-send currently supports only WhatsApp sessions"

        args: dict[str, Any] = {"content": text}
        args["verbatim"] = True
        target_lower = target.lower()
        if target_lower not in {"here", "this", "current"}:
            if "@" in target:
                args["chat_id"] = target
            else:
                args["group"] = target

        result = await self._execute_tool(
            "send_voice",
            args,
            is_owner=True,
        )
        if str(result).startswith("Error:"):
            return result
        return "done"

    @staticmethod
    def _route_for_event(event: InboundEvent) -> tuple[str, str]:
        if event.channel != "system":
            return event.channel, event.chat_id
        if ":" not in event.chat_id:
            return "cli", event.chat_id
        channel, chat_id = event.chat_id.split(":", 1)
        if not channel or not chat_id:
            return "cli", event.chat_id
        return channel, chat_id

    def _resolve_group_reference(self, reference: str) -> tuple[str | None, str | None]:
        resolver = self.group_resolver
        if resolver is None:
            return None, "WhatsApp group resolver is not configured"
        try:
            return resolver(reference)
        except Exception as e:
            return None, f"group resolver failed: {e}"

    def _resolve_tts_profile(self, *, route: str, channel: str) -> object | None:
        if self._model_router is None:
            return None
        task_key = str(route or "").strip() or "tts.speak"
        if task_key.startswith(f"{channel}."):
            return self._model_router.resolve(task_key)
        return self._model_router.resolve(task_key, channel=channel)

    async def _send_voice_message(self, request: VoiceSendRequest) -> str:
        channel = str(request.channel or "").strip()
        chat_id = str(request.chat_id or "").strip()
        content = str(request.content or "").strip()
        if not channel or not chat_id:
            return "Error: Missing target channel/chat for voice sending"
        if channel != "whatsapp":
            return "Error: send_voice currently supports only WhatsApp"
        if not content:
            return "Error: Voice content is empty"
        if self._tts is None or self._whatsapp_tts_outgoing_dir is None:
            return "Error: Voice sending runtime is not configured"

        route = str(request.tts_route or "").strip() or "tts.speak"
        profile = self._resolve_tts_profile(route=route, channel=channel)
        if profile is None:
            return f"Error: TTS route is unresolved: {route}"

        voice = str(request.voice or "").strip() or "alloy"
        if request.verbatim:
            limited = content
        else:
            max_sentences = max(1, int(request.max_sentences or 3))
            max_chars = max(1, int(request.max_chars or 260))
            plain = strip_markdown_for_tts(content)
            limited = truncate_for_voice(plain, max_sentences=max_sentences, max_chars=max_chars)
        if not limited:
            return "Error: Nothing to synthesize after normalization"

        # Switch presence from "composing" (typing dots) to "recording" (mic icon)
        if self._recording_notifier is not None:
            try:
                await self._recording_notifier(channel, chat_id)
            except Exception:
                pass  # best-effort

        try:
            audio, tts_error = await self._tts.synthesize_with_status(
                limited,
                profile=profile,  # type: ignore[arg-type]
                voice=voice,
                format="opus",
            )
        except Exception as e:
            return f"Error: TTS synthesis failed ({e.__class__.__name__})"
        if not audio:
            return f"Error: TTS synthesis failed ({tts_error or 'empty audio'})"
        if len(audio) > self._whatsapp_tts_max_raw_bytes:
            return (
                "Error: Synthesized audio too large "
                f"({len(audio)} bytes > {self._whatsapp_tts_max_raw_bytes})"
            )

        out_dir = self._whatsapp_tts_outgoing_dir / "tts"
        path = write_tts_audio_file(out_dir, audio, ext=".ogg")
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                reply_to=str(request.reply_to or "").strip() or None,
                media=[str(path)],
            )
        )
        return f"Voice message delivered to {channel}:{chat_id}."

    @staticmethod
    def _delivery_success_target(result: str) -> tuple[str, str] | None:
        match = _DELIVERY_SUCCESS_RE.search(str(result or "").strip())
        if not match:
            return None
        return match.group("channel").strip(), match.group("chat_id").strip()

    @staticmethod
    def _sender_id_from_whatsapp_chat_id(chat_id: str) -> str:
        token = str(chat_id or "").strip()
        if "@" in token:
            token = token.split("@", 1)[0]
        return token.split(":", 1)[0].removeprefix("+")

    def _record_hidden_assistant_marker(self, content: str) -> None:
        compact = " ".join(str(content or "").split())
        if compact:
            self._pending_hidden_assistant_messages.append(compact)

    def _flush_hidden_assistant_markers(self, session: Any) -> None:
        if not self._pending_hidden_assistant_messages:
            return
        for marker in self._pending_hidden_assistant_messages:
            session.add_message("assistant", marker, hidden=True, synthetic=True)
        self._pending_hidden_assistant_messages = []

    def _maybe_open_private_handoff(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        current_channel: str,
        current_chat_id: str,
        current_sender_id: str,
        current_is_group: bool,
        current_user_message: str,
        origin_label: str,
    ) -> None:
        if self._private_handoff_store is None:
            return
        if tool_name not in {"message", "send_voice"}:
            return
        if not current_is_group or current_channel != "whatsapp" or not current_chat_id.endswith("@g.us"):
            return
        if not _private_delivery_intent_present(current_user_message):
            return
        target = self._delivery_success_target(result)
        if target is None:
            return
        target_channel, target_chat_id = target
        if target_channel != "whatsapp" or target_chat_id.endswith("@g.us"):
            return
        target_sender = self._sender_id_from_whatsapp_chat_id(target_chat_id) or current_sender_id
        try:
            self._private_handoff_store.open(
                channel=target_channel,
                target_chat_id=target_chat_id,
                target_sender_id=target_sender,
                origin_chat_id=current_chat_id,
                origin_label=origin_label or current_chat_id,
            )
        except Exception as exc:
            logger.warning("private handoff open failed: {}", exc)

    def _private_delivery_target_is_resolvable(
        self,
        *,
        arguments: dict[str, Any],
        current_sender_id: str,
        current_metadata: dict[str, object],
    ) -> bool:
        target = _delivery_arg_chat_id(arguments)
        if not target:
            return False
        target_aliases = _whatsapp_aliases(_normalize_whatsapp_chat_id_for_delivery(target))
        allowed_values = [
            current_sender_id,
            str(current_metadata.get("participant") or ""),
            str(current_metadata.get("reply_to_participant") or ""),
        ]
        mentioned = current_metadata.get("mentioned_jids")
        if isinstance(mentioned, list):
            allowed_values.extend(str(value) for value in mentioned if str(value or "").strip())
        if target_aliases.intersection(_whatsapp_aliases(*allowed_values)):
            return True
        if self.contacts_service is not None:
            for jid in self.contacts_service.known_jids:
                if target_aliases.intersection(_whatsapp_aliases(str(jid))):
                    return True
        return False

    @staticmethod
    def _metadata_for_event(event: InboundEvent) -> dict[str, object]:
        metadata = dict(event.raw_metadata)
        metadata.update(
            {
                "message_id": event.message_id,
                "sender_id": event.sender_id,
                "participant": event.participant,
                "is_group": event.is_group,
                "mentioned_bot": event.mentioned_bot,
                "reply_to_bot": event.reply_to_bot,
                "reply_to_message_id": event.reply_to_message_id,
                "reply_to_participant": event.reply_to_participant,
                "reply_to_text": event.reply_to_text,
            }
        )
        return metadata

    @staticmethod
    def _session_user_metadata(sender_id: str | None, metadata: dict[str, object]) -> dict[str, object]:
        values: dict[str, object] = {
            "sender_id": sender_id or metadata.get("sender_id"),
            "sender_name": metadata.get("sender_name"),
            "message_id": metadata.get("message_id"),
            "timestamp": metadata.get("timestamp"),
            "reply_to_message_id": metadata.get("reply_to_message_id"),
            "reply_to_participant": metadata.get("reply_to_participant"),
            "reply_to_text": metadata.get("reply_to_text"),
        }
        return {
            key: value
            for key, value in values.items()
            if value is not None and str(value).strip()
        }

    @staticmethod
    def _is_inbound_voice(event: InboundEvent) -> bool:
        return bool(event.raw_metadata.get("is_voice", False)) or (
            str(event.raw_metadata.get("media_kind") or "").strip().lower() == "audio"
        )

    @classmethod
    def _voice_reply_expected(
        cls,
        *,
        event: InboundEvent,
        decision: PolicyDecision,
        outbound_channel: str,
    ) -> bool:
        if outbound_channel != "whatsapp":
            return False
        mode = str(getattr(decision, "voice_output_mode", "text") or "text").strip().lower()
        if mode in {"", "off", "text"}:
            return False
        if mode == "always":
            return True
        if mode == "in_kind":
            return cls._is_inbound_voice(event)
        return False

    def _tool_definitions(self, allowed_tools: set[str]) -> list[dict[str, Any]]:
        return [
            schema
            for schema in self.tools.get_definitions()
            if schema.get("function", {}).get("name") in allowed_tools
        ]

    def _profile_for_name(self, profile_name: str | None) -> object | None:
        """Return the resolved model profile object, or None if unresolvable.

        Profile names in policy.json are camelCase (e.g. "grokFast") but config
        loader converts dict keys to snake_case (e.g. "grok_fast"), so we normalize.
        """
        if not profile_name or self._model_router is None:
            return None
        from yeoman_shared.config.loader import camel_to_snake
        snake_name = camel_to_snake(profile_name)
        try:
            return self._model_router.resolve_by_profile(snake_name)
        except KeyError:
            return None

    def _model_for_profile(self, profile_name: str | None) -> str | None:
        """Return the model string for a named profile, or None if unresolvable."""
        profile = self._profile_for_name(profile_name)
        return str(getattr(profile, "model", "") or "").strip() or None

    def _reasoning_for_profile(self, profile_name: str | None) -> dict[str, object] | None:
        """Return the reasoning config for a named profile, or None."""
        profile = self._profile_for_name(profile_name)
        reasoning = getattr(profile, "reasoning", None)
        return reasoning if isinstance(reasoning, dict) else None

    def _provider_for_profile(self, profile: object | None) -> LLMProvider | None:
        if self._routed_provider_factory is None or profile is None:
            return None
        model = str(getattr(profile, "model", "") or "").strip()
        if not model:
            return None
        provider = str(getattr(profile, "provider", "") or "").strip() or None
        return self._routed_provider_factory(model, provider)

    def _should_enable_grants(self, is_owner: bool) -> bool:
        """Check whether grants should be activated for tool execution."""
        if self.file_access_resolver is None or not self.file_access_resolver.has_grants:
            return False
        if self.file_access_resolver.owner_only:
            return is_owner
        return True

    async def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        is_owner: bool,
    ) -> str | None:
        """Execute a tool call, activating grant context when appropriate."""
        if self._should_enable_grants(is_owner):
            with enable_grants():
                return await self.tools.execute(name, arguments)
        return await self.tools.execute(name, arguments)

    @staticmethod
    def _parse_textual_tool_call(
        content: str | None,
        *,
        allowed_tools: set[str],
    ) -> ToolCallRequest | None:
        if not content:
            return None
        text = _strip_single_code_fence(content)
        if not text:
            return None

        try:
            parsed_json = json.loads(text)
        except json.JSONDecodeError:
            parsed_json = None

        if isinstance(parsed_json, dict):
            name = parsed_json.get("tool") or parsed_json.get("name")
            arguments = (
                parsed_json.get("arguments")
                or parsed_json.get("args")
                or parsed_json.get("parameters")
                or {}
            )
            function = parsed_json.get("function")
            if isinstance(function, dict):
                name = name or function.get("name")
                arguments = function.get("arguments", arguments)
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return None
            if isinstance(name, str) and isinstance(arguments, dict):
                tool_name = name.strip()
                if tool_name in allowed_tools:
                    return ToolCallRequest(
                        id=f"textual_tool_{int(time.time() * 1000)}",
                        name=tool_name,
                        arguments=dict(arguments),
                    )
            return None

        try:
            parsed_expr = ast.parse(text, mode="eval")
        except SyntaxError:
            return None
        call = parsed_expr.body
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            return None
        tool_name = call.func.id
        if tool_name not in allowed_tools:
            return None
        arguments = _literal_tool_args_from_ast(call)
        if arguments is None:
            return None
        return ToolCallRequest(
            id=f"textual_tool_{int(time.time() * 1000)}",
            name=tool_name,
            arguments=arguments,
        )

    async def _chat_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        allowed_tools: set[str],
        security_context: dict[str, object] | None = None,
        is_owner: bool = False,
        model: str | None = None,
        provider: LLMProvider | None = None,
        temperature: float | None = None,
        reasoning: dict[str, object] | None = None,
        current_user_message: str = "",
        current_channel: str = "",
        current_chat_id: str = "",
        current_sender_id: str = "",
        current_is_group: bool = False,
        current_origin_label: str = "",
        current_metadata: dict[str, object] | None = None,
        trace: Any = None,
    ) -> str | None:
        iteration = 0
        final_content: str | None = None
        chat_provider = provider or self.provider
        deferred_work_repair_attempted = False
        # Guard against the model looping on the same side-effecting tool call
        _sent_calls: set[tuple[str, str]] = set()
        _send_tools = frozenset({"message", "send_voice", "send_media"})

        while iteration < self.max_iterations:
            iteration += 1
            iter_span = lf.start_span(
                trace=trace,
                name=f"iteration-{iteration}",
            ) if trace is not None else None
            try:
                response = await chat_provider.chat(
                    messages=messages,
                    tools=self._tool_definitions(allowed_tools),
                    model=model or self.model,
                    temperature=temperature if temperature is not None else 0.7,
                    reasoning=reasoning,
                )
                lf.log_generation(
                    parent=iter_span or trace,
                    name="llm",
                    model=model or self.model,
                    input={"message_count": len(messages), "has_tools": bool(self._tool_definitions(allowed_tools))},
                    output=response.content,
                    usage={
                        "input": response.usage.get("prompt_tokens", 0),
                        "output": response.usage.get("completion_tokens", 0),
                        "total": response.usage.get("total_tokens", 0),
                    },
                )

                tool_calls = response.tool_calls
                assistant_content = response.content
                if not tool_calls:
                    textual_tool_call = self._parse_textual_tool_call(
                        response.content,
                        allowed_tools=allowed_tools,
                    )
                    if textual_tool_call is not None:
                        if textual_tool_call.name in _TEXTUAL_TOOL_COERCION_SAFE_TOOLS:
                            logger.warning(
                                "Coercing textual tool call into runtime tool call: {}",
                                textual_tool_call.name,
                            )
                            tool_calls = [textual_tool_call]
                            assistant_content = None
                        else:
                            logger.warning(
                                "Rejected textual side-effecting tool call: {}",
                                textual_tool_call.name,
                            )
                            messages.append(
                                {
                                    "role": "system",
                                    "content": (
                                        "Runtime rejected your previous output because it was a "
                                        "tool call printed as user-visible text. Do not send JSON, "
                                        "code fences, or function-call syntax to the chat. Use a "
                                        "proper tool call if needed, otherwise answer in normal prose."
                                    ),
                                }
                            )
                            continue

                if (
                    not tool_calls
                    and allowed_tools
                    and _looks_like_deferred_work_promise(response.content)
                ):
                    if deferred_work_repair_attempted:
                        logger.warning(
                            "Suppressing repeated deferred work promise before outbound: {!r}",
                            response.content,
                        )
                        return None
                    deferred_work_repair_attempted = True
                    logger.warning(
                        "Rejecting deferred work promise before outbound: {!r}",
                        response.content,
                    )
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Runtime rejected your previous response because it only "
                                "promised future work. Either call an available tool now, "
                                "or answer directly if no tool is needed. Do not send a "
                                "waiting or preamble message to the chat."
                            ),
                        }
                    )
                    continue

                if tool_calls:
                    tool_call_dicts: list[dict[str, Any]] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls
                    ]
                    messages = self.context.add_assistant_message(
                        messages,
                        assistant_content,
                        tool_call_dicts,
                        reasoning_content=response.reasoning_content,
                    )

                    for tool_call in tool_calls:
                        args_preview = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.info("Tool call: {}({})", tool_call.name, args_preview[:200])
                        tool_span = lf.start_span(
                            trace=trace,
                            name=f"tool/{tool_call.name}",
                            metadata={"arguments": args_preview[:500]},
                            parent_span_id=iter_span.span_id if iter_span else None,
                        ) if trace is not None else None

                        # --- repair/dedup guards for send-type tools ---
                        if tool_call.name in _send_tools:
                            metadata_for_delivery = current_metadata or {}
                            if (
                                _delivery_targets_private_chat(
                                    arguments=tool_call.arguments,
                                    current_channel=current_channel,
                                    current_chat_id=current_chat_id,
                                )
                                and _private_delivery_intent_present(current_user_message)
                                and not self._private_delivery_target_is_resolvable(
                                    arguments=tool_call.arguments,
                                    current_sender_id=current_sender_id,
                                    current_metadata=metadata_for_delivery,
                                )
                            ):
                                repair_result = (
                                    "Repair required: the user asked for private delivery, "
                                    "but the personal target is not the current sender, a "
                                    "mentioned/replied participant, or a known contact. Ask "
                                    "one short clarification question and do not guess a phone number."
                                )
                                logger.warning(
                                    "Blocked delivery tool call with unresolved private target: {}",
                                    tool_call.name,
                                )
                                lf.end_span(tool_span, output=repair_result)
                                messages = self.context.add_tool_result(
                                    messages,
                                    tool_call.id,
                                    tool_call.name,
                                    repair_result,
                                )
                                continue
                            repair_result = _delivery_repair_result(
                                tool_name=tool_call.name,
                                arguments=tool_call.arguments,
                                current_user_message=current_user_message,
                                current_channel=current_channel,
                                current_chat_id=current_chat_id,
                            )
                            if repair_result is not None and tool_call.name in allowed_tools:
                                logger.warning(
                                    "Blocked delivery tool call missing explicit target: {}",
                                    tool_call.name,
                                )
                                lf.end_span(tool_span, output=repair_result)
                                messages = self.context.add_tool_result(
                                    messages,
                                    tool_call.id,
                                    tool_call.name,
                                    repair_result,
                                )
                                continue

                            call_key = (tool_call.name, args_preview)
                            if call_key in _sent_calls:
                                result = (
                                    f"Blocked: you already called {tool_call.name} "
                                    "with these exact arguments earlier in this turn. "
                                    "The message was already delivered. "
                                    "Tell the user it was already sent."
                                )
                                logger.warning("Blocked duplicate tool call: {}", tool_call.name)
                                lf.end_span(tool_span, output=result)
                                messages = self.context.add_tool_result(
                                    messages, tool_call.id, tool_call.name, result,
                                )
                                continue
                            _sent_calls.add(call_key)

                        if tool_call.name not in allowed_tools:
                            result = (
                                f"Error: Tool '{tool_call.name}' is blocked by policy for this chat."
                            )
                            lf.end_span(tool_span, output=result)
                        else:
                            if self.security is not None:
                                tool_security = self.security.check_tool(
                                    tool_call.name,
                                    tool_call.arguments,
                                    context=security_context,
                                )
                                if tool_security.decision.action == "block":
                                    self._metric(
                                        "security_tool_blocked",
                                        labels=(("tool", tool_call.name),),
                                    )
                                    result = (
                                        "Error: Tool call blocked by security middleware "
                                        f"({tool_security.decision.reason})."
                                    )
                                    lf.end_span(tool_span, output=result)
                                else:
                                    if tool_security.decision.action == "warn":
                                        self._metric(
                                            "security_tool_warn",
                                            labels=(("tool", tool_call.name),),
                                        )
                                    result = await self._execute_tool(
                                        tool_call.name,
                                        tool_call.arguments,
                                        is_owner=is_owner,
                                    )
                                    lf.end_span(tool_span, output=result[:500] if result else "")
                            else:
                                result = await self._execute_tool(
                                    tool_call.name,
                                    tool_call.arguments,
                                    is_owner=is_owner,
                                )
                                lf.end_span(tool_span, output=result[:500] if result else "")
                        if hasattr(self, '_current_session') and self._current_session is not None:
                            self._current_session.add_tool_call(
                                tool_name=tool_call.name,
                                tool_call_id=tool_call.id,
                                arguments=tool_call.arguments,
                                result=result,
                            )
                        messages = self.context.add_tool_result(
                            messages,
                            tool_call.id,
                            tool_call.name,
                            result,
                        )
                        self._maybe_open_private_handoff(
                            tool_name=tool_call.name,
                            arguments=tool_call.arguments,
                            result=str(result or ""),
                            current_channel=current_channel,
                            current_chat_id=current_chat_id,
                            current_sender_id=current_sender_id,
                            current_is_group=current_is_group,
                            current_user_message=current_user_message,
                            origin_label=current_origin_label,
                        )
                        if (
                            tool_call.name == "send_voice"
                            and result.startswith("Voice message delivered")
                        ):
                            target = self._delivery_success_target(str(result or ""))
                            target_label = (
                                f"{target[0]}:{target[1]}" if target is not None else "the requested chat"
                            )
                            self._record_hidden_assistant_marker(
                                "Voice message delivered to "
                                f"{target_label}. Do not send it again for this request."
                            )
                            return None
                    continue

                final_content = response.content
                break
            finally:
                lf.end_span(iter_span)
        else:
            return "⚙️❓"  # max iterations reached without a text response

        return final_content or "🤔❓"

    async def _handle_approve_command(self, channel: str, sender_id: str, content: str) -> str | None:
        """Handle owner approve/deny commands for new groups.

        Commands:
        - /approve <chat_id> - Allow group + reply to all
        - /deny <chat_id> - Block group
        - yes <chat_id> - Shortcut for approve
        - approve <chat_id> - Shortcut for approve
        """
        # Check if sender is owner
        if self.owner_alert_resolver is None:
            return None
        owners = self.owner_alert_resolver(channel)
        if sender_id not in owners:
            return None

        content_lower = content.lower().strip()

        # Parse command
        chat_id = None
        command_type = None

        # /approve <chat_id>
        if content_lower.startswith("/approve "):
            chat_id = content[8:].strip()
            command_type = "approve"
        # /deny <chat_id>
        elif content_lower.startswith("/deny "):
            chat_id = content[5:].strip()
            command_type = "deny"
        # just "yes" or "approve" - need to find pending group from context
        elif content_lower in ("yes", "approve", "approved"):
            # Could track pending approvals, for now just return help
            return "Please specify the group ID: /approve <chat_id@g.us>"
        # "yes <chat_id>" or "approve <chat_id>"
        elif content_lower.startswith("yes ") or content_lower.startswith("approve "):
            parts = content.split(None, 1)
            if len(parts) == 2:
                chat_id = parts[1].strip()
                command_type = "approve"
        elif content_lower.startswith("deny "):
            parts = content.split(None, 1)
            if len(parts) == 2:
                chat_id = parts[1].strip()
                command_type = "deny"

        if not chat_id or not command_type:
            return None

        # Validate chat_id format
        if not chat_id.endswith("@g.us") and not chat_id.endswith("@s.whatsapp.net"):
            return "Invalid chat ID format. Use: /approve <chat_id@g.us>"

        # Execute the command via policy admin (if available) or return instructions
        if command_type == "approve":
            return (
                f"✅ Approving group {chat_id}\n"
                f"Run these commands:\n"
                f"  /policy allow-group {chat_id}\n"
                f"  /policy set-when {chat_id} all"
            )
        else:  # deny
            return (
                f"🚫 Blocking group {chat_id}\n"
                f"Run:\n"
                f"  /policy block-group {chat_id}"
            )

    @staticmethod
    def _topic_tokens(text: str) -> set[str]:
        compact = re.sub(r"https?://\S+", " ", text.lower())
        compact = re.sub(r"[^a-z0-9_\s]+", " ", compact)
        tokens = {t for t in compact.split() if len(t) >= 4 and not t.isdigit()}
        if tokens:
            return set(list(tokens)[:40])
        fallback = {t for t in compact.split() if len(t) >= 2 and not t.isdigit()}
        return set(list(fallback)[:24])

    @staticmethod
    def _topic_overlap(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        union = left | right
        if not union:
            return 0.0
        return len(left & right) / len(union)

    @staticmethod
    def _looks_like_new_value_request(text: str) -> bool:
        compact = " ".join(str(text or "").strip().split())
        if not compact:
            return False
        if _NEW_VALUE_REQUEST_RE.search(compact):
            return True
        if (
            _RETRY_REFINEMENT_REQUEST_RE.search(compact)
            and _VOICE_REFINEMENT_CONTEXT_RE.search(compact)
        ):
            return True
        if compact.endswith("?") and _CLARIFYING_QUESTION_START_RE.search(compact):
            return True
        return False

    @staticmethod
    def _looks_like_social_reply(text: str) -> bool:
        compact = " ".join(str(text or "").strip().split())
        if not compact:
            return False
        if compact.startswith("::reaction::"):
            return True
        if compact.endswith("?") and _CLARIFYING_QUESTION_START_RE.search(compact):
            return False
        if _BANTER_MARKER_RE.search(compact):
            return True
        if len(compact) > 180:
            return False
        if re.search(r"https?://|\b\d+(?:[.,]\d+)?\s*(?:%|usd|eur|mrd|billion|million)\b", compact, re.IGNORECASE):
            return False
        return not _NEW_VALUE_REQUEST_RE.search(compact)

    @staticmethod
    def _should_hold_back_after_social_reply(
        *,
        session_messages: list[dict[str, Any]],
        content: str,
        metadata: dict[str, object],
        is_owner: bool = False,
        sender_id: str | None = None,
    ) -> bool:
        if is_owner:
            return False
        if not bool(metadata.get("is_group", False)):
            return False
        if not (bool(metadata.get("reply_to_bot", False)) or bool(metadata.get("mentioned_bot", False))):
            return False
        if LLMResponder._looks_like_new_value_request(content):
            return False

        last_assistant_idx: int | None = None
        for index in range(len(session_messages) - 1, -1, -1):
            if str(session_messages[index].get("role") or "") == "assistant":
                last_assistant_idx = index
                break
        if last_assistant_idx is None:
            return False

        last_assistant = str(session_messages[last_assistant_idx].get("content") or "").strip()
        if not LLMResponder._looks_like_social_reply(last_assistant):
            return False

        last_assistant_ts = LLMResponder._parse_session_timestamp(
            session_messages[last_assistant_idx].get("timestamp")
        )
        if last_assistant_ts is not None:
            now = datetime.now(last_assistant_ts.tzinfo) if last_assistant_ts.tzinfo else datetime.now()
            if (now - last_assistant_ts) > timedelta(minutes=10):
                return False

        prior_user_sender: str | None = None
        for index in range(last_assistant_idx - 1, -1, -1):
            row = session_messages[index]
            if str(row.get("role") or "") != "user":
                continue
            sender = row.get("sender_id")
            prior_user_sender = str(sender).strip() if sender else None
            break
        current_sender = (sender_id or "").strip()
        if not (prior_user_sender and current_sender and prior_user_sender == current_sender):
            return False

        return True

    @staticmethod
    def _parse_session_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _normalize_social_question_ending(text: str, metadata: dict[str, object]) -> str:
        compact = str(text or "").rstrip()
        if not compact or not bool(metadata.get("is_group", False)):
            return compact
        if not compact.endswith("?"):
            return compact
        sentence = _last_sentence(compact)
        if _CLARIFYING_QUESTION_START_RE.search(sentence):
            return compact
        return compact[:-1].rstrip() + "."

    @staticmethod
    def _is_probably_german(text: str) -> bool:
        lowered = f" {text.lower()} "
        de_markers = (
            " und ",
            " der ",
            " die ",
            " das ",
            " ist ",
            " nicht ",
            " was ",
            " wie ",
            " heute ",
            " kann ",
            " kannst ",
            " bitte ",
            " danke ",
        )
        en_markers = (
            " the ",
            " and ",
            " is ",
            " not ",
            " what ",
            " how ",
            " today ",
            " can ",
            " please ",
            " thanks ",
        )
        de_score = sum(1 for marker in de_markers if marker in lowered)
        en_score = sum(1 for marker in en_markers if marker in lowered)
        return de_score >= en_score

    def _talkative_message_for(self, text: str) -> str:
        if self._is_probably_german(text):
            return "Bro, du nervst gerade mit dem gleichen Thema. Kurz Pause."
        return (
            "Bro, this is getting annoying on the same topic. Take a short pause."
        )

    async def _generate_talkative_message_llm(self, text: str) -> str | None:
        language_hint = "German" if self._is_probably_german(text) else "English"
        prompt = [
            {
                "role": "system",
                "content": (
                    "You write one short playful cooldown message for a busy group chat. "
                    "No markdown. No threats. No slurs. No factual claims. "
                    "Set a blunt boundary without customer-support softness. "
                    "Do not ask a follow-up question. "
                    "Max 2 sentences and max 160 characters."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Write a cheeky message telling one very talkative person they are getting annoying "
                    "with the same topic and should take a short pause. "
                    f"Output language: {language_hint}."
                ),
            },
        ]
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    messages=prompt,
                    tools=[],
                    model=self.model,
                    max_tokens=80,
                    temperature=0.9,
                ),
                timeout=6.0,
            )
        except Exception as exc:
            logger.debug("talkative llm message generation failed: {}", exc)
            return None

        if response.has_tool_calls:
            return None
        content = (response.content or "").strip()
        if not content:
            return None
        if len(content) > 220:
            content = content[:220].rstrip() + "..."
        return content

    async def _maybe_talkative_cooldown_reply(
        self,
        *,
        session_key: str,
        sender_id: str | None,
        content: str,
        metadata: dict[str, object],
        enabled: bool,
        streak_threshold: int,
        topic_overlap_threshold: float,
        cooldown_seconds: int,
        delay_seconds: float,
        use_llm_message: bool,
    ) -> str | None:
        if not enabled:
            return None
        if not bool(metadata.get("is_group", False)):
            return None
        actor = (sender_id or "").strip()
        if not actor:
            return None

        tokens = self._topic_tokens(content)
        if not tokens:
            return None

        state = self._talkative_state.get(session_key, _TalkativeCooldownState())
        same_sender = actor == state.sender_id
        same_topic = (
            same_sender
            and bool(state.topic_tokens)
            and self._topic_overlap(tokens, state.topic_tokens) >= float(topic_overlap_threshold)
        )

        if same_sender and same_topic:
            state.streak += 1
            state.topic_tokens = set(list(state.topic_tokens | tokens)[:40])
        else:
            state.sender_id = actor
            state.topic_tokens = tokens
            state.streak = 1

        now = time.monotonic()
        if state.cooldown_until > now:
            self._talkative_state[session_key] = state
            return None

        if state.streak < int(streak_threshold):
            self._talkative_state[session_key] = state
            return None

        state.cooldown_until = now + float(cooldown_seconds)
        state.streak = 0
        self._talkative_state[session_key] = state

        if delay_seconds > 0:
            await asyncio.sleep(float(delay_seconds))
        if use_llm_message:
            llm_message = await self._generate_talkative_message_llm(content)
            if llm_message:
                return llm_message
        return self._talkative_message_for(content)

    async def _generate(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        content: str,
        sender_id: str | None,
        media: tuple[str, ...],
        metadata: dict[str, object],
        allowed_tools: set[str],
        persona_text: str | None,
        talkative_cooldown_enabled: bool = False,
        talkative_cooldown_streak_threshold: int = 7,
        talkative_cooldown_topic_overlap_threshold: float = 0.34,
        talkative_cooldown_cooldown_seconds: int = 900,
        talkative_cooldown_delay_seconds: float = 2.5,
        talkative_cooldown_use_llm_message: bool = False,
        is_owner: bool = False,
        model_profile: str | None = None,
        session_history_limit: int | None = None,
        private_handoff_id: str | None = None,
    ) -> str | None:
        # Serialize concurrent calls for the same session to prevent session
        # state corruption (lost messages, overwritten saves).
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await self._generate_locked(
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                content=content,
                sender_id=sender_id,
                media=media,
                metadata=metadata,
                allowed_tools=allowed_tools,
                persona_text=persona_text,
                talkative_cooldown_enabled=talkative_cooldown_enabled,
                talkative_cooldown_streak_threshold=talkative_cooldown_streak_threshold,
                talkative_cooldown_topic_overlap_threshold=talkative_cooldown_topic_overlap_threshold,
                talkative_cooldown_cooldown_seconds=talkative_cooldown_cooldown_seconds,
                talkative_cooldown_delay_seconds=talkative_cooldown_delay_seconds,
                talkative_cooldown_use_llm_message=talkative_cooldown_use_llm_message,
                is_owner=is_owner,
                model_profile=model_profile,
                session_history_limit=session_history_limit,
                private_handoff_id=private_handoff_id,
            )

    async def _generate_locked(
        self,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        content: str,
        sender_id: str | None,
        media: tuple[str, ...],
        metadata: dict[str, object],
        allowed_tools: set[str],
        persona_text: str | None,
        talkative_cooldown_enabled: bool = False,
        talkative_cooldown_streak_threshold: int = 7,
        talkative_cooldown_topic_overlap_threshold: float = 0.34,
        talkative_cooldown_cooldown_seconds: int = 900,
        talkative_cooldown_delay_seconds: float = 2.5,
        talkative_cooldown_use_llm_message: bool = False,
        is_owner: bool = False,
        model_profile: str | None = None,
        session_history_limit: int | None = None,
        private_handoff_id: str | None = None,
    ) -> str | None:
        # Handle owner approve/deny commands
        if is_owner and channel == "whatsapp":
            approval_response = await self._handle_approve_command(channel, sender_id or "", content)
            if approval_response:
                return approval_response

        trace = lf.start_trace(
            name="generate",
            metadata={
                "channel": channel,
                "chat_id": chat_id,
                "session_key": session_key,
                "model": self._model_for_profile(model_profile) or self.model,
            },
            tags=[channel],
            session_id=session_key,
        )
        self._current_trace = trace
        self._pending_hidden_assistant_messages = []
        metadata = dict(metadata)

        session = self.sessions.get_or_create(session_key)

        # Save session immediately on first message (even if no response yet)
        if not session.messages:
            session.add_message("user", content, **self._session_user_metadata(sender_id, metadata))
            self.sessions.save(session)
            # Track that we've already added the user message to avoid duplication
            _user_message_already_added = True
        else:
            _user_message_already_added = False

        self._set_tool_context(
            channel=channel, chat_id=chat_id, session_key=session_key, is_owner=is_owner,
        )

        if self.memory is not None:
            try:
                self.memory.pre_write_session_state(
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    user_message=content,
                    metadata=metadata,
                )
            except Exception as e:
                logger.warning("memory wal pre-write failed: {}", e)

        if self._should_hold_back_after_social_reply(
            session_messages=session.messages,
            content=content,
            metadata=metadata,
            is_owner=is_owner,
            sender_id=sender_id,
        ):
            if not _user_message_already_added:
                session.add_message("user", content, **self._session_user_metadata(sender_id, metadata))
            self.sessions.save(session)
            self._metric("social_holdback")
            logger.info(
                "social_holdback fired channel={} chat={} sender={} content_preview={!r}",
                channel,
                chat_id,
                sender_id,
                content[:80],
            )
            self._current_trace = None
            return None

        owner_raw_voice_reply = await self._maybe_handle_owner_raw_voice_command(
            channel=channel,
            content=content,
            is_owner=is_owner,
        )
        if owner_raw_voice_reply is not None:
            final_content = owner_raw_voice_reply
        else:
            if self._lazy_media_resolver is not None:
                try:
                    retrieval = await self._lazy_media_resolver.resolve(
                        channel=channel,
                        chat_id=chat_id,
                        content=content,
                        metadata=metadata,
                    )
                    if retrieval is not None:
                        metadata["temporary_media_retrieval"] = retrieval
                        self._metric(
                            "temporary_media_retrieval_chars",
                            len(str(retrieval.get("content") or "")),
                        )
                except Exception as e:
                    logger.warning("lazy media retrieval failed: {}", e)

            retrieved_memory_text = ""
            retrieved_hits_count = 0
            if self.memory is not None:
                try:
                    # Augment the memory query with recent ambient messages so that vague
                    # inputs like "what do you think?" can surface relevant memories.
                    memory_query = content
                    ambient_raw = metadata.get("ambient_context_window") if metadata else None
                    if isinstance(ambient_raw, list) and ambient_raw:
                        ambient_snippet = " ".join(
                            (line.split("] ", 1)[-1] if "] " in line else line)
                            for line in ambient_raw[:5]
                            if isinstance(line, str)
                        ).strip()
                        if ambient_snippet:
                            memory_query = f"{ambient_snippet} {content}".strip()
                    retrieved_memory_text, retrieved_hits = self.memory.build_retrieved_context(
                        channel=channel,
                        chat_id=chat_id,
                        sender_id=sender_id,
                        query=memory_query,
                        reply_to_text=str(metadata.get("reply_to_text") or "").strip() or None,
                        reply_to_jid=str(metadata.get("reply_to_participant") or "").strip() or None,
                        owner_context=is_owner,
                    )
                    retrieved_hits_count = len(retrieved_hits)
                except Exception as e:
                    logger.warning("memory recall failed: {}", e)

                if retrieved_hits_count > 0:
                    self._metric("memory_recall_hit")
                else:
                    self._metric("memory_recall_miss")
                if retrieved_memory_text:
                    self._metric("memory_prompt_chars", len(retrieved_memory_text))

            talkative_reply = await self._maybe_talkative_cooldown_reply(
                session_key=session_key,
                sender_id=sender_id,
                content=content,
                metadata=metadata,
                enabled=talkative_cooldown_enabled,
                streak_threshold=talkative_cooldown_streak_threshold,
                topic_overlap_threshold=talkative_cooldown_topic_overlap_threshold,
                cooldown_seconds=talkative_cooldown_cooldown_seconds,
                delay_seconds=talkative_cooldown_delay_seconds,
                use_llm_message=talkative_cooldown_use_llm_message,
            )
            if talkative_reply is not None:
                final_content = talkative_reply
            else:
                # Append contacts roster if present
                roster_text = metadata.pop("_contacts_roster_text", None)
                if roster_text:
                    if retrieved_memory_text:
                        retrieved_memory_text = f"{retrieved_memory_text}\n\n{roster_text}"
                    else:
                        retrieved_memory_text = str(roster_text)

                messages = self.context.build_messages(
                    history=session.get_history(
                        max_messages=self._resolve_history_limit(chat_id, session_history_limit, content),
                    ),
                    current_message=content,
                    current_metadata=metadata,
                    retrieved_memory_text=retrieved_memory_text,
                    persona_text=persona_text,
                    media=list(media),
                    channel=channel,
                    chat_id=chat_id,
                )

                self._current_session = session
                resolved_profile = self._profile_for_name(model_profile)
                final_content = await self._chat_loop(
                    messages=messages,
                    allowed_tools=allowed_tools,
                    security_context={
                        "channel": channel,
                        "chat_id": chat_id,
                        "sender_id": sender_id or "",
                        "session_key": session_key,
                    },
                    is_owner=is_owner,
                    model=str(getattr(resolved_profile, "model", "") or "").strip() or None,
                    provider=self._provider_for_profile(resolved_profile),
                    temperature=(
                        float(getattr(resolved_profile, "temperature"))
                        if getattr(resolved_profile, "temperature", None) is not None
                        else None
                    ),
                    reasoning=(
                        getattr(resolved_profile, "reasoning", None)
                        if isinstance(getattr(resolved_profile, "reasoning", None), dict)
                        else None
                    ),
                    current_user_message=content,
                    current_channel=channel,
                    current_chat_id=chat_id,
                    current_sender_id=sender_id or "",
                    current_is_group=bool(metadata.get("is_group", False)),
                    current_origin_label=str(
                        metadata.get("group_name")
                        or metadata.get("subject")
                        or metadata.get("chat_name")
                        or chat_id
                    ),
                    current_metadata=metadata,
                    trace=trace,
                )
                self._current_session = None

        if final_content is None:
            if not _user_message_already_added:
                session.add_message("user", content, **self._session_user_metadata(sender_id, metadata))
            self._flush_hidden_assistant_markers(session)
            self.sessions.save(session)
            self._current_trace = None
            return None

        final_content = self._normalize_social_question_ending(final_content, metadata)

        if self.memory is not None:
            try:
                capture_result = self.memory.capture_from_turn(
                    channel=channel,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    user_message=content,
                    source_message_id=str(metadata.get("message_id") or "").strip() or None,
                    assistant_reply=final_content,
                )
                logger.info(
                    "memory capture: saved={} deduped={} dropped_low_conf={} dropped_safety={}",
                    len(capture_result.saved),
                    capture_result.deduped,
                    capture_result.dropped_low_confidence,
                    capture_result.dropped_safety,
                )
                if capture_result.saved:
                    self._metric("memory_capture_saved", len(capture_result.saved))
                if capture_result.dropped_low_confidence:
                    self._metric(
                        "memory_capture_dropped_low_conf",
                        capture_result.dropped_low_confidence,
                    )
                if capture_result.dropped_safety:
                    self._metric("memory_capture_dropped_safety", capture_result.dropped_safety)
                if capture_result.deduped:
                    self._metric("memory_capture_deduped", capture_result.deduped)
            except Exception as e:
                logger.warning("memory capture failed: {}", e)

            try:
                self.memory.post_write_session_state(
                    session_key=session_key,
                    assistant_reply=final_content,
                    pending_actions=[],
                )
            except Exception as e:
                logger.warning("memory wal post-write failed: {}", e)

        # Only add messages if they weren't already added (for new sessions)
        if not _user_message_already_added:
            session.add_message("user", content, **self._session_user_metadata(sender_id, metadata))
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        if private_handoff_id and self._private_handoff_store is not None:
            try:
                self._private_handoff_store.consume_reply(private_handoff_id)
            except Exception as exc:
                logger.warning("private handoff consume failed: {}", exc)
        self._current_trace = None
        return final_content

    @override
    async def generate_reply(self, event: InboundEvent, decision: PolicyDecision) -> str | None:
        route_channel, route_chat_id = self._route_for_event(event)
        session_key = f"{route_channel}:{route_chat_id}"
        metadata = self._metadata_for_event(event)
        if self._voice_reply_expected(
            event=event,
            decision=decision,
            outbound_channel=route_channel,
        ):
            metadata["voice_reply_expected"] = True
            metadata["voice_reply_max_sentences"] = int(
                getattr(decision, "voice_output_max_sentences", 3) or 3
            )
            metadata["voice_reply_max_chars"] = int(
                getattr(decision, "voice_output_max_chars", 500) or 500
            )
        if decision.private_handoff_active:
            metadata["private_handoff_active"] = True
            metadata["private_handoff_origin_chat_id"] = (
                decision.private_handoff_origin_chat_id or ""
            )
            metadata["private_handoff_origin_label"] = (
                decision.private_handoff_origin_label
                or decision.private_handoff_origin_chat_id
                or ""
            )
            metadata["private_handoff_remaining_replies"] = (
                decision.private_handoff_remaining_replies
            )
        # Inject contacts roster for group chats with disclosure enabled
        if (
            decision.contacts_disclosure
            and event.is_group
            and self.contacts is not None
        ):
            mentioned_jids = event.raw_metadata.get("mentioned_jids", [])
            jids: list[str] = []
            if isinstance(mentioned_jids, list):
                jids.extend(str(j) for j in mentioned_jids if isinstance(j, str))
            for jid in self.contacts.known_jids:
                if jid not in jids:
                    jids.append(jid)
            roster_text = self.contacts.format_roster_text(
                channel=route_channel, participant_jids=jids,
            )
            if roster_text:
                metadata["_contacts_roster_text"] = roster_text
        return await self._generate(
            session_key=session_key,
            channel=route_channel,
            chat_id=route_chat_id,
            content=event.content,
            sender_id=event.sender_id,
            media=event.media,
            metadata=metadata,
            allowed_tools=set(decision.allowed_tools),
            persona_text=decision.persona_text,
            talkative_cooldown_enabled=decision.talkative_cooldown_enabled,
            talkative_cooldown_streak_threshold=decision.talkative_cooldown_streak_threshold,
            talkative_cooldown_topic_overlap_threshold=decision.talkative_cooldown_topic_overlap_threshold,
            talkative_cooldown_cooldown_seconds=decision.talkative_cooldown_cooldown_seconds,
            talkative_cooldown_delay_seconds=decision.talkative_cooldown_delay_seconds,
            talkative_cooldown_use_llm_message=decision.talkative_cooldown_use_llm_message,
            is_owner=decision.is_owner,
            model_profile=decision.model_profile,
            session_history_limit=decision.session_history_limit,
            private_handoff_id=decision.private_handoff_id,
        )

    async def process_direct(
        self,
        content: str,
        *,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        allowed_tools: set[str] | None = None,
        persona_text: str | None = None,
        is_owner: bool = True,
        model_profile: str | None = None,
    ) -> str:
        return await self._generate(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            content=content,
            sender_id=chat_id,
            media=(),
            metadata={},
            allowed_tools=set(allowed_tools or self.tool_names),
            persona_text=persona_text,
            talkative_cooldown_enabled=False,
            talkative_cooldown_streak_threshold=7,
            talkative_cooldown_topic_overlap_threshold=0.34,
            talkative_cooldown_cooldown_seconds=900,
            talkative_cooldown_delay_seconds=2.5,
            talkative_cooldown_use_llm_message=False,
            is_owner=is_owner,
            model_profile=model_profile,
        ) or ""

    async def aclose(self) -> None:
        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            await exec_tool.aclose()

    def close(self) -> None:
        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.close()

    async def send_outbound(self, message: OutboundMessage) -> None:
        """Convenience wrapper used by tests and callers needing direct publish."""
        await self.bus.publish_outbound(message)
