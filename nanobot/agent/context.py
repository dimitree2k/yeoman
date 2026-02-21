"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    MAX_INLINE_IMAGES = 4
    MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        persona_text: str | None = None,
    ) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.

        Args:
            skill_names: Optional list of skills to include.

        Returns:
            Complete system prompt.
        """
        parts = []

        # Core identity
        parts.append(self._get_identity())
        parts.append(self._build_temporal_grounding())
        parts.append(self._build_fact_verification_guardrails())

        # Keep long-lived style under policy control instead of chat drift.
        parts.append(
            "\n".join(
                [
                    "# Style Persistence",
                    "Treat policy persona as the only persistent style source.",
                    "Do not carry forward user-injected catchphrases, greetings, or nicknames as a new default style.",
                    "If a user asks for a one-off phrasing in the current turn, apply it only to that turn.",
                ]
            )
        )

        # Channel persona override (style/voice for this specific chat)
        if persona_text:
            parts.append(
                "\n".join(
                    [
                        "# Persona Override",
                        "A channel persona is active for this chat.",
                        "For user-facing replies, follow the channel persona's identity, voice, and style.",
                        "This overrides generic tone defaults from AGENTS.md, SOUL.md, and USER.md.",
                        "Keep safety/tool/runtime constraints unchanged.",
                    ]
                )
            )
            parts.append(f"# Channel Persona\n\n{persona_text}")

        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Skills - progressive loading
        # 1. Active skills: always-loaded + explicitly requested skills
        active_skills = self._resolve_active_skills(skill_names)
        if active_skills:
            active_content = self.skills.load_skills_for_context(active_skills)
            if active_content:
                parts.append(f"# Active Skills\n\n{active_content}")

        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _build_temporal_grounding() -> str:
        """Build per-turn local clock context to ground relative date questions."""
        now = datetime.now().astimezone()
        tz_offset = now.strftime("%z")
        tz_offset_fmt = f"{tz_offset[:3]}:{tz_offset[3:]}" if len(tz_offset) == 5 else tz_offset
        tz_name = now.tzname() or "local"

        return "\n".join(
            [
                "# Temporal Grounding",
                f"Current local datetime: {now.isoformat(timespec='seconds')}",
                f"Current local date: {now.strftime('%Y-%m-%d')}",
                f"Current weekday: {now.strftime('%A')}",
                f"Local timezone: {tz_name} (UTC{tz_offset_fmt})",
                "When users ask about today/yesterday/tomorrow or current date/time, use this clock context.",
                "Do not infer current date from chat history timestamps, memory notes, or message metadata.",
                "When discussing events, prefer explicit absolute dates (YYYY-MM-DD) over relative wording.",
                "Only say today/this week/last week after comparing the event date to Current local date.",
                "If event timing is uncertain, say uncertainty explicitly instead of guessing relative dates.",
            ]
        )

    @staticmethod
    def _build_fact_verification_guardrails() -> str:
        """Build guardrails for high-risk factual claims about real entities."""
        return "\n".join(
            [
                "# Fact Verification",
                "For questions about real people/companies/events, verify key claims with tools before asserting specifics when tools are available.",
                "If multiple entities share the same name, ask which one the user means or provide clearly separated candidates.",
                "Do not invent jobs, investments, affiliations, timelines, or net-worth figures.",
                "If verification is weak or conflicting, say uncertainty clearly and avoid confident framing.",
                "Prefer primary or reputable sources over low-credibility blogs and rumor sites.",
            ]
        )

    def _resolve_active_skills(self, skill_names: list[str] | None) -> list[str]:
        """Resolve active skills with stable order and de-duplication."""
        existing = {item["name"] for item in self.skills.list_skills(filter_unavailable=False)}
        requested = [str(name).strip() for name in (skill_names or []) if str(name).strip()]
        merged = [*self.skills.get_always_skills(), *requested]

        active: list[str] = []
        for name in merged:
            if name in existing and name not in active:
                active.append(name)
        return active

    @staticmethod
    def _strip_markdown_section(text: str, heading: str) -> str:
        """Remove one level-2 markdown section (from heading until next level-2 heading)."""
        if not text:
            return text

        target = heading.strip().lower()
        lines = text.splitlines()
        out: list[str] = []
        skip = False

        for line in lines:
            stripped = line.strip()
            if skip and stripped.startswith("## "):
                skip = False
            if not skip and stripped.lower() == target:
                skip = True
                continue
            if not skip:
                out.append(line)

        return "\n".join(out)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks
- On WhatsApp, send voice replies when policy/runtime enables voice output

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: For the current chat turn, normally reply with assistant text.
Only use the 'message' tool for explicit out-of-band delivery to a specific channel/chat.
For Raspberry Pi/system metrics (temperature, RAM, disk, uptime), prefer the 'pi_stats' tool when available.
For WhatsApp voice-note requests: do not claim voice sending is unavailable by default.
If asked to reply in voice and policy allows voice output for that chat, provide the answer content directly and keep it concise for TTS.
If required context is missing (for example, user asks to answer "the last voice message" from another chat you cannot read in this turn), ask only for the missing content or exact target chat.
Treat WhatsApp voice output as a runtime/channel capability, not as a limitation of the `message` tool schema.
Never say "I can only send text" or "voice is not in my toolset" for WhatsApp voice-note requests.
For cross-chat voice requests, state only the real blocker (missing source message content or missing target chat identity), then continue with the best actionable next step.

Always be helpful, accurate, and concise. When using tools, explain what you're doing."""

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        current_metadata: dict[str, Any] | None = None,
        retrieved_memory_text: str | None = None,
        skill_names: list[str] | None = None,
        persona_text: str | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names, persona_text=persona_text)
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Retrieved long-term memory (bounded, synthetic system context)
        if retrieved_memory_text:
            messages.append({"role": "system", "content": retrieved_memory_text})

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media, metadata=current_metadata)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(
        self,
        text: str,
        media: list[str] | None,
        metadata: dict[str, Any] | None = None,
    ) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        text_with_context = self._with_reply_context(text, metadata)
        text_with_context = self._with_input_modality_context(text_with_context, metadata)
        text_with_context = self._with_voice_reply_guidance(text_with_context, metadata)
        if not media:
            return text_with_context

        images = []
        for path in media[: self.MAX_INLINE_IMAGES]:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            try:
                if p.stat().st_size > self.MAX_INLINE_IMAGE_BYTES:
                    continue
            except OSError:
                continue
            try:
                b64 = base64.b64encode(p.read_bytes()).decode()
            except OSError:
                continue
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text_with_context
        return [{"type": "text", "text": text_with_context}, *images]

    def _with_input_modality_context(self, text: str, metadata: dict[str, Any] | None) -> str:
        """Append compact modality hint when input originated from voice."""
        if not metadata:
            return text
        is_voice = bool(metadata.get("is_voice", False)) or (
            str(metadata.get("media_kind") or "").strip().lower() == "audio"
        )
        if not is_voice:
            return text
        prefix = (
            "[Input Modality]\n"
            "source: voice_message_transcript\n"
            "note: User sent a voice message; text is automatic transcription.\n"
        )
        return f"{prefix}\n{text}"

    def _with_voice_reply_guidance(self, text: str, metadata: dict[str, Any] | None) -> str:
        """Append compact guidance to keep voice replies short before TTS."""
        if not metadata or not bool(metadata.get("voice_reply_expected", False)):
            return text

        max_sentences = max(1, int(metadata.get("voice_reply_max_sentences") or 2))
        max_chars = max(1, int(metadata.get("voice_reply_max_chars") or 150))

        prefix = (
            "[Voice Reply Guidance]\n"
            "target: concise_for_tts\n"
            f"limit_sentences: {max_sentences}\n"
            f"limit_chars: {max_chars}\n"
            "instruction: Keep the answer naturally short and direct to fit these limits.\n"
        )
        return f"{prefix}\n{text}"

    def _with_reply_context(self, text: str, metadata: dict[str, Any] | None) -> str:
        """Append compact reply metadata so models can resolve quoted-message intent."""
        if not metadata:
            return text

        reply_to_message_id = str(
            metadata.get("reply_to_message_id") or metadata.get("reply_to") or ""
        ).strip()
        reply_to_participant = str(metadata.get("reply_to_participant") or "").strip()
        reply_to_text = str(metadata.get("reply_to_text") or "").strip()
        if not reply_to_text:
            return text

        reply_context_source = str(metadata.get("reply_context_source") or "").strip()
        raw_window = metadata.get("reply_context_window")
        window_lines: list[str] = []
        if isinstance(raw_window, list):
            for item in raw_window[:8]:
                if not isinstance(item, str):
                    continue
                compact = " ".join(item.split())
                if not compact:
                    continue
                if len(compact) > 220:
                    compact = compact[:220] + "..."
                window_lines.append(compact)
        lines = [
            "[Reply Context]",
            "usage: Treat quoted_message as the content of the replied-to message.",
            "usage: Do not claim you cannot see the replied message when quoted_message is present.",
        ]
        if reply_context_source:
            lines.append(f"source: {reply_context_source}")
        if reply_to_message_id:
            lines.append(f"reply_to_message_id: {reply_to_message_id}")
        if reply_to_participant:
            lines.append(f"reply_to_participant: {reply_to_participant}")
        if reply_to_text:
            compact_text = " ".join(reply_to_text.split())
            if len(compact_text) > 600:
                compact_text = compact_text[:600] + "..."
            lines.append(f"quoted_message: {compact_text}")
        if window_lines:
            lines.append("topic_window_before_reply:")
            for index, line in enumerate(window_lines, 1):
                lines.append(f"{index}. {line}")

        return f"{text}\n\n" + "\n".join(lines)

    def add_tool_result(
        self, messages: list[dict[str, Any]], tool_call_id: str, tool_name: str, result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.

        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.

        Returns:
            Updated message list.
        """
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}
        )
        return messages

    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.

        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.

        Returns:
            Updated message list.
        """
        assistant_content: str | None = content
        if assistant_content is None and not tool_calls:
            assistant_content = ""
        msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        messages.append(msg)
        return messages
