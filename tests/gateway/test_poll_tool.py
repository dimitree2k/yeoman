"""The poll tool: per-chat opt-in, target-free sending, validation and persistent limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.agent.tools.poll import (
    MAX_POLLS_PER_WINDOW,
    POLL_TTL_MS,
    PollGuard,
    PollTool,
    normalize_poll,
)
from yeoman_gateway.policy.engine import ActorContext, PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.tool_context import ToolInvocationContext, set_tool_context

GROUP = "123@g.us"
OWNER = "owner@s.whatsapp.net"
MEMBER = "member@s.whatsapp.net"


class _Sender:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def __call__(self, chat_id, question, options, max_selections, client_message_id):
        self.calls.append((chat_id, question, options, max_selections, client_message_id))
        if self.fail:
            raise RuntimeError("bridge down")
        return f"poll-{len(self.calls)}"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000_000_000

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def setup(tmp_path: Path):
    sender = _Sender()
    clock = _Clock()
    tool = PollTool(sender=lambda: sender, guard=PollGuard(tmp_path / "poll-guard.json"),
                    clock_ms=clock)
    return tool, sender, clock


def _context(chat_id: str = GROUP, *, is_owner: bool = False, channel: str = "whatsapp",
             turn: str = "turn-1") -> None:
    set_tool_context(ToolInvocationContext(
        channel=channel, chat_id=chat_id, is_owner=is_owner, turn_id=turn))


ASK = {"question": "Wann treffen wir uns?", "options": ["Freitag", "Samstag"]}


# --- policy gate -------------------------------------------------------------------


def _actor(chat_id: str) -> ActorContext:
    return ActorContext(
        channel="whatsapp",
        chat_id=chat_id,
        sender_primary=OWNER,
        sender_aliases=[],
        is_group=chat_id.endswith("@g.us"),
        mentioned_bot=True,
        reply_to_bot=False,
    )


def test_polls_are_off_unless_the_chat_opts_in(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": [OWNER]},
            "ownerTools": ["poll"],
            "defaults": {"allowedTools": {"mode": "all"}},
            "channels": {"whatsapp": {"chats": {GROUP: {"polls": True}}}},
        }
    )
    engine = PolicyEngine(policy, apply_channels={"whatsapp"}, workspace=tmp_path)
    tools = {"message", "poll"}

    enabled = engine.evaluate(_actor(GROUP), tools)
    disabled = engine.evaluate(_actor("456@g.us"), tools)

    assert "poll" in enabled.allowed_tools
    # Neither the default allowedTools "all" nor ownerTools switches polls on.
    assert "poll" not in disabled.allowed_tools
    assert "message" in disabled.allowed_tools
    assert engine.resolve_policy("whatsapp", GROUP).polls is True
    assert engine.resolve_policy("whatsapp", "456@g.us").polls is False


# --- validation --------------------------------------------------------------------


def test_normalize_poll_dedupes_and_trims() -> None:
    assert normalize_poll("  Pizza  oder Sushi? ", ["Pizza", " pizza ", "Sushi", ""]) == (
        "Pizza oder Sushi?", ["Pizza", "Sushi"])


@pytest.mark.parametrize("question,options,message", [
    ("", ["a", "b"], "needs a question"),
    ("q" * 256, ["a", "b"], "longer than 255"),
    ("q", ["a", "A"], "2 to 12"),
    ("q", [str(i) for i in range(13)], "2 to 12"),
    ("q", ["a", "b" * 101], "at most 100"),
    ("q", "a,b", "must be a list"),
])
def test_normalize_poll_rejects_bad_input(question, options, message) -> None:
    result = normalize_poll(question, options)
    assert isinstance(result, str) and message in result


# --- sending -----------------------------------------------------------------------


async def test_poll_goes_to_the_current_chat_only(setup) -> None:
    tool, sender, _ = setup
    _context()

    result = await tool.execute(action="create", **ASK)

    assert result.startswith("Poll sent")
    ((chat_id, question, options, max_selections, client_id),) = sender.calls
    assert (chat_id, question, options, max_selections) == (
        GROUP, ASK["question"], ASK["options"], 1)
    assert client_id.startswith("YPOLL") and 8 <= len(client_id) <= 128
    # No parameter can name another chat.
    assert set(tool.parameters["properties"]) == {"action", "question", "options", "multi_select"}


async def test_multi_select_allows_every_option(setup) -> None:
    tool, sender, _ = setup
    _context()
    await tool.execute(action="create", question="Welche Tage?",
                       options=["Mo", "Di", "Mi"], multi_select=True)
    assert sender.calls[0][3] == 3


@pytest.mark.parametrize("chat_id,is_owner,channel,allowed", [
    (GROUP, False, "whatsapp", True),
    (OWNER, True, "whatsapp", True),
    (MEMBER, False, "whatsapp", False),
    ("-100200", False, "telegram", False),
])
async def test_polls_need_a_group_or_the_owner_dm(setup, chat_id, is_owner, channel, allowed):
    tool, sender, _ = setup
    _context(chat_id, is_owner=is_owner, channel=channel)
    result = await tool.execute(action="create", **ASK)
    assert result.startswith("Poll sent") is allowed
    assert len(sender.calls) == int(allowed)


async def test_repeated_call_in_the_same_turn_is_not_sent_twice(setup) -> None:
    tool, sender, _ = setup
    _context()
    await tool.execute(action="create", **ASK)
    await tool.execute(action="close")

    again = await tool.execute(action="create", **ASK)

    assert "already sent" in again
    assert len(sender.calls) == 1


# --- limits ------------------------------------------------------------------------


async def test_one_open_poll_per_chat_until_closed(setup) -> None:
    tool, sender, _ = setup
    _context(turn="t1")
    await tool.execute(action="create", **ASK)
    _context(turn="t2")

    blocked = await tool.execute(action="create", question="Noch eine?", options=["ja", "nein"])
    assert "already open" in blocked
    # Another chat is not affected.
    _context("456@g.us", turn="t2")
    assert (await tool.execute(action="create", **ASK)).startswith("Poll sent")

    _context(turn="t3")
    assert "now closed" in await tool.execute(action="close")
    assert "no open poll" in await tool.execute(action="close")
    assert (await tool.execute(action="create", question="Noch eine?",
                               options=["ja", "nein"])).startswith("Poll sent")


async def test_at_most_three_polls_per_rolling_day(setup) -> None:
    tool, sender, clock = setup
    for turn in range(MAX_POLLS_PER_WINDOW):
        _context(turn=f"t{turn}")
        assert (await tool.execute(action="create", **ASK)).startswith("Poll sent")
        await tool.execute(action="close")
        clock.now += 60_000

    _context(turn="t-over")
    assert "3 polls in the last 24 hours" in await tool.execute(action="create", **ASK)

    clock.now += POLL_TTL_MS
    assert (await tool.execute(action="create", **ASK)).startswith("Poll sent")


async def test_open_poll_expires_after_a_day(setup) -> None:
    tool, _, clock = setup
    _context(turn="t1")
    await tool.execute(action="create", **ASK)
    clock.now += POLL_TTL_MS + 1
    _context(turn="t2")
    assert (await tool.execute(action="create", **ASK)).startswith("Poll sent")


async def test_failed_send_releases_the_reservation(tmp_path: Path) -> None:
    sender = _Sender(fail=True)
    tool = PollTool(sender=lambda: sender, guard=PollGuard(tmp_path / "g.json"), clock_ms=_Clock())
    _context()

    assert await tool.execute(action="create", **ASK) == "Error: the poll could not be sent."

    sender.fail = False
    assert (await tool.execute(action="create", **ASK)).startswith("Poll sent")


async def test_limits_survive_a_restart(tmp_path: Path) -> None:
    clock = _Clock()
    path = tmp_path / "ops" / "poll-guard.json"
    first = PollTool(sender=lambda: _Sender(), guard=PollGuard(path), clock_ms=clock)
    _context(turn="t1")
    await first.execute(action="create", **ASK)

    restarted = PollTool(sender=lambda: _Sender(), guard=PollGuard(path), clock_ms=clock)
    _context(turn="t2")
    assert "already open" in await restarted.execute(action="create", **ASK)


async def test_unavailable_transport_reserves_nothing(tmp_path: Path) -> None:
    path = tmp_path / "g.json"
    tool = PollTool(sender=lambda: None, guard=PollGuard(path), clock_ms=_Clock())
    _context()
    assert await tool.execute(action="create", **ASK) == "Error: polls are unavailable right now."
    assert not path.exists()


# --- transport ---------------------------------------------------------------------


async def test_whatsapp_send_poll_payload(tmp_path: Path) -> None:
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_shared.config.schema import WhatsAppConfig

    channel = WhatsAppChannel(WhatsAppConfig(media={"outgoing_dir": str(tmp_path)}), MessageBus())
    channel._connected = True
    calls: list[tuple[str, dict, dict]] = []

    async def send(command, payload, **kwargs):
        calls.append((command, payload, kwargs))
        return {"sent": {"messageId": "provider-poll-1"}}

    channel._send_command_with_retry = send  # type: ignore[method-assign]

    poll_id = await channel.send_poll(GROUP, "Q?", ["a", "b"], 1, "YPOLLABCDEF123")

    assert poll_id == "provider-poll-1"
    ((command, payload, kwargs),) = calls
    assert command == "send_poll"
    assert payload == {"to": GROUP, "question": "Q?", "options": ["a", "b"],
                       "maxSelections": 1, "clientMessageId": "YPOLLABCDEF123"}
    assert kwargs["max_attempts"] == 1


async def test_channel_manager_poll_respects_the_global_pause() -> None:
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    from yeoman_shared.config.schema import Config

    sent: list[str] = []

    class _FakeWhatsApp:
        async def send_poll(self, chat_id, question, options, max_selections, client_id):
            sent.append(chat_id)
            return "p1"

    manager = ChannelManager(Config(), MessageBus())
    manager.channels["whatsapp"] = _FakeWhatsApp()

    assert await manager.send_poll_now(GROUP, "Q?", ["a", "b"], 1, "YPOLLABCDEF123") == "p1"
    manager.set_global_pause_probe(lambda channel, chat_id: "owner_paused")
    with pytest.raises(Exception):
        await manager.send_poll_now(GROUP, "Q?", ["a", "b"], 1, "YPOLLABCDEF124")
    assert sent == [GROUP]
