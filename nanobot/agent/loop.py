"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService
    from nanobot.policy.engine import PolicyEngine
    from nanobot.policy.middleware import PolicyMiddleware

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
from nanobot.policy.middleware import MessagePolicyContext, PolicyMiddleware
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
        policy_middleware: "PolicyMiddleware | None" = None,
        policy_path: Path | None = None,
        timing_logs_enabled: bool = False,
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
        self.policy: "PolicyMiddleware | None" = policy_middleware
        self.policy_path = policy_path
        self.timing_logs_enabled = timing_logs_enabled
        self.policy_counters = {
            "dropped_by_access": 0,
            "dropped_by_reply": 0,
            "blocked_tool_call": 0,
        }
        self._created_at_epoch = time.time()
        self._started_at_epoch: float | None = None
        self._last_usage_by_session: dict[str, dict[str, int]] = {}
        self._zai_usage_cache: dict[str, tuple[float, str]] = {}
        self._zai_usage_cache_ttl_seconds = 30.0

        self._running = False
        self._register_default_tools()
        tool_names = set(self.tools.tool_names)
        if self.policy is not None:
            self.policy.engine.validate(tool_names)
        elif self.policy_engine:
            self.policy_engine.validate(tool_names)
            self.policy = PolicyMiddleware(
                engine=self.policy_engine,
                known_tools=tool_names,
                policy_path=self.policy_path,
            )

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
        if self._started_at_epoch is None:
            self._started_at_epoch = time.time()
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

        total_started = time.perf_counter()
        policy_ms = 0.0
        context_ms = 0.0
        llm_ms = 0.0
        tools_ms = 0.0

        def log_timing(iterations: int) -> None:
            if not self.timing_logs_enabled:
                return
            total_ms = (time.perf_counter() - total_started) * 1000
            logger.info(
                "timing channel={} chat={} total_ms={:.1f} policy_ms={:.1f} "
                "context_ms={:.1f} llm_ms={:.1f} tools_ms={:.1f} iterations={}",
                msg.channel,
                msg.chat_id,
                total_ms,
                policy_ms,
                context_ms,
                llm_ms,
                tools_ms,
                iterations,
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)

        # Policy evaluation (access/reply/tool/persona)
        policy_started = time.perf_counter()
        policy_ctx = self._policy_context(msg)
        policy_ms = (time.perf_counter() - policy_started) * 1000
        self._log_policy_decision(msg, policy_ctx)
        if not policy_ctx.decision.accept_message:
            self.policy_counters["dropped_by_access"] += 1
            log_timing(iterations=0)
            return None
        if not policy_ctx.decision.should_respond:
            self.policy_counters["dropped_by_reply"] += 1
            log_timing(iterations=0)
            return None
        persona_text = policy_ctx.persona_text

        fast_status = await self._try_fast_status(msg.content, msg, session, policy_ctx)
        if fast_status is not None:
            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {fast_status[:120]}")
            session.add_message("user", msg.content)
            session.add_message("assistant", fast_status)
            self.sessions.save(session)
            log_timing(iterations=0)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=fast_status,
            )

        fast_model_info = self._try_fast_model_info(msg.content)
        if fast_model_info is not None:
            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {fast_model_info[:120]}")
            session.add_message("user", msg.content)
            session.add_message("assistant", fast_model_info)
            self.sessions.save(session)
            log_timing(iterations=0)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=fast_model_info,
            )

        # Fast path: avoid slow multi-step tool chains for simple weather-now queries.
        # This remains policy-aware through capability gating.
        fast_weather = None
        if self._allow_internal_capability("weather_fastpath", policy_ctx):
            fast_weather = await self._try_fast_weather(msg.content)
        if fast_weather is not None:
            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {fast_weather[:120]}")
            session.add_message("user", msg.content)
            session.add_message("assistant", fast_weather)
            self.sessions.save(session)
            log_timing(iterations=0)
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
        context_started = time.perf_counter()
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            persona_text=persona_text,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        context_ms = (time.perf_counter() - context_started) * 1000

        # Agent loop
        iteration = 0
        final_content = None

        while iteration < self.max_iterations:
            iteration += 1

            # Call LLM
            llm_started = time.perf_counter()
            response = await self.provider.chat(
                messages=messages,
                tools=self._tool_definitions(policy_ctx.decision.allowed_tools),
                model=self.model
            )
            self._record_usage(msg.session_key, response.usage)
            llm_ms += (time.perf_counter() - llm_started) * 1000

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
                    tool_started = time.perf_counter()
                    if not self._is_tool_allowed(tool_call.name, policy_ctx):
                        self.policy_counters["blocked_tool_call"] += 1
                        result = f"Error: Tool '{tool_call.name}' is blocked by policy for this chat."
                    else:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    tools_ms += (time.perf_counter() - tool_started) * 1000
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
        log_timing(iterations=iteration)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content
        )

    def _policy_context(self, msg: InboundMessage) -> MessagePolicyContext:
        """Evaluate policy context for one message."""
        if self.policy is None:
            allowed = set(self.tools.tool_names)
            fallback_actor = ActorContext(
                channel=msg.channel,
                chat_id=msg.chat_id,
                sender_primary=str(msg.sender_id),
                sender_aliases=[str(msg.sender_id)],
                is_group=bool((msg.metadata or {}).get("is_group", False)),
                mentioned_bot=bool((msg.metadata or {}).get("mentioned_bot", False)),
                reply_to_bot=bool((msg.metadata or {}).get("reply_to_bot", False)),
            )
            return MessagePolicyContext(
                actor=fallback_actor,
                decision=PolicyDecision(
                    accept_message=True,
                    should_respond=True,
                    allowed_tools=allowed,
                    persona_file=None,
                    reason="policy_disabled",
                ),
                effective_policy=None,
                persona_text=None,
                source="disabled",
            )
        return self.policy.evaluate_message(msg)

    def _tool_definitions(self, allowed_tools: set[str]) -> list[dict]:
        """Filter tool schemas by policy."""
        definitions = self.tools.get_definitions()
        if self.policy:
            return self.policy.filter_tool_definitions(definitions, allowed_tools)
        return [
            schema
            for schema in definitions
            if schema.get("function", {}).get("name") in allowed_tools
        ]

    def _is_tool_allowed(self, tool_name: str, context: MessagePolicyContext) -> bool:
        if self.policy:
            return self.policy.is_tool_allowed(tool_name, context)
        return tool_name in context.decision.allowed_tools

    def _allow_internal_capability(self, capability: str, context: MessagePolicyContext) -> bool:
        if self.policy:
            return self.policy.allows_capability(capability, context)
        return True

    def _log_policy_decision(self, msg: InboundMessage, context: MessagePolicyContext) -> None:
        """Structured policy debug line for observability."""
        logger.debug(
            "policy_decision channel={} chat={} sender={} accepted={} replied={} "
            "reason={} tools_count={} persona={} source={} actor_primary={}",
            msg.channel,
            msg.chat_id,
            msg.sender_id,
            context.decision.accept_message,
            context.decision.should_respond,
            context.decision.reason,
            len(context.decision.allowed_tools),
            context.decision.persona_file or "-",
            context.source,
            context.actor.sender_primary,
        )

    def _try_fast_model_info(self, text: str) -> str | None:
        """Return configured model for explicit model commands only."""
        normalized = " ".join(text.split()).strip().lower()
        if not normalized:
            return None
        response = f"Configured model: {self.model} (from ~/.nanobot/config.json)."
        if normalized == "/model" or normalized.startswith("/model@"):
            return response
        return None

    async def _try_fast_status(
        self,
        text: str,
        msg: InboundMessage,
        session,
        context: MessagePolicyContext,
    ) -> str | None:
        """Return runtime status for explicit /status commands."""
        normalized = " ".join(text.split()).strip().lower()
        if not normalized:
            return None
        if normalized != "/status" and not normalized.startswith("/status@"):
            return None

        provider_name = type(self.provider).__name__
        api_key = getattr(self.provider, "api_key", None)
        api_key_masked = self._mask_secret(str(api_key)) if api_key else "-"
        raw_api_base = getattr(self.provider, "api_base", None)
        api_base = raw_api_base or "-"
        gateway = getattr(self.provider, "_gateway", None)
        gateway_name = getattr(gateway, "name", "-") if gateway else "-"
        raw_headers = getattr(self.provider, "extra_headers", None)
        header_names = sorted(raw_headers.keys()) if isinstance(raw_headers, dict) else []
        headers_text = ", ".join(header_names) if header_names else "-"

        usage = self._last_usage_by_session.get(msg.session_key, {})
        prompt_tokens = usage.get("prompt_tokens", "-")
        completion_tokens = usage.get("completion_tokens", "-")
        total_tokens = usage.get("total_tokens", "-")

        effective = context.effective_policy
        who_mode = effective.who_can_talk_mode if effective else "-"
        reply_mode = effective.when_to_reply_mode if effective else "-"
        allowed_tools = sorted(context.decision.allowed_tools)
        allowed_text = ", ".join(allowed_tools) if allowed_tools else "-"

        updated_at = session.updated_at.isoformat(timespec="seconds")
        uptime_seconds = int(time.time() - (self._started_at_epoch or self._created_at_epoch))
        zai_usage = await self._build_zai_usage_status(
            api_key=str(api_key) if isinstance(api_key, str) else None,
            api_base=raw_api_base if isinstance(raw_api_base, str) else None,
            gateway_name=gateway_name,
        )

        rows = [
            ("model", self.model),
            ("provider", f"{provider_name} gateway={gateway_name}"),
            ("api_base", str(api_base)),
            ("api_key", api_key_masked),
            ("session", f"{msg.session_key} msgs={len(session.messages)} updated={updated_at}"),
            ("channel", f"{msg.channel} chat={msg.chat_id} sender={msg.sender_id}"),
            (
                "policy",
                f"accept={context.decision.accept_message} reply={context.decision.should_respond} "
                f"who={who_mode} when={reply_mode} reason={context.decision.reason}",
            ),
            ("queue", f"in={self.bus.inbound_size} out={self.bus.outbound_size}"),
            (
                "counters",
                f"drop_access={self.policy_counters['dropped_by_access']} "
                f"drop_reply={self.policy_counters['dropped_by_reply']} "
                f"block_tools={self.policy_counters['blocked_tool_call']}",
            ),
            ("usage", f"in={prompt_tokens} out={completion_tokens} total={total_tokens}"),
            ("zai_usage", zai_usage or "-"),
            ("uptime", self._format_duration(uptime_seconds)),
            (
                "runtime",
                f"running={self._running} iterations={self.max_iterations} "
                f"isolation={self.exec_config.isolation.enabled} timing_logs={self.timing_logs_enabled}",
            ),
            ("tools", f"{len(allowed_tools)}/{len(self.tools.tool_names)} [{allowed_text}]"),
            ("headers", headers_text),
        ]
        lines = [f"{key:<10} {value}" for key, value in rows]
        return "```\n" + "\n".join(lines) + "\n```"

    @staticmethod
    def _mask_secret(value: str) -> str:
        """Mask secrets for status output."""
        if not value:
            return "-"
        if len(value) <= 8:
            return f"{value[:1]}...{value[-1:]}" if len(value) > 1 else "*"
        return f"{value[:6]}...{value[-6:]}"

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        """Format seconds into a compact duration string."""
        hours, rem = divmod(max(total_seconds, 0), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{seconds:02d}s"
        if minutes:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"

    def _record_usage(self, session_key: str, usage: dict[str, int]) -> None:
        """Track last provider token usage per session."""
        if not usage:
            return
        normalized: dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                normalized[key] = value
        if normalized:
            self._last_usage_by_session[session_key] = normalized

    async def _build_zai_usage_status(
        self,
        api_key: str | None,
        api_base: str | None,
        gateway_name: str,
    ) -> str | None:
        """Fetch and cache usage stats from Z.AI monitoring endpoints for /status."""
        if not self._should_query_zai_usage(api_key, api_base, gateway_name):
            return None
        if not api_key:
            return None

        monitor_base = self._resolve_zai_monitor_base(api_base)
        cache_key = f"{monitor_base}|{api_key}"
        now = time.monotonic()
        cached = self._zai_usage_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        summary = await self._fetch_zai_usage_status(monitor_base, api_key)
        if summary is None:
            return None
        self._zai_usage_cache[cache_key] = (now + self._zai_usage_cache_ttl_seconds, summary)
        return summary

    def _should_query_zai_usage(
        self,
        api_key: str | None,
        api_base: str | None,
        gateway_name: str,
    ) -> bool:
        """Only query Z.AI monitor APIs for direct Z.AI/Zhipu configurations."""
        if not api_key:
            return False
        if gateway_name and gateway_name != "-":
            return False

        base_lower = (api_base or "").lower()
        model_lower = self.model.lower()
        if "api.z.ai" in base_lower or "open.bigmodel.cn" in base_lower:
            return True
        return "glm" in model_lower or "zai/" in model_lower

    @staticmethod
    def _resolve_zai_monitor_base(api_base: str | None) -> str:
        """Resolve monitor host from configured base URL."""
        base_lower = (api_base or "").lower()
        if "open.bigmodel.cn" in base_lower:
            return "https://open.bigmodel.cn"
        return "https://api.z.ai"

    async def _fetch_zai_usage_status(self, monitor_base: str, api_key: str) -> str | None:
        """Query Z.AI internal usage endpoints and return a compact summary."""
        now = datetime.now(UTC)
        # Z.AI monitor endpoints expect: yyyy-MM-dd HH:mm:ss
        start = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        headers = {
            "Authorization": api_key,
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
        }
        requests: list[tuple[str, str, dict[str, str] | None, tuple[str, ...]]] = [
            (
                "quota",
                f"{monitor_base}/api/monitor/usage/quota/limit",
                None,
                ("quota", "limit", "used", "percent", "rate"),
            ),
            (
                "model24h",
                f"{monitor_base}/api/monitor/usage/model-usage",
                {"startTime": start, "endTime": end},
                ("token", "request", "count", "input", "output", "usage"),
            ),
            (
                "tool24h",
                f"{monitor_base}/api/monitor/usage/tool-usage",
                {"startTime": start, "endTime": end},
                ("tool", "request", "count", "success", "fail"),
            ),
        ]

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                tasks = [
                    client.get(url, headers=headers, params=params)
                    for _, url, params, _ in requests
                ]
                responses = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            return None

        parts: list[str] = []
        for (name, _, _, hints), response in zip(requests, responses, strict=False):
            if isinstance(response, Exception):
                continue
            if response.status_code != 200:
                parts.append(f"{name}=http{response.status_code}")
                continue
            try:
                payload = response.json()
            except Exception:
                parts.append(f"{name}=invalid_json")
                continue
            if isinstance(payload, dict):
                code = payload.get("code")
                if isinstance(code, int) and code != 200:
                    msg = payload.get("msg") or payload.get("message") or "error"
                    short_msg = str(msg).replace("\n", " ").strip()
                    if len(short_msg) > 42:
                        short_msg = short_msg[:39] + "..."
                    parts.append(f"{name}=err{code}({short_msg})")
                    continue
            if name == "quota":
                summary = self._summarize_zai_quota(payload)
            elif name == "model24h":
                summary = self._summarize_zai_model_usage(payload)
            elif name == "tool24h":
                summary = self._summarize_zai_tool_usage(payload)
            else:
                summary = self._summarize_usage_payload(payload, hints)
            parts.append(f"{name}={summary}")

        return " ; ".join(parts) if parts else None

    def _summarize_zai_quota(self, payload: Any) -> str:
        """Summarize Z.AI quota/limit payload."""
        data = payload.get("data") if isinstance(payload, dict) else None
        limits = data.get("limits") if isinstance(data, dict) else None
        if not isinstance(limits, list):
            return self._summarize_usage_payload(payload, ("limit", "usage", "remaining", "percentage"))

        parts: list[str] = []
        for item in limits[:2]:
            if not isinstance(item, dict):
                continue
            limit_type = str(item.get("type", "LIMIT")).replace("_LIMIT", "")
            remaining = item.get("remaining")
            current = item.get("currentValue")
            percentage = item.get("percentage")
            chunk = f"{limit_type}:"
            if isinstance(current, (int, float)):
                chunk += f" used={int(current) if float(current).is_integer() else current}"
            if isinstance(remaining, (int, float)):
                chunk += f" rem={int(remaining) if float(remaining).is_integer() else remaining}"
            if isinstance(percentage, (int, float)):
                pct = int(percentage) if float(percentage).is_integer() else percentage
                chunk += f" pct={pct}"
            parts.append(chunk)
        return " | ".join(parts) if parts else self._summarize_usage_payload(
            payload,
            ("limit", "usage", "remaining", "percentage"),
        )

    def _summarize_zai_model_usage(self, payload: Any) -> str:
        """Summarize Z.AI model usage payload."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return self._summarize_usage_payload(payload, ("model", "tokens", "usage", "count"))
        calls = data.get("modelCallCount", data.get("totalModelCallCount"))
        tokens = data.get("tokensUsage", data.get("totalTokensUsage"))
        total = data.get("totalUsage")
        parts: list[str] = []
        if isinstance(calls, (int, float)):
            parts.append(f"calls={int(calls) if float(calls).is_integer() else calls}")
        if isinstance(tokens, (int, float)):
            parts.append(f"tokens={int(tokens) if float(tokens).is_integer() else tokens}")
        if isinstance(total, (int, float)):
            parts.append(f"total={int(total) if float(total).is_integer() else total}")
        return " ".join(parts) if parts else self._summarize_usage_payload(
            payload,
            ("model", "tokens", "usage", "count"),
        )

    def _summarize_zai_tool_usage(self, payload: Any) -> str:
        """Summarize Z.AI tool usage payload."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return self._summarize_usage_payload(payload, ("tool", "count", "usage", "search", "read"))
        search = data.get("networkSearchCount", data.get("totalNetworkSearchCount"))
        web_read = data.get("webReadMcpCount", data.get("totalWebReadMcpCount"))
        zread = data.get("zreadMcpCount", data.get("totalZreadMcpCount"))
        total = data.get("totalUsage")
        parts: list[str] = []
        if isinstance(search, (int, float)):
            parts.append(f"search={int(search) if float(search).is_integer() else search}")
        if isinstance(web_read, (int, float)):
            parts.append(f"web_read={int(web_read) if float(web_read).is_integer() else web_read}")
        if isinstance(zread, (int, float)):
            parts.append(f"zread={int(zread) if float(zread).is_integer() else zread}")
        if isinstance(total, (int, float)):
            parts.append(f"total={int(total) if float(total).is_integer() else total}")
        return " ".join(parts) if parts else self._summarize_usage_payload(
            payload,
            ("tool", "count", "usage", "search", "read"),
        )

    def _summarize_usage_payload(self, payload: Any, key_hints: tuple[str, ...]) -> str:
        """Pick a few numeric facts from payload; fall back to compact JSON."""
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        pairs = self._extract_numeric_pairs(data, key_hints, max_pairs=3)
        if pairs:
            return " ".join(f"{k}={v}" for k, v in pairs)
        try:
            compact = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        except Exception:
            compact = str(data)
        if len(compact) > 96:
            return compact[:93] + "..."
        return compact

    def _extract_numeric_pairs(
        self,
        data: Any,
        key_hints: tuple[str, ...],
        max_pairs: int,
    ) -> list[tuple[str, str]]:
        """Extract numeric key/value pairs recursively with key-hint preference."""
        found: list[tuple[str, float]] = []

        def walk(node: Any, prefix: str = "") -> None:
            if len(found) >= 64:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    next_prefix = f"{prefix}.{key}" if prefix else str(key)
                    walk(value, next_prefix)
                return
            if isinstance(node, list):
                for idx, value in enumerate(node[:8]):
                    next_prefix = f"{prefix}[{idx}]"
                    walk(value, next_prefix)
                return
            if isinstance(node, bool):
                return
            if isinstance(node, (int, float)):
                found.append((prefix, float(node)))

        walk(data)
        if not found:
            return []

        hints_lower = tuple(h.lower() for h in key_hints)
        prioritized = [
            (path, value)
            for path, value in found
            if any(h in path.lower() for h in hints_lower)
        ]
        selected = prioritized or found
        pairs: list[tuple[str, str]] = []
        for path, value in selected[:max_pairs]:
            key = path.rsplit(".", 1)[-1] if path else "value"
            if "[" in key:
                key = key.split("[", 1)[0] or "value"
            if value.is_integer():
                value_text = str(int(value))
            else:
                value_text = f"{value:.2f}".rstrip("0").rstrip(".")
            pairs.append((key, value_text))
        return pairs

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
            self._record_usage(session_key, response.usage)

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
