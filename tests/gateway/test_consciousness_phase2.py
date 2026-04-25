"""Tests for Phase 2 group preview approval flow."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.approval import PendingSpeakupApproval, SpeakupApprovalStore
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.speakup_approval import SpeakupApprovalMiddleware
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import Config, ConsciousnessConfig


class _FakeSecurity:
    def check_output(self, text: str, context: dict[str, object] | None = None):
        del text, context
        from yeoman_gateway.core.models import SecurityDecision, SecurityResult

        return SecurityResult(
            stage="output",
            decision=SecurityDecision(action="allow", reason="ok"),
        )


class _FakeMemory:
    def search(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


def _config(**overrides: object) -> Config:
    payload = {
        "enabled": True,
        "ownerDmDefaultEnabled": False,
        "defaultDailyCap": 1,
        "approvalTimeoutSeconds": 3600,
        "maxSpeakupLengthChars": 200,
    }
    payload.update(overrides)
    return Config(consciousness=ConsciousnessConfig.model_validate(payload))


def _policy(*, opt_in_group: bool, preview: str | None = None) -> PolicyConfig:
    group_spontaneity: dict[str, object] = {}
    if opt_in_group:
        group_spontaneity = {"enabled": True, "profile": "balanced"}
        if preview is not None:
            group_spontaneity["preview"] = preview
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "owner@s.whatsapp.net": {
                            "spontaneity": {"enabled": True, "profile": "helpful"},
                        },
                        "group@g.us": {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "all"},
                            **(
                                {"spontaneity": group_spontaneity}
                                if group_spontaneity
                                else {}
                            ),
                        },
                    }
                }
            },
        }
    )


def _tools(
    tmp_path: Path,
    *,
    opt_in_group: bool,
    preview: str | None = None,
    config: Config | None = None,
) -> tuple[ConsciousnessTools, SpeakupApprovalStore, SpeakupLog]:
    fixed_now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    approval_store = SpeakupApprovalStore(
        tmp_path / "speakup-approvals.json",
        now=lambda: fixed_now.timestamp(),
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    tools = ConsciousnessTools(
        config=config or _config(),
        policy_engine=PolicyEngine(_policy(opt_in_group=opt_in_group, preview=preview), workspace=tmp_path),
        bus=MessageBus(),
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=_FakeMemory(),
        security=_FakeSecurity(),
        approval_store=approval_store,
        now=lambda: fixed_now,
    )
    tools.begin_run(trigger="cron")
    return tools, approval_store, log


def _owner_ctx(content: str) -> PipelineContext:
    ctx = PipelineContext(
        event=InboundEvent(
            channel="whatsapp",
            sender_id="owner@s.whatsapp.net",
            chat_id="owner@s.whatsapp.net",
            content=content,
            timestamp=datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
        )
    )
    ctx.decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        is_owner=True,
    )
    return ctx


@pytest.mark.asyncio
async def test_group_opt_in_is_required(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path, opt_in_group=False)

    proposal = await tools.propose_speakup(
        chat_id="group@g.us",
        message="hello group",
        action_type="observation",
        confidence=0.9,
    )

    assert proposal["status"] == "rejected"
    assert proposal["reason"] == "chat_not_eligible"


@pytest.mark.asyncio
async def test_group_preview_queues_owner_approval_instead_of_sending_directly(tmp_path: Path) -> None:
    tools, store, _ = _tools(tmp_path, opt_in_group=True)
    proposal = await tools.propose_speakup(
        chat_id="group@g.us",
        message="hello group",
        action_type="observation",
        confidence=0.9,
    )

    result = await tools.commit_speakup(str(proposal["proposal_id"]))
    preview = await tools.bus.consume_outbound()
    pending = await store.list_pending()

    assert result["status"] == "queued_for_approval"
    assert preview.chat_id == "owner@s.whatsapp.net"
    assert "spk-approve-" in preview.content
    assert "spk-deny-" in preview.content
    assert len(pending) == 1
    assert pending[0].target_chat_id == "group@g.us"


@pytest.mark.asyncio
async def test_chat_window_uses_most_recent_messages(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path, opt_in_group=True)
    archive = tools.inbound_archive
    for index, (timestamp, text) in enumerate(
        [
            (1776581816, "old-1"),
            (1776581817, "old-2"),
            (1776581818, "old-3"),
            (1777100000, "recent-1"),
            (1777100001, "recent-2"),
            (1777100002, "recent-3"),
        ]
    ):
        archive.record_inbound(
            channel="whatsapp",
            chat_id="group@g.us",
            message_id=f"msg-{index}",
            participant="user@s.whatsapp.net",
            sender_id="user@s.whatsapp.net",
            text=text,
            timestamp=timestamp,
        )

    window = await tools.read_chat_window("group@g.us", n=3)

    assert [message["text"] for message in window["messages"]] == [
        "recent-1",
        "recent-2",
        "recent-3",
    ]


def test_whatsapp_owner_phone_normalizes_to_dm_jid() -> None:
    assert (
        ConsciousnessTools._owner_dm_chat_id("whatsapp", "+491757070305")
        == "491757070305@s.whatsapp.net"
    )


@pytest.mark.asyncio
async def test_approve_code_sends_to_target_chat(tmp_path: Path) -> None:
    tools, store, log = _tools(tmp_path, opt_in_group=True)
    proposal = await tools.propose_speakup(
        chat_id="group@g.us",
        message="hello group",
        action_type="observation",
        confidence=0.9,
    )
    await tools.commit_speakup(str(proposal["proposal_id"]))
    preview = await tools.bus.consume_outbound()
    code = preview.content.split("Approve: ", 1)[1].splitlines()[0].strip()

    middleware = SpeakupApprovalMiddleware(
        approval_store=store,
        bus=tools.bus,
        log=log,
        security=tools.security,
    )
    next_fn = AsyncMock()
    await middleware(_owner_ctx(code), next_fn)
    outbound = await tools.bus.consume_outbound()

    assert outbound.chat_id == "group@g.us"
    assert outbound.metadata["spontaneous"] is True
    assert outbound.metadata["approved"] is True
    assert await log.count_sent_today(
        channel="whatsapp",
        chat_id="group@g.us",
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
    ) == 1
    assert next_fn.await_count == 0


@pytest.mark.asyncio
async def test_deny_code_prevents_send(tmp_path: Path) -> None:
    tools, store, log = _tools(tmp_path, opt_in_group=True)
    proposal = await tools.propose_speakup(
        chat_id="group@g.us",
        message="hello group",
        action_type="observation",
        confidence=0.9,
    )
    await tools.commit_speakup(str(proposal["proposal_id"]))
    preview = await tools.bus.consume_outbound()
    code = preview.content.split("Deny: ", 1)[1].splitlines()[0].strip()

    middleware = SpeakupApprovalMiddleware(
        approval_store=store,
        bus=tools.bus,
        log=log,
        security=tools.security,
    )
    next_fn = AsyncMock()
    await middleware(_owner_ctx(code), next_fn)

    assert tools.bus.outbound_size == 0
    history = await log.history("whatsapp", "group@g.us", limit=5)
    assert history[0]["status"] == "denied"
    assert next_fn.await_count == 0


@pytest.mark.asyncio
async def test_expired_approval_does_not_send_and_does_not_consume_daily_cap(tmp_path: Path) -> None:
    tools, store, log = _tools(tmp_path, opt_in_group=True)
    fixed_ts = datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp()
    proposal_id = await log.record_proposed(
        proposal_id="expired-proposal",
        channel="whatsapp",
        chat_id="group@g.us",
        action_type="observation",
        profile="balanced",
        message="expired",
        trigger="cron",
        context_snapshot={},
        now=fixed_ts,
    )
    await log.mark_status(proposal_id, status="queued_for_approval")
    await store.add(
        PendingSpeakupApproval(
            proposal_id=proposal_id,
            target_channel="whatsapp",
            target_chat_id="group@g.us",
            owner_channel="whatsapp",
            owner_chat_id="owner@s.whatsapp.net",
            message="expired",
            action_type="observation",
            profile="balanced",
            created_at=fixed_ts - 10,
            expires_at=fixed_ts - 1,
            context_snapshot={},
        )
    )

    middleware = SpeakupApprovalMiddleware(
        approval_store=store,
        bus=tools.bus,
        log=log,
        security=tools.security,
    )
    next_fn = AsyncMock()
    await middleware(_owner_ctx("spk-approve-expired-proposal"), next_fn)

    assert tools.bus.outbound_size == 0
    assert await log.count_sent_today(channel="whatsapp", chat_id="group@g.us", now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC)) == 0
    history = await log.history("whatsapp", "group@g.us", limit=5)
    assert history[0]["status"] == "expired"
