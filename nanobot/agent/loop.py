"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService
    from nanobot.policy.engine import PolicyEngine

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.policy.engine import ActorContext, PolicyDecision
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        policy_engine: "PolicyEngine | None" = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.effective_restrict_to_workspace = (
            restrict_to_workspace
            or (
                self.exec_config.isolation.enabled
                and self.exec_config.isolation.force_workspace_restriction
            )
        )

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=self.effective_restrict_to_workspace,
        )
        self.policy_engine = policy_engine
        self.policy_counters = {
            "dropped_by_access": 0,
            "dropped_by_reply": 0,
            "blocked_tool_call": 0,
        }
        self._warned_missing_wa_mention_meta: set[str] = set()

        self._running = False
        self._register_default_tools()
        if self.policy_engine:
            self.policy_engine.validate(set(self.tools.tool_names))

    _WEATHER_KEYWORDS = ("weather", "wetter", "temperature", "temp")
    _WEATHER_BLOCKLIST = (
        "forecast",
        "tomorrow",
        "next week",
        "this week",
        "compare",
        "historical",
        "yesterday",
        "rain tomorrow",
    )
    _WEATHER_LOCATION_PATTERNS = (
        re.compile(r"\bweather\s+in\s+([^\n\?\.,;:!]+)", re.IGNORECASE),
        re.compile(r"\bin\s+([^\n\?\.,;:!]+)\s+(?:now|today|currently)\b", re.IGNORECASE),
        re.compile(r"\bfor\s+([^\n\?\.,;:!]+)\b", re.IGNORECASE),
    )

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.effective_restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))

        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.effective_restrict_to_workspace,
            isolation_config=self.exec_config.isolation,
        ))

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())

        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )

                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.close()
        logger.info("Agent loop stopping")

    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.

        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)

        # Policy evaluation (access/reply/tool persona)
        decision = self._policy_decision(msg)
        self._log_policy_decision(msg, decision)
        if not decision.accept_message:
            self.policy_counters["dropped_by_access"] += 1
            return None
        if not decision.should_respond:
            self.policy_counters["dropped_by_reply"] += 1
            return None
        persona_text = self._persona_text(decision)

        # Fast path: avoid slow multi-step tool chains for simple weather-now queries.
        fast_weather = await self._try_fast_weather(msg.content)
        if fast_weather is not None:
            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {fast_weather[:120]}")
            session.add_message("user", msg.content)
            session.add_message("assistant", fast_weather)
            self.sessions.save(session)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=fast_weather,
            )

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)

        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.set_session_context(msg.session_key)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)

        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            persona_text=persona_text,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        # Agent loop
        iteration = 0
        final_content = None

        while iteration < self.max_iterations:
            iteration += 1

            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self._tool_definitions(decision.allowed_tools),
                model=self.model
            )

            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    if tool_call.name not in decision.allowed_tools:
                        self.policy_counters["blocked_tool_call"] += 1
                        result = f"Error: Tool '{tool_call.name}' is blocked by policy for this chat."
                    else:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Log response preview
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )

    @staticmethod
    def _sender_aliases(sender_id: str) -> tuple[str, list[str]]:
        """Normalize sender identifier and aliases."""
        raw_parts = [p.strip() for p in str(sender_id).split("|") if p.strip()]
        if not raw_parts:
            return "", []
        primary = raw_parts[0]
        aliases = list(dict.fromkeys(raw_parts))
        return primary, aliases

    def _policy_decision(self, msg: InboundMessage) -> PolicyDecision:
        """Evaluate policy for inbound message, defaulting to allow-all."""
        default_allowed = set(self.tools.tool_names)
        if not self.policy_engine:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=default_allowed,
                persona_file=None,
                reason="policy_disabled",
            )

        sender_primary, sender_aliases = self._sender_aliases(msg.sender_id)
        metadata = msg.metadata or {}
        actor = ActorContext(
            channel=msg.channel,
            chat_id=msg.chat_id,
            sender_primary=sender_primary,
            sender_aliases=sender_aliases,
            is_group=bool(metadata.get("is_group", False)),
            mentioned_bot=bool(metadata.get("mentioned_bot", False)),
            reply_to_bot=bool(metadata.get("reply_to_bot", False)),
        )
        decision = self.policy_engine.evaluate(actor, default_allowed)
        if (
            msg.channel == "whatsapp"
            and actor.is_group
            and "mentioned_bot" not in metadata
            and "reply_to_bot" not in metadata
            and decision.reason.endswith("when_to_reply:mention_only_group")
        ):
            warning_key = f"{msg.channel}:{msg.chat_id}"
            if warning_key not in self._warned_missing_wa_mention_meta:
                logger.warning(
                    "whatsapp mention metadata missing; mention_only groups will fail-closed until bridge metadata is available"
                )
                self._warned_missing_wa_mention_meta.add(warning_key)
        return decision

    def _persona_text(self, decision: PolicyDecision) -> str | None:
        if not self.policy_engine:
            return None
        return self.policy_engine.persona_text(decision.persona_file)

    def _tool_definitions(self, allowed_tools: set[str]) -> list[dict]:
        """Filter tool schemas by policy."""
        return [
            schema
            for schema in self.tools.get_definitions()
            if schema.get("function", {}).get("name") in allowed_tools
        ]

    def _log_policy_decision(self, msg: InboundMessage, decision: PolicyDecision) -> None:
        """Structured policy debug line for observability."""
        logger.debug(
            "policy_decision channel={} chat={} sender={} accepted={} replied={} "
            "reason={} tools_count={} persona={}",
            msg.channel,
            msg.chat_id,
            msg.sender_id,
            decision.accept_message,
            decision.should_respond,
            decision.reason,
            len(decision.allowed_tools),
            decision.persona_file or "-",
        )

    async def _try_fast_weather(self, text: str) -> str | None:
        """Handle simple current-weather prompts with one direct wttr.in request."""
        lowered = text.lower()
        if not any(k in lowered for k in self._WEATHER_KEYWORDS):
            return None
        if any(b in lowered for b in self._WEATHER_BLOCKLIST):
            return None

        location = self._extract_weather_location(text)
        encoded_location = quote(location) if location else ""
        url = f"https://wttr.in/{encoded_location}?format=%l:+%c+%t+%h+%w"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"User-Agent": "nanobot-weather/1.0"})
            raw = (resp.text or "").strip()
            if not raw:
                return None
            if raw.startswith("<!DOCTYPE") or raw.startswith("<html"):
                return None
            return self._format_weather_line(raw)
        except Exception:
            return None

    def _extract_weather_location(self, text: str) -> str:
        """Extract a likely location from a weather question."""
        cleaned = text.strip().strip("\"'")
        for pattern in self._WEATHER_LOCATION_PATTERNS:
            m = pattern.search(cleaned)
            if m:
                location = m.group(1).strip()
                location = re.sub(
                    r"\s+(right\s+now|now|today|currently)\s*$",
                    "",
                    location,
                    flags=re.IGNORECASE,
                ).strip()
                return location
        return ""

    @staticmethod
    def _format_weather_line(line: str) -> str:
        """Format wttr compact output into a user-friendly reply."""
        if ":" in line:
            location, rest = line.split(":", 1)
            location = location.strip()
            rest = " ".join(rest.split())
            parts = rest.split()
            if len(parts) >= 4:
                condition = parts[0]
                temp = parts[1]
                humidity = parts[2]
                wind = " ".join(parts[3:])
                return (
                    f"Current weather in {location}:\n\n"
                    f"{condition} **{temp}**\n"
                    f"Humidity: {humidity}\n"
                    f"Wind: {wind}"
                )
            return f"Current weather in {location}: {rest}"
        return f"Current weather: {line}"

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)

        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)

        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)

        exec_tool = self.tools.get("exec")
        if isinstance(exec_tool, ExecTool):
            exec_tool.set_session_context(session_key)

        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)

        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )

        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )

            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break

        if final_content is None:
            final_content = "Background task completed."

        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )

        response = await self._process_message(msg)
        return response.content if response else ""
