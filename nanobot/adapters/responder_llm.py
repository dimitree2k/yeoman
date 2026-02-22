"""Typed responder that runs LLM + tools without legacy AgentLoop."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.file_access import FileAccessResolver, enable_grants
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.pi_stats import PiStatsTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.send_voice import SendVoiceTool, VoiceSendRequest
from nanobot.agent.tools.exec_isolation import SandboxMount
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import DeepResearchTool, WebFetchTool, WebSearchTool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import ResponderPort, SecurityPort, TelemetryPort
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager
from nanobot.media.tts import strip_markdown_for_tts, truncate_for_voice, write_tts_audio_file

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService
    from nanobot.media.router import ModelRouter
    from nanobot.media.tts import TTSSynthesizer
    from nanobot.memory.service import MemoryService


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
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        memory_service: "MemoryService | None" = None,
        telemetry: TelemetryPort | None = None,
        security: SecurityPort | None = None,
        owner_alert_resolver: "callable[[str], list[str]] | None" = None,
        file_access_resolver: FileAccessResolver | None = None,
        group_resolver: "callable[[str], tuple[str | None, str | None]] | None" = None,
        model_router: "ModelRouter | None" = None,
        tts: "TTSSynthesizer | None" = None,
        whatsapp_tts_outgoing_dir: Path | None = None,
        whatsapp_tts_max_raw_bytes: int = 160 * 1024,
    ) -> None:
        from nanobot.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.max_iterations = max(1, int(max_iterations))
        self.tavily_api_key = tavily_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.memory = memory_service
        self.telemetry = telemetry
        self.security = security
        self.owner_alert_resolver = owner_alert_resolver
        self.file_access_resolver = file_access_resolver
        self.group_resolver = group_resolver
        self._model_router = model_router
        self._tts = tts
        self._whatsapp_tts_outgoing_dir = whatsapp_tts_outgoing_dir
        self._whatsapp_tts_max_raw_bytes = max(1, int(whatsapp_tts_max_raw_bytes))
        self._seen_chats: set[str] = set()
        self._seen_chats_path = Path.home() / ".nanobot" / "seen_chats.json"
        self._load_seen_chats()
        self._talkative_state: dict[str, _TalkativeCooldownState] = {}

        self.effective_restrict_to_workspace = restrict_to_workspace or (
            self.exec_config.isolation.enabled
            and self.exec_config.isolation.force_workspace_restriction
        )

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()  # type: ignore[no-untyped-call]  # boundary-any
        subagent_model_to_use = subagent_model or self.model
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=subagent_model_to_use,
            tavily_api_key=tavily_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=self.effective_restrict_to_workspace,
            file_access_resolver=file_access_resolver,
        )
        self._register_default_tools()

    def _load_seen_chats(self) -> None:
        """Load seen chats from persistent storage."""
        try:
            if self._seen_chats_path.exists():
                data = json.loads(self._seen_chats_path.read_text())
                self._seen_chats = set(data.get("chats", []))
                logger.info(
                    "loaded {} seen chats from {}", len(self._seen_chats), self._seen_chats_path
                )
        except Exception as e:
            logger.warning("failed to load seen chats: {}", e)
            self._seen_chats = set()

    def _save_seen_chats(self) -> None:
        """Save seen chats to persistent storage."""
        try:
            self._seen_chats_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"chats": list(self._seen_chats)}
            self._seen_chats_path.write_text(json.dumps(data))
        except Exception as e:
            logger.warning("failed to save seen chats: {}", e)

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
        self.tools.register(PiStatsTool())

        self.tools.register(WebSearchTool(api_key=self.tavily_api_key))
        self.tools.register(WebFetchTool(api_key=self.tavily_api_key))
        self.tools.register(DeepResearchTool(api_key=self.tavily_api_key))

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

        if self.cron_service is not None:
            cron_tool = CronTool(self.cron_service)
            self.tools.register(cron_tool)

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

    def _set_tool_context(self, *, channel: str, chat_id: str, session_key: str) -> None:
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
        return f"Voice message sent to {channel}:{chat_id}"

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
    ) -> str:
        """Execute a tool call, activating grant context when appropriate."""
        if self._should_enable_grants(is_owner):
            with enable_grants():
                return await self.tools.execute(name, arguments)
        return await self.tools.execute(name, arguments)

    async def _chat_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        allowed_tools: set[str],
        security_context: dict[str, object] | None = None,
        is_owner: bool = False,
    ) -> str:
        iteration = 0
        final_content: str | None = None

        while iteration < self.max_iterations:
            iteration += 1
            response = await self.provider.chat(
                messages=messages,
                tools=self._tool_definitions(allowed_tools),
                model=self.model,
            )

            if response.has_tool_calls:
                tool_call_dicts: list[dict[str, Any]] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                )

                for tool_call in response.tool_calls:
                    args_preview = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_preview[:200])
                    if tool_call.name not in allowed_tools:
                        result = (
                            f"Error: Tool '{tool_call.name}' is blocked by policy for this chat."
                        )
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
                        else:
                            result = await self._execute_tool(
                                tool_call.name,
                                tool_call.arguments,
                                is_owner=is_owner,
                            )
                    messages = self.context.add_tool_result(
                        messages,
                        tool_call.id,
                        tool_call.name,
                        result,
                    )
                continue

            final_content = response.content
            break
        else:
            return "⚙️❓"  # max iterations reached without a text response

        return final_content or "🤔❓"

    async def _notify_new_chat(self, channel: str, chat_id: str) -> None:
        """Notify owner when Nano sees a new chat for the first time."""
        if self.owner_alert_resolver is None:
            return
        if channel != "whatsapp":
            return

        full_key = f"{channel}:{chat_id}"
        if full_key in self._seen_chats:
            return

        self._seen_chats.add(full_key)
        self._save_seen_chats()
        owners = self.owner_alert_resolver(channel)
        if not owners:
            return

        is_group = chat_id.endswith("@g.us")
        chat_type = "group" if is_group else "chat"
        message = f"🔔 Nano was added to a new WhatsApp {chat_type}: `{chat_id}`"

        for owner in owners:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=owner,
                    content=message,
                )
            )
            logger.info("notified owner {} about new chat {}", owner, chat_id)

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
            return (
                "Bro, du bist heute extrem gespraechig zum selben Thema. "
                "Nano braucht kurz Pause. "
                "Wenn du 24/7 quatschen willst, goenn dir ein OpenAI/Kimi/Anthropic-Abo."
            )
        return (
            "Bro, you are very talkative on the same topic today. "
            "Nano needs a short break. "
            "If you want 24/7 bot chat, get an OpenAI/Kimi/Anthropic subscription."
        )

    async def _generate_talkative_message_llm(self, text: str) -> str | None:
        language_hint = "German" if self._is_probably_german(text) else "English"
        prompt = [
            {
                "role": "system",
                "content": (
                    "You write one short playful cooldown message for a busy group chat. "
                    "No markdown. No threats. No slurs. No factual claims. "
                    "Max 2 sentences and max 160 characters."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Write a cheeky message telling one very talkative person to take a short pause, "
                    "and suggest buying an OpenAI/Kimi/Anthropic subscription for all-day bot chatting. "
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
    ) -> str:
        # Check for new chat and notify owner
        await self._notify_new_chat(channel, chat_id)

        session = self.sessions.get_or_create(session_key)

        # Save session immediately on first message (even if no response yet)
        if not session.messages:
            session.add_message("user", content)
            self.sessions.save(session)
            # Track that we've already added the user message to avoid duplication
            _user_message_already_added = True
        else:
            _user_message_already_added = False

        self._set_tool_context(channel=channel, chat_id=chat_id, session_key=session_key)

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

        owner_raw_voice_reply = await self._maybe_handle_owner_raw_voice_command(
            channel=channel,
            content=content,
            is_owner=is_owner,
        )
        if owner_raw_voice_reply is not None:
            final_content = owner_raw_voice_reply
        else:
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
                messages = self.context.build_messages(
                    history=session.get_history(),
                    current_message=content,
                    current_metadata=metadata,
                    retrieved_memory_text=retrieved_memory_text,
                    persona_text=persona_text,
                    media=list(media),
                    channel=channel,
                    chat_id=chat_id,
                )

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
                )

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
            session.add_message("user", content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)
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
                getattr(decision, "voice_output_max_sentences", 2) or 2
            )
            metadata["voice_reply_max_chars"] = int(
                getattr(decision, "voice_output_max_chars", 150) or 150
            )
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
        )

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
