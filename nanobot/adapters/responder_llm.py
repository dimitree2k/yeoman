"""Typed responder that runs LLM + tools without legacy AgentLoop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.pi_stats import PiStatsTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import ResponderPort, SecurityPort, TelemetryPort
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService
    from nanobot.memory.service import MemoryService


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
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        memory_service: "MemoryService | None" = None,
        telemetry: TelemetryPort | None = None,
        security: SecurityPort | None = None,
        owner_alert_resolver: "callable[[str], list[str]] | None" = None,
    ) -> None:
        from nanobot.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.max_iterations = max(1, int(max_iterations))
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.memory = memory_service
        self.telemetry = telemetry
        self.security = security
        self.owner_alert_resolver = owner_alert_resolver
        self._seen_chats: set[str] = set()
        self._seen_chats_path = Path.home() / ".nanobot" / "seen_chats.json"
        self._load_seen_chats()

        self.effective_restrict_to_workspace = (
            restrict_to_workspace
            or (
                self.exec_config.isolation.enabled
                and self.exec_config.isolation.force_workspace_restriction
            )
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
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=self.effective_restrict_to_workspace,
        )
        self._register_default_tools()

    def _load_seen_chats(self) -> None:
        """Load seen chats from persistent storage."""
        try:
            if self._seen_chats_path.exists():
                data = json.loads(self._seen_chats_path.read_text())
                self._seen_chats = set(data.get("chats", []))
                logger.info("loaded {} seen chats from {}", len(self._seen_chats), self._seen_chats_path)
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
        allowed_dir = self.workspace if self.effective_restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        exec_tool = ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.effective_restrict_to_workspace,
            allow_host_execution=self.exec_config.allow_host_execution,
            isolation_config=self.exec_config.isolation,
        )
        self.tools.register(exec_tool)
        self.tools.register(PiStatsTool())

        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())

        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

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
    def _route_for_event(event: InboundEvent) -> tuple[str, str]:
        if event.channel != "system":
            return event.channel, event.chat_id
        if ":" not in event.chat_id:
            return "cli", event.chat_id
        channel, chat_id = event.chat_id.split(":", 1)
        if not channel or not chat_id:
            return "cli", event.chat_id
        return channel, chat_id

    @staticmethod
    def _metadata_for_event(event: InboundEvent) -> dict[str, object]:
        metadata = dict(event.raw_metadata)
        metadata.update({
            "message_id": event.message_id,
            "sender_id": event.sender_id,
            "participant": event.participant,
            "is_group": event.is_group,
            "mentioned_bot": event.mentioned_bot,
            "reply_to_bot": event.reply_to_bot,
            "reply_to_message_id": event.reply_to_message_id,
            "reply_to_participant": event.reply_to_participant,
            "reply_to_text": event.reply_to_text,
        })
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

    async def _chat_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        allowed_tools: set[str],
        security_context: dict[str, object] | None = None,
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
                        result = f"Error: Tool '{tool_call.name}' is blocked by policy for this chat."
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
                                result = await self.tools.execute(tool_call.name, tool_call.arguments)
                        else:
                            result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages,
                        tool_call.id,
                        tool_call.name,
                        result,
                    )
                continue

            final_content = response.content
            break

        return final_content or "I've completed processing but have no response to give."

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

        retrieved_memory_text = ""
        retrieved_hits_count = 0
        if self.memory is not None:
            try:
                retrieved_memory_text, retrieved_hits = self.memory.build_retrieved_context(
                    channel=channel,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    query=content,
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
