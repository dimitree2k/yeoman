"""Tests for smart context windowing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
from yeoman_gateway.session.manager import Session, SessionManager


def _make_event(
    chat_id: str = "owner@s.whatsapp.net",
    message_id: str = "msg-1",
    content: str = "hello",
    channel: str = "whatsapp",
    is_group: bool = False,
) -> InboundEvent:
    return InboundEvent(
        channel=channel,
        chat_id=chat_id,
        sender_id="owner@s.whatsapp.net",
        content=content,
        message_id=message_id,
        is_group=is_group,
        raw_metadata={},
    )


class TestAmbientWindowSkipDM:
    """Ambient window should be empty for DM chats, populated for groups."""

    def test_dm_returns_empty_ambient(self):
        archive = MagicMock()
        archive.lookup_messages_before = MagicMock(return_value=[])
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="owner@s.whatsapp.net", is_group=False)
        result = mw._build_ambient_window(event)

        assert result == []
        archive.lookup_messages_before.assert_not_called()

    def test_group_calls_archive(self):
        archive = MagicMock()
        archive.lookup_messages_before = MagicMock(return_value=[])
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="123456@g.us", is_group=True)
        mw._build_ambient_window(event)

        archive.lookup_messages_before.assert_called_once()


class TestSessionBoundary:
    """Session.get_history() should stop at the most recent session_boundary."""

    def test_no_boundary_returns_all(self):
        s = Session(key="test")
        s.add_message("user", "msg1")
        s.add_message("assistant", "reply1")
        s.add_message("user", "msg2")
        s.add_message("assistant", "reply2")
        history = s.get_history(max_messages=50)
        assert len(history) == 4

    def test_boundary_limits_history(self):
        s = Session(key="test")
        s.add_message("user", "old message")
        s.add_message("assistant", "old reply")
        s.add_boundary()
        s.add_message("user", "new message")
        s.add_message("assistant", "new reply")
        history = s.get_history(max_messages=50)
        assert len(history) == 2
        assert history[0]["content"] == "new message"
        assert history[1]["content"] == "new reply"

    def test_multiple_boundaries_uses_latest(self):
        s = Session(key="test")
        s.add_message("user", "ancient")
        s.add_boundary()
        s.add_message("user", "old")
        s.add_boundary()
        s.add_message("user", "recent")
        history = s.get_history(max_messages=50)
        assert len(history) == 1
        assert history[0]["content"] == "recent"

    def test_boundary_with_max_messages_uses_smaller(self):
        s = Session(key="test")
        s.add_boundary()
        for i in range(20):
            s.add_message("user", f"msg {i}")
        history = s.get_history(max_messages=5)
        assert len(history) == 5

    def test_boundary_at_end_returns_empty(self):
        s = Session(key="test")
        s.add_message("user", "hello")
        s.add_boundary()
        history = s.get_history(max_messages=50)
        assert len(history) == 0

    def test_preserves_safe_message_metadata_for_prompt_context(self):
        s = Session(key="test")
        s.add_message(
            "user",
            "Wat fürn Deckel",
            sender_id="4917632625469",
            sender_name="Frank Taeger",
            message_id="3BCC5AB37B9E4F343C7E",
            reply_to_message_id="3EB019A2845309C24863EF",
            reply_to_participant="203075365150770@lid",
            internal_debug="do not expose",
        )

        history = s.get_history(max_messages=50)

        assert history == [
            {
                "role": "user",
                "content": "Wat fürn Deckel",
                "timestamp": history[0]["timestamp"],
                "sender_id": "4917632625469",
                "sender_name": "Frank Taeger",
                "message_id": "3BCC5AB37B9E4F343C7E",
                "reply_to_message_id": "3EB019A2845309C24863EF",
                "reply_to_participant": "203075365150770@lid",
            }
        ]
        assert "internal_debug" not in history[0]


class TestPreflightHeuristic:
    """Preflight heuristic should detect backward references in messages."""

    def test_detects_earlier_reference(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert _has_backward_reference("as we discussed earlier, the plan was...")
        assert _has_backward_reference("you mentioned something about auth")
        assert _has_backward_reference("remember when we talked about the API?")
        assert _has_backward_reference("go back to what you said about config")
        assert _has_backward_reference("we discussed that last time")

    def test_ignores_normal_messages(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert not _has_backward_reference("hello")
        assert not _has_backward_reference("what's the weather?")
        assert not _has_backward_reference("please write a function that adds two numbers")
        assert not _has_backward_reference("can you help me?")


class TestConversationStateContext:
    """Conversation state should be visible to the responder prompt."""

    def test_system_prompt_separates_repair_questions_from_engagement_bait(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        prompt = ContextBuilder(tmp_path).build_system_prompt()

        assert "# Conversational Repair" in prompt
        assert "ask one short clarification question" in prompt
        assert "Do not ask questions to keep the conversation open" in prompt
        assert "Do not end an otherwise complete answer with a question" in prompt

    def test_system_prompt_allows_social_calibration_without_extra_engagement(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        prompt = ContextBuilder(tmp_path).build_system_prompt()

        assert "# Social Calibration" in prompt
        assert "brief affiliative marker" in prompt
        assert "set one blunt boundary" in prompt
        assert "Do not add a follow-up question" in prompt

    def test_system_prompt_routes_market_questions_to_market_tools(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        prompt = ContextBuilder(tmp_path).build_system_prompt()

        assert "# Market Data" in prompt
        assert "market_intelligence" in prompt
        assert "market_quote" in prompt
        assert "web_search" in prompt
        assert "do not infer prices from web_search" in prompt

    def test_system_prompt_discourages_self_justification_loops(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        prompt = ContextBuilder(tmp_path).build_system_prompt()

        assert "# Epistemic Posture" in prompt
        assert "Make claims only when you have enough grounding to defend them" in prompt
        assert "Do not perform self-critique, self-diagnosis, or self-abasement" in prompt
        assert "If you are not grounded enough to defend the claim, do not make it" in prompt

    def test_system_prompt_routes_cross_chat_media_to_media_history(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        prompt = ContextBuilder(tmp_path).build_system_prompt()

        assert "use `media_history`" in prompt
        assert "previously shared images, screenshots, PDFs, or documents" in prompt

    def test_group_prompt_hides_dm_only_cross_chat_capabilities(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[],
            current_message="Und in der Ente?",
            current_metadata={"is_owner": True, "sender_id": "owner@s.whatsapp.net"},
            channel="whatsapp",
            chat_id="491786127564-1611913127@g.us",
        )

        system_prompt = str(messages[0]["content"]).lower()

        assert "owner dm" not in system_prompt
        assert "another group" not in system_prompt
        assert "cross-chat" not in system_prompt
        assert "other chats" not in system_prompt

    def test_owner_dm_prompt_keeps_cross_chat_history_capability(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[],
            current_message="Was ging heute in der Ente?",
            current_metadata={"is_owner": True, "sender_id": "owner@s.whatsapp.net"},
            channel="whatsapp",
            chat_id="491757070305@s.whatsapp.net",
        )

        system_prompt = str(messages[0]["content"])

        assert "owner DM" in system_prompt
        assert "summarize_history" in system_prompt
        assert "media_history" in system_prompt

    def test_current_message_includes_repair_guidance(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[],
            current_message="keine gute antwort Arvid",
            current_metadata={
                "sender_id": "user@s.whatsapp.net",
                "conversation_state": {
                    "addressed_to_bot": True,
                    "address_mode": "repair_feedback",
                    "preferred_action": "answer",
                    "answer_shape": "repair",
                    "room_mode": "direct_thread",
                },
            },
            channel="whatsapp",
            chat_id="group@g.us",
        )

        user_text = str(messages[-1]["content"])
        assert "[Conversation State]" in user_text
        assert "address_mode: repair_feedback" in user_text
        assert "preferred_action: answer" in user_text
        assert "answer_shape: repair" in user_text
        assert "Acknowledge the problem briefly, then give the corrected answer." in user_text
        assert "Do not stop after only apologizing" in user_text

    def test_recent_group_messages_override_stale_session_for_vague_references(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[
                {
                    "role": "assistant",
                    "content": "Timo's 40%-Take-Profit-Logik ist bei dir jetzt durch.",
                },
            ],
            current_message="@203075365150770 vorteile oder Nachteile der Strategie?",
            current_metadata={
                "sender_id": "491757070305",
                "sender_name": "D.",
                "ambient_context_window": [
                    "[Genti Halilaj] Wenn ich in Rente gehen will einfach irgend einen in Minecraft erschießen",
                    "[Genti Halilaj] Dann 15 Jahre lang bezahltes wohnen",
                    "[D.] Hm. Gar nicht so schlecht die Idee 🤓 am Besten in der Schweiz oder Norwegen oder so",
                ],
            },
            channel="whatsapp",
            chat_id="491786127564-1611913127@g.us",
        )

        user_text = str(messages[-1]["content"])

        assert "[Recent Messages]" in user_text
        assert "fresh_recent_messages_take_precedence=true" in user_text
        assert "If current and older session context point to different topics" in user_text
        assert "do not answer from older session history" in user_text
        assert "bezahlt" in user_text or "bezahltes" in user_text

    def test_external_history_preserves_original_sender_context(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[
                {
                    "role": "user",
                    "content": "Ich rede einfach weiter mit dir",
                    "timestamp": "2026-06-09T22:04:21+02:00",
                    "sender_id": "4917632625469",
                    "sender_name": "Frank Taeger",
                    "message_id": "m1",
                },
                {
                    "role": "assistant",
                    "content": "Der hat ja auch keinen Deckel mehr draufgekriegt.",
                    "timestamp": "2026-06-09T22:53:25+02:00",
                },
                {
                    "role": "user",
                    "content": "Wat fürn Deckel",
                    "timestamp": "2026-06-09T23:04:45+02:00",
                    "sender_id": "4917632625469",
                    "sender_name": "Frank Taeger",
                    "message_id": "m2",
                    "reply_to_message_id": "bot1",
                    "reply_to_participant": "203075365150770@lid",
                },
            ],
            current_message="Der Typ bin ich",
            current_metadata={"sender_id": "4917632625469"},
            channel="whatsapp",
            chat_id="group@g.us",
        )

        history_texts = [str(message["content"]) for message in messages if message["role"] == "user"]

        assert "sender=history" not in "\n".join(history_texts)
        assert "sender_name=Frank Taeger" in history_texts[0]
        assert "sender_id=4917632625469" in history_texts[0]
        assert "at=2026-06-09T22:04:21+02:00" in history_texts[0]
        assert "message_id=m1" in history_texts[0]
        assert "sender_name=Frank Taeger" in history_texts[1]
        assert "reply_to_message_id=bot1" in history_texts[1]
        assert "reply_to_participant=203075365150770@lid" in history_texts[1]

    def test_external_current_message_includes_owner_runtime_context(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[],
            current_message="Schick eine Sprachnachricht in die Ente",
            current_metadata={
                "sender_id": "491757070305",
                "sender_name": "D.",
                "is_owner": True,
            },
            channel="whatsapp",
            chat_id="491786127564-1611913127@g.us",
        )

        current_text = str(messages[-1]["content"])

        assert "sender_name=D." in current_text
        assert "sender_id=491757070305" in current_text
        assert "runtime_is_owner=true" in current_text

    def test_current_message_includes_social_one_liner_guidance(self, tmp_path):
        from yeoman_gateway.agent.context import ContextBuilder

        messages = ContextBuilder(tmp_path).build_messages(
            history=[],
            current_message=(
                "[Image] @203075365150770\n"
                "[image_description] This image is a screenshot of a social media post. "
                'The text reads, "Andrej Karpathy is the Sydney Sweeney of AI."'
            ),
            current_metadata={
                "sender_id": "user@s.whatsapp.net",
                "conversation_state": {
                    "addressed_to_bot": True,
                    "address_mode": "explicit_social_mention",
                    "preferred_action": "answer",
                    "answer_shape": "social_one_liner",
                    "room_mode": "direct_thread",
                },
            },
            channel="whatsapp",
            chat_id="group@g.us",
        )

        user_text = str(messages[-1]["content"])
        assert "answer_shape: social_one_liner" in user_text
        assert "Treat this as a social beat, not a request for analysis." in user_text
        assert "Do not explain the premise" in user_text


class TestDeliveryRepairGate:
    """Delivery tools should repair ambiguous targets instead of guessing."""

    @pytest.mark.asyncio
    async def test_message_tool_rejects_unresolved_whatsapp_chat_id(self):
        from yeoman_gateway.agent.tools.message import MessageTool
        from yeoman_gateway.bus.events import OutboundMessage

        sent: list[OutboundMessage] = []

        async def _send(message: OutboundMessage) -> None:
            sent.append(message)

        tool = MessageTool(
            send_callback=_send,
            default_channel="whatsapp",
            default_chat_id="491234567890-123456789@g.us",
        )

        result = await tool.execute(content="kommst du?", chat_id="Martin")

        assert result.startswith("Error: Cannot resolve WhatsApp target")
        assert "Ask which WhatsApp chat/contact to use" in result
        assert sent == []

    @pytest.mark.asyncio
    async def test_send_voice_tool_rejects_unresolved_whatsapp_chat_id(self):
        from yeoman_gateway.agent.tools.send_voice import SendVoiceTool, VoiceSendRequest

        sent: list[VoiceSendRequest] = []

        async def _send(request: VoiceSendRequest) -> str:
            sent.append(request)
            return "Voice message delivered."

        tool = SendVoiceTool(
            send_callback=_send,
            default_channel="whatsapp",
            default_chat_id="491234567890-123456789@g.us",
        )

        result = await tool.execute(content="kommst du?", chat_id="Martin")

        assert result.startswith("Error: Cannot resolve WhatsApp target")
        assert "Ask which WhatsApp chat/contact to use" in result
        assert sent == []

    @pytest.mark.asyncio
    async def test_send_voice_tool_still_allows_current_whatsapp_context(self):
        from yeoman_gateway.agent.tools.send_voice import SendVoiceTool, VoiceSendRequest

        sent: list[VoiceSendRequest] = []

        async def _send(request: VoiceSendRequest) -> str:
            sent.append(request)
            return "Voice message delivered."

        tool = SendVoiceTool(
            send_callback=_send,
            default_channel="whatsapp",
            default_chat_id="491234567890-123456789@g.us",
        )

        result = await tool.execute(content="kommst du?")

        assert result == "Voice message delivered."
        assert sent[0].chat_id == "491234567890-123456789@g.us"

    @pytest.mark.asyncio
    async def test_responder_persists_user_sender_metadata_for_later_context(self, tmp_path):
        from typing import Any

        from yeoman_gateway.adapters.responder_llm import LLMResponder
        from yeoman_gateway.bus.queue import MessageBus
        from yeoman_gateway.core.models import PolicyDecision
        from yeoman_gateway.providers.base import LLMProvider, LLMResponse

        class _Provider(LLMProvider):
            async def chat(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                model: str | None = None,
                max_tokens: int = 4096,
                temperature: float = 0.7,
                reasoning: dict[str, Any] | None = None,
            ) -> LLMResponse:
                del messages, tools, model, max_tokens, temperature, reasoning
                return LLMResponse(content="verstanden")

            def get_default_model(self) -> str:
                return "dummy/model"

        responder = LLMResponder(bus=MessageBus(), provider=_Provider(), workspace=tmp_path)

        out = await responder.generate_reply(
            InboundEvent(
                channel="whatsapp",
                chat_id="group@g.us",
                sender_id="4915253696948",
                content="Ich BIN Carschten",
                message_id="carschten-1",
                is_group=True,
                reply_to_bot=True,
                reply_to_message_id="bot-1",
                reply_to_participant="203075365150770@lid",
                reply_to_text="Carschten, 50+ oder nicht...",
                raw_metadata={"sender_name": "Carschten"},
            ),
            PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset(),
                reason="test",
            ),
        )

        history = responder.sessions.get_or_create("whatsapp:group@g.us").get_history()
        await responder.aclose()

        assert out == "verstanden"
        assert history[0]["sender_id"] == "4915253696948"
        assert history[0]["sender_name"] == "Carschten"
        assert history[0]["message_id"] == "carschten-1"
        assert history[0]["reply_to_message_id"] == "bot-1"
        assert history[0]["reply_to_participant"] == "203075365150770@lid"
        assert history[0]["reply_to_text"] == "Carschten, 50+ oder nicht..."

    @pytest.mark.asyncio
    async def test_side_effecting_tool_without_explicit_target_gets_repair_result(
        self,
        tmp_path,
    ):
        from typing import Any

        from yeoman_gateway.adapters.responder_llm import LLMResponder
        from yeoman_gateway.bus.queue import MessageBus
        from yeoman_gateway.core.models import PolicyDecision
        from yeoman_gateway.providers.base import LLMProvider, LLMResponse, ToolCallRequest

        class _VoiceToolWithoutTargetProvider(LLMProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.second_messages: list[dict[str, Any]] = []

            async def chat(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                model: str | None = None,
                max_tokens: int = 4096,
                temperature: float = 0.7,
                reasoning: dict[str, Any] | None = None,
            ) -> LLMResponse:
                del tools, model, max_tokens, temperature, reasoning
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        content=None,
                        tool_calls=[
                            ToolCallRequest(
                                id="call_voice_1",
                                name="send_voice",
                                arguments={"content": "Kommst du?"},
                            )
                        ],
                    )
                self.second_messages = messages
                return LLMResponse(content="Welchen Martin meinst du?")

            def get_default_model(self) -> str:
                return "dummy/model"

        provider = _VoiceToolWithoutTargetProvider()
        responder = LLMResponder(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            max_iterations=2,
        )

        out = await responder.generate_reply(
            InboundEvent(
                channel="whatsapp",
                chat_id="491234567890-123456789@g.us",
                sender_id="user@s.whatsapp.net",
                content="Arvid schick eine Sprachnachricht an Martin: Kommst du?",
                is_group=True,
                mentioned_bot=True,
            ),
            PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset({"send_voice"}),
                reason="test",
            ),
        )

        await responder.aclose()

        assert out == "Welchen Martin meinst du?"
        assert provider.calls == 2
        assert provider.second_messages[-1]["role"] == "tool"
        assert provider.second_messages[-1]["name"] == "send_voice"
        assert "Repair required" in provider.second_messages[-1]["content"]
        assert "Do not default to the current chat" in provider.second_messages[-1]["content"]


class TestRecallConversationTool:
    """recall_conversation tool should search session history."""

    @pytest.fixture
    def session_manager(self, tmp_path):
        return SessionManager(workspace=tmp_path, sessions_dir=tmp_path / "sessions")

    @pytest.fixture
    def tool(self, session_manager):
        t = RecallConversationTool(session_manager=session_manager)
        t.set_context("whatsapp", "owner@s.whatsapp.net")
        return t

    @pytest.mark.asyncio
    async def test_finds_matching_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "let's use PostgreSQL for the database")
        session.add_message("assistant", "sure, PostgreSQL is a good choice")
        session.add_message("user", "what about Redis for caching?")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result
        assert "Redis" not in result

    @pytest.mark.asyncio
    async def test_returns_no_matches(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "hello world")
        session_manager.save(session)

        result = await tool.execute(query="kubernetes")
        assert "No matching" in result or "no match" in result.lower()

    @pytest.mark.asyncio
    async def test_searches_across_boundaries(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "use PostgreSQL")
        session.add_boundary()
        session.add_message("user", "hello")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_respects_max_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        for i in range(50):
            session.add_message("user", f"message about topic {i}")
        session_manager.save(session)

        result = await tool.execute(query="topic", max_messages=5)
        lines = [line for line in result.strip().split("\n") if line.strip() and not line.startswith("Found")]
        assert len(lines) <= 5


class TestHistoryLimitResolution:
    """Test the full resolution chain: per-chat policy > heuristic > global default."""

    def test_config_defaults(self):
        from yeoman_shared.config.schema import WhatsAppConfig

        c = WhatsAppConfig()
        assert c.session_history_limit == 15
        assert c.session_history_limit_group == 20

    def test_policy_decision_carries_limit(self):
        from yeoman_gateway.core.models import PolicyDecision

        d = PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="ok",
            session_history_limit=30,
        )
        assert d.session_history_limit == 30

    def test_policy_decision_defaults_to_none(self):
        from yeoman_gateway.core.models import PolicyDecision

        d = PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="ok",
        )
        assert d.session_history_limit is None

    def test_resolve_history_limit_heuristic(self):
        from yeoman_gateway.adapters.responder_llm import LLMResponder

        r = LLMResponder.__new__(LLMResponder)
        r._session_history_limit = 15
        r._session_history_limit_group = 20
        # Heuristic expands DM from 15 → 45
        assert r._resolve_history_limit("user@s.whatsapp.net", None, "as we discussed") == 45
        # Normal DM stays 15
        assert r._resolve_history_limit("user@s.whatsapp.net", None, "hello") == 15
        # Group expands from 20 → 50 (capped)
        assert r._resolve_history_limit("group@g.us", None, "you mentioned") == 50
        # Explicit policy override is NOT expanded by heuristic
        assert r._resolve_history_limit("user@s.whatsapp.net", 5, "as we discussed") == 5

    def test_session_boundary_and_max_messages_combined(self):
        s = Session(key="test")
        for i in range(30):
            s.add_message("user", f"old msg {i}")
        s.add_boundary()
        for i in range(5):
            s.add_message("user", f"new msg {i}")

        # With limit=50, boundary wins (5 messages)
        assert len(s.get_history(max_messages=50)) == 5
        # With limit=3, max_messages wins (3 messages)
        assert len(s.get_history(max_messages=3)) == 3

    def test_boundary_persists_through_save_load(self, tmp_path):
        """Boundary markers survive JSONL save/load cycle."""
        mgr = SessionManager(workspace=tmp_path, sessions_dir=tmp_path / "sessions")
        session = mgr.get_or_create("test:boundary-persist")
        session.add_message("user", "old message")
        session.add_boundary()
        session.add_message("user", "new message")
        mgr.save(session)

        # Clear cache and reload
        mgr._cache.clear()
        reloaded = mgr.get_or_create("test:boundary-persist")
        history = reloaded.get_history(max_messages=50)
        assert len(history) == 1
        assert history[0]["content"] == "new message"
