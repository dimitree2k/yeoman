"""Tests for persona evolution proposal evidence."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.telegram import TelegramChannel
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.memory.service import MemoryService
from yeoman_gateway.persona_evolution import (
    PersonaEvolutionLedger,
    apply_persona_evolution_proposal,
    build_persona_evolution_approval_message,
    build_persona_evolution_status,
    chats_for_persona,
    collect_persona_evolution_evidence,
    deny_persona_evolution_proposal,
    persona_evolution_result_needs_notification,
    render_persona_evolution_proposal,
    run_persona_evolution_cron,
)
from yeoman_gateway.pipeline.persona_evolution_approval import (
    PersonaEvolutionApprovalMiddleware,
)
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import MemoryConfig, TelegramConfig


def _policy() -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "channels": {
                "whatsapp": {
                    "default": {"personaFile": "personas/professional.md"},
                    "chats": {
                        "group-a": {"personaFile": "personas/alpha-2.md"},
                        "group-b": {"personaFile": "personas/alpha-2.md"},
                        "group-c": {},
                    },
                }
            }
        }
    )


def _memory(tmp_path: Path) -> MemoryService:
    return MemoryService(
        workspace=tmp_path / "workspace",
        config=MemoryConfig(db_path=str(tmp_path / "memory.db")),
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas"
    persona_dir.mkdir(parents=True)
    (persona_dir / "alpha-2.md").write_text("# Alpha\n", encoding="utf-8")
    (persona_dir / "alpha-2.evolution.md").write_text(
        "# Evolution Layer: Alpha\n",
        encoding="utf-8",
    )
    return workspace


def _record_messages(
    archive: InboundArchive,
    *,
    chat_id: str = "group-a",
    day: int,
    count: int,
) -> None:
    for index in range(count):
        archive.record_inbound(
            channel="whatsapp",
            chat_id=chat_id,
            message_id=f"d{day}-m{index}",
            participant=f"user-{index % 3}@s.whatsapp.net",
            sender_id=f"user-{index % 3}@s.whatsapp.net",
            text=f"message {day}-{index}",
            timestamp=int(datetime(2026, 5, day, 12, index, tzinfo=UTC).timestamp()),
        )


def _owner_ctx(
    content: str,
    *,
    reply_to_bot: bool = False,
    reply_to_text: str | None = None,
    reply_to_message_id: str | None = None,
) -> PipelineContext:
    ctx = PipelineContext(
        event=InboundEvent(
            channel="telegram",
            chat_id="tg-owner",
            sender_id="tg-owner",
            content=content,
            reply_to_bot=reply_to_bot,
            reply_to_text=reply_to_text,
            reply_to_message_id=reply_to_message_id,
            timestamp=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
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


def _record_test_proposal(
    *,
    workspace: Path,
    state_db: Path,
    proposal_path: Path,
    proposal_id: str = "proposal-1",
    created_at: datetime = datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
) -> None:
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    proposal_path.write_text(
        "\n".join(
            [
                "# Persona Evolution Proposal",
                "",
                f"proposal_id: `{proposal_id}`",
                "persona_file: `personas/alpha-2.md`",
                f"evolution_path: `{evolution_path}`",
                "evidence_from: `2026-04-20T03:00:00+00:00`",
                "evidence_to: `2026-05-04T03:00:00+00:00`",
                "",
                "## Proposed Change",
                "",
                "- 2026-05-04 `whatsapp:group-a` confidence=medium evidence=12 speakups: Compact market numbers land better than broad proactive takes.",
                "",
                "## Current Evolution Digest",
                "",
                f"- hash: `{hashlib.sha256(evolution_path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()}`",
            ]
        ),
        encoding="utf-8",
    )
    ledger = PersonaEvolutionLedger(state_db)
    try:
        ledger.record_proposal(
            proposal_id=proposal_id,
            persona_file="personas/alpha-2.md",
            proposal_path=proposal_path,
            created_at=created_at,
            evidence_from=datetime(2026, 4, 20, 3, 0, tzinfo=UTC),
            evidence_to=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
            total_message_count=50,
            signal_score=47.5,
            base_hash=hashlib.sha256(
                evolution_path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
            persona_hash=hashlib.sha256(
                (workspace / "personas" / "alpha-2.md")
                .read_text(encoding="utf-8")
                .encode("utf-8")
            ).hexdigest(),
        )
    finally:
        ledger.close()


def test_chats_for_persona_uses_effective_policy_persona() -> None:
    refs = chats_for_persona(_policy(), "personas/alpha-2.md")

    assert [(ref.channel, ref.chat_id, ref.source) for ref in refs] == [
        ("whatsapp", "group-a", "chat"),
        ("whatsapp", "group-b", "chat"),
    ]


def test_persona_evolution_notification_detection_handles_auto_apply_prefix() -> None:
    assert persona_evolution_result_needs_notification(
        "persona_evolution proposal written: /tmp/proposal.md"
    )
    assert persona_evolution_result_needs_notification(
        "persona_evolution no proposal: pending_proposal proposal_id=abc123"
    )
    assert persona_evolution_result_needs_notification(
        "persona_evolution auto_applied ignored proposals: old123; "
        "persona_evolution proposal written: /tmp/proposal.md"
    )
    assert not persona_evolution_result_needs_notification(
        "persona_evolution auto_applied ignored proposals: old123; "
        "persona_evolution no proposal: no_durable_changes messages=50 score=167.50"
    )


@pytest.mark.asyncio
async def test_collect_persona_evolution_evidence_excludes_raw_messages(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    persona_dir = workspace / "personas"
    persona_dir.mkdir(parents=True)
    (persona_dir / "alpha-2.md").write_text("# Alpha\n", encoding="utf-8")
    (persona_dir / "alpha-2.evolution.md").write_text(
        "# Evolution Layer: Alpha\n",
        encoding="utf-8",
    )
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_sent(
        proposal_id="spk-1",
        channel="whatsapp",
        chat_id="group-a",
        action_type="share_opinion",
        profile="balanced",
        message="Raw speakup text should not appear in proposal.",
        trigger="cron",
        context_snapshot={},
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp(),
    )
    await log.mark_outcome("spk-1", outcome="replied")
    archive = InboundArchive(tmp_path / "reply_context.db")
    archive.record_inbound(
        channel="whatsapp",
        chat_id="group-a",
        message_id="m1",
        participant="user@s.whatsapp.net",
        sender_id="user@s.whatsapp.net",
        text="Private raw chat text must not appear.",
        timestamp=int(datetime(2026, 4, 25, 12, 5, tzinfo=UTC).timestamp()),
    )

    evidence = await collect_persona_evolution_evidence(
        policy=_policy(),
        workspace=workspace,
        persona_file="personas/alpha-2.md",
        memory=memory,
        speakup_log=log,
        inbound_archive=archive,
        now=datetime(2026, 4, 25, 13, 0, tzinfo=UTC),
    )
    rendered = render_persona_evolution_proposal(evidence)

    assert evidence.current_evolution_text == "# Evolution Layer: Alpha\n"
    assert evidence.chats[0].learned_taste == [
        "Proactive speakup taste pattern: compact market numbers land."
    ]
    assert evidence.chats[0].recent_message_count == 1
    assert "compact market numbers land" in rendered
    assert "Raw speakup text should not appear" not in rendered
    assert "Private raw chat text must not appear" not in rendered
    assert "no persona files were modified" in rendered

    memory.close()
    log.close()
    archive.close()


@pytest.mark.asyncio
async def test_render_persona_evolution_proposal_dedupes_review_noise(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_sent(
        proposal_id="spk-1",
        channel="whatsapp",
        chat_id="group-a",
        action_type="share_opinion",
        profile="balanced",
        message="Raw speakup text should not appear in proposal.",
        trigger="cron",
        context_snapshot={},
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp(),
    )
    await log.mark_outcome("spk-1", outcome="replied")
    archive = InboundArchive(tmp_path / "reply_context.db")
    archive.record_inbound(
        channel="whatsapp",
        chat_id="group-a",
        message_id="m1",
        participant="user@s.whatsapp.net",
        sender_id="user@s.whatsapp.net",
        text="Private raw chat text must not appear.",
        timestamp=int(datetime(2026, 4, 25, 12, 5, tzinfo=UTC).timestamp()),
    )

    try:
        evidence = await collect_persona_evolution_evidence(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            now=datetime(2026, 4, 25, 13, 0, tzinfo=UTC),
        )
        rendered = render_persona_evolution_proposal(evidence, proposal_id="abc123")
    finally:
        memory.close()
        log.close()
        archive.close()

    assert "## Proposed Change" in rendered
    assert "## Evidence Digest" in rendered
    assert "## Current Evolution Digest" in rendered
    assert "Current Evolution File" not in rendered
    assert "whatsapp:group-b" not in rendered
    assert rendered.count("compact market numbers land") == 1
    assert "Recent chat preferences:" not in rendered
    assert "Learned proactive taste:" not in rendered


@pytest.mark.asyncio
async def test_run_persona_evolution_cron_writes_proposal(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    output = workspace / "persona-evolution" / "proposal.md"
    _record_messages(archive, day=1, count=3)

    try:
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            window_days=1,
            limit=10,
            output_path=output,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert result == f"persona_evolution proposal written: {output}"
    rendered = output.read_text(encoding="utf-8")
    assert "Persona Evolution Proposal" in rendered
    assert "persona_file: `personas/alpha-2.md`" in rendered
    assert "Current Evolution Digest" in rendered


@pytest.mark.asyncio
async def test_run_persona_evolution_cron_skips_without_durable_change(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    output = workspace / "persona-evolution" / "proposal.md"
    _record_messages(archive, day=1, count=3)

    try:
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            window_days=1,
            limit=10,
            output_path=output,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert result == "persona_evolution no proposal: no_durable_changes messages=3 score=0.75"
    assert not output.exists()


@pytest.mark.asyncio
async def test_run_persona_evolution_cron_skips_redundant_durable_lesson(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "personas" / "alpha-2.evolution.md").write_text(
        "\n".join(
            [
                "# Evolution Layer: Alpha",
                "",
                "## Consciousness Outcome Lessons",
                "- 2026-05-07 `whatsapp:group-a` confidence=medium evidence=50 speakups, 50 messages: Proactive speakups favor data-dense messages quantifying extreme market moves, trading dilutions, contrarian fiscal projections, or light humor; silences routine corporate metrics, broad sector trends, or basic fact corrections.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: Engages most with data-dense observations and error corrections on trading, markets, and finance; resists contrarian views and opinion-heavy shares.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    output = workspace / "persona-evolution" / "proposal.md"
    _record_messages(archive, day=1, count=3)

    try:
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            window_days=1,
            limit=10,
            output_path=output,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert result == "persona_evolution no proposal: no_durable_changes messages=3 score=5.75"
    assert not output.exists()


@pytest.mark.asyncio
async def test_cron_accumulates_below_threshold_messages_until_proposal(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    state_db = tmp_path / "persona-evolution.db"
    output = workspace / "persona-evolution" / "proposal.md"

    try:
        _record_messages(archive, day=1, count=9)
        first = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=output,
            state_db_path=state_db,
            min_meaningful_messages=25,
            min_signal_score=0.0,
            max_accumulation_days=14,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
        assert first == "persona_evolution no proposal: below_threshold messages=9 score=2.25"
        assert not output.exists()

        _record_messages(archive, day=2, count=9)
        second = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=output,
            state_db_path=state_db,
            min_meaningful_messages=25,
            min_signal_score=0.0,
            max_accumulation_days=14,
            now=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        )
        assert second == "persona_evolution no proposal: below_threshold messages=18 score=4.50"
        assert not output.exists()

        memory.record_manual(
            channel="whatsapp",
            chat_id="group-a",
            sender_id=None,
            scope_type="chat",
            kind="preference",
            text="Proactive speakup taste pattern: compact market numbers land.",
            importance=0.75,
            confidence=0.88,
        )
        _record_messages(archive, day=3, count=9)
        third = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=output,
            state_db_path=state_db,
            min_meaningful_messages=25,
            min_signal_score=0.0,
            max_accumulation_days=14,
            now=datetime(2026, 5, 3, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert third == f"persona_evolution proposal written: {output}"
    rendered = output.read_text(encoding="utf-8")
    assert "total_message_count: `27`" in rendered


@pytest.mark.asyncio
async def test_auto_apply_mode_applies_ignored_expired_proposal(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="ignored123",
        created_at=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
    )
    memory = _memory(tmp_path)
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")

    try:
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            state_db_path=state_db,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            proposal_ttl_seconds=3600,
            proposal_mode="auto_apply",
            now=datetime(2026, 5, 4, 4, 0, 1, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    ledger = PersonaEvolutionLedger(state_db)
    try:
        proposal = ledger.get_proposal("ignored123")
    finally:
        ledger.close()

    assert result.startswith(
        "persona_evolution auto_applied ignored proposals: ignored123; "
    )
    assert "Compact market numbers land" in evolution_path.read_text(encoding="utf-8")
    assert proposal is not None
    assert proposal["status"] == "applied"
    assert proposal["final_outcome"] == "auto_approved"
    assert proposal["approval_channel"] == "auto_apply"
    assert proposal["approval_chat_id"] == "ignored_ttl"


@pytest.mark.asyncio
async def test_closed_proposal_watermark_prevents_reusing_old_messages(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    state_db = tmp_path / "persona-evolution.db"
    output = workspace / "persona-evolution" / "proposal.md"

    try:
        _record_messages(archive, day=1, count=3)
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=output,
            state_db_path=state_db,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            max_accumulation_days=14,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
        assert result == f"persona_evolution proposal written: {output}"

        ledger = PersonaEvolutionLedger(state_db)
        proposal = ledger.pending_proposal("personas/alpha-2.md")
        assert proposal is not None
        ledger.close_proposal(str(proposal["proposal_id"]), status="denied")
        ledger.close()
        output.unlink()

        _record_messages(archive, day=2, count=1)
        later = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=output,
            state_db_path=state_db,
            min_meaningful_messages=3,
            min_signal_score=0.0,
            max_accumulation_days=14,
            now=datetime(2026, 5, 2, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert later == "persona_evolution no proposal: below_threshold messages=1 score=5.25"
    assert not output.exists()


@pytest.mark.asyncio
async def test_run_persona_evolution_cron_rejects_output_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "reply_context.db")
    outside_output = tmp_path / "outside-proposal.md"
    _record_messages(archive, day=1, count=3)

    try:
        result = await run_persona_evolution_cron(
            policy=_policy(),
            workspace=workspace,
            persona_file="personas/alpha-2.md",
            memory=memory,
            speakup_log=log,
            inbound_archive=archive,
            limit=100,
            output_path=outside_output,
            min_meaningful_messages=1,
            min_signal_score=0.0,
            now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        )
    finally:
        memory.close()
        log.close()
        archive.close()

    assert result.startswith("persona_evolution no proposal: invalid_output_path")
    assert not outside_output.exists()


@pytest.mark.asyncio
async def test_build_persona_evolution_status_summarizes_chat_learning(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    memory = _memory(tmp_path)
    memory.record_manual(
        channel="whatsapp",
        chat_id="group-a",
        sender_id=None,
        scope_type="chat",
        kind="preference",
        text="Proactive speakup taste pattern: compact market numbers land.",
        importance=0.75,
        confidence=0.88,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_sent(
        proposal_id="spk-1",
        channel="whatsapp",
        chat_id="group-a",
        action_type="share_opinion",
        profile="balanced",
        message="short",
        trigger="cron",
        context_snapshot={},
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp(),
    )
    await log.mark_outcome("spk-1", outcome="replied")
    await log.record_taste_distillation(
        channel="whatsapp",
        chat_id="group-a",
        sample_fingerprint="fp-1",
        now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC).timestamp(),
    )
    state_db = tmp_path / "persona-evolution.db"

    try:
        status = await build_persona_evolution_status(
            policy=_policy(),
            workspace=workspace,
            memory=memory,
            speakup_log=log,
            state_db_path=state_db,
            persona_file="personas/alpha-2.md",
            channel="whatsapp",
            chat_id="group-a",
        )
    finally:
        memory.close()
        log.close()

    assert status["persona_file"] == "personas/alpha-2.md"
    assert status["pending_proposals"] == []
    assert status["metrics"] == {"proposals": {}, "scans": {}}
    assert status["chat"]["sent_speakups"] == 1
    assert status["chat"]["labeled_outcomes"] == 1
    assert status["chat"]["taste_distillations"] == 1
    assert status["chat"]["last_learned_taste"].endswith("compact market numbers land.")


def test_approval_message_is_compact_for_telegram_review(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    ledger = PersonaEvolutionLedger(state_db)
    try:
        proposal = ledger.get_proposal("abc123")
    finally:
        ledger.close()

    assert proposal is not None
    message = build_persona_evolution_approval_message(proposal)

    assert message == "\n".join(
        [
            "Persona evolution proposal for personas/alpha-2.md",
            "Proposed change:",
            "- 2026-05-04 `whatsapp:group-a` confidence=medium evidence=12 speakups: Compact market numbers land better than broad proactive takes.",
            "Evidence window: 2026-04-20T03:00:00+00:00 -> 2026-05-04T03:00:00+00:00",
        ]
    )
    assert str(proposal_path) not in message
    assert "pe-approve-abc123" not in message
    assert "pe-deny-abc123" not in message


def test_telegram_metadata_includes_reply_target_text() -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    channel._bot_id = 42
    message = SimpleNamespace(
        text="yes",
        caption=None,
        entities=None,
        caption_entities=None,
        reply_to_message=SimpleNamespace(
            message_id=99,
            text="Persona evolution proposal\npe-approve-abc123\npe-deny-abc123",
            caption=None,
            from_user=SimpleNamespace(id=42, is_bot=True),
        ),
    )

    metadata = channel._mention_metadata(message)

    assert metadata["reply_to_bot"] is True
    assert metadata["reply_to_message_id"] == "99"
    assert metadata["reply_to_text"] == "Persona evolution proposal\npe-approve-abc123\npe-deny-abc123"


def test_apply_persona_evolution_proposal_updates_evolution_file_and_metadata(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    evolution_path.write_text(
        "\n".join(
            [
                "# Evolution Layer: Alpha",
                "<!-- Last consolidated: 2026-03-21 -->",
                "<!-- Consolidation count: 0 (seed file — no consolidation has run yet) -->",
                "",
                "## Trait Drift",
                "Verbosity: low-moderate (stable)",
                "",
                "## Consolidation Changelog",
                "2026-03-21 — seed file created, no consolidation has run yet.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="abc123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    updated = evolution_path.read_text(encoding="utf-8")
    ledger = PersonaEvolutionLedger(state_db)
    try:
        proposal = ledger.get_proposal("abc123")
    finally:
        ledger.close()

    assert result.status == "applied"
    assert "<!-- Last consolidated: 2026-05-04 -->" in updated
    assert "<!-- Consolidation count: 1 -->" in updated
    assert "## Consciousness Outcome Lessons" in updated
    assert "Compact market numbers land better than broad proactive takes." in updated
    assert "Consider adding a Consciousness Outcome Lesson" not in updated
    assert "2026-05-04 — consolidation #1: applied proposal abc123" in updated
    assert proposal is not None
    assert proposal["status"] == "applied"
    assert proposal["approval_channel"] == "telegram"
    assert proposal["approval_chat_id"] == "tg-owner"
    assert proposal["applied_hash"]


def test_apply_persona_evolution_proposal_blocks_if_base_persona_changed(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    persona_path = workspace / "personas" / "alpha-2.md"
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    persona_path.write_text("# Alpha\n\n## Invariants\n- new invariant\n", encoding="utf-8")

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="abc123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert "base persona file changed" in result.message
    assert evolution_path.read_text(encoding="utf-8") == original


def test_apply_persona_evolution_proposal_blocks_expired_proposal(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
        created_at=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
    )

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="abc123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 5, 3, 0, 1, tzinfo=UTC),
    )

    ledger = PersonaEvolutionLedger(state_db)
    try:
        proposal = ledger.get_proposal("abc123")
    finally:
        ledger.close()

    assert result.status == "expired"
    assert evolution_path.read_text(encoding="utf-8") == original
    assert proposal is not None
    assert proposal["status"] == "expired"
    assert proposal["final_outcome"] == "expired"


def test_apply_persona_evolution_proposal_rejects_unknown_evolution_section(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    evolution_path.write_text(
        "\n".join(
            [
                "# Evolution Layer: Alpha",
                "",
                "## Trait Drift",
                "Verbosity: low-moderate (stable)",
                "",
                "## Random New Identity",
                "- should not be accepted",
                "",
                "## Consolidation Changelog",
                "2026-03-21 - seed file created.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="abc123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert "unsupported evolution section" in result.message
    assert evolution_path.read_text(encoding="utf-8") == original


def test_apply_persona_evolution_proposal_rejects_note_without_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    proposal_path.write_text(
        "\n".join(
            [
                "# Persona Evolution Proposal",
                "",
                "## Proposed Change",
                "",
                "- 2026-05-04 `whatsapp:group-a` confidence=medium: Compact numbers land.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ledger = PersonaEvolutionLedger(state_db)
    try:
        ledger.record_proposal(
            proposal_id="abc123",
            persona_file="personas/alpha-2.md",
            proposal_path=proposal_path,
            created_at=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
            evidence_from=datetime(2026, 4, 20, 3, 0, tzinfo=UTC),
            evidence_to=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
            total_message_count=50,
            signal_score=47.5,
            base_hash=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            persona_hash=hashlib.sha256(
                (workspace / "personas" / "alpha-2.md")
                .read_text(encoding="utf-8")
                .encode("utf-8")
            ).hexdigest(),
        )
    finally:
        ledger.close()

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="abc123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    assert result.status == "blocked"
    assert result.message == "proposal note missing evidence count"
    assert evolution_path.read_text(encoding="utf-8") == original


def test_apply_legacy_persona_evolution_proposal_collapses_repeated_chat_notes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    evolution_path.write_text(
        "\n".join(
            [
                "# Evolution Layer: Alpha",
                "<!-- Last consolidated: 2026-03-21 -->",
                "<!-- Consolidation count: 0 -->",
                "",
                "## Consolidation Changelog",
                "2026-03-21 - seed file created.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "legacy-proposal.md"
    proposal_path.write_text(
        "\n".join(
            [
                "# Persona Evolution Proposal",
                "",
                "## Suggested Evolution Notes",
                "",
                "- 2026-05-05 `whatsapp:group-a`: Consider adding a Consciousness Outcome Lesson from 31 recent speakup records and learned taste: Proactive speakup taste pattern: compact market numbers land.",
                "- 2026-05-05 `whatsapp:group-a`: Consider adding a Consciousness Outcome Lesson from 31 recent speakup records and learned taste: Proactive speakup taste pattern: broad takes miss.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ledger = PersonaEvolutionLedger(state_db)
    try:
        ledger.record_proposal(
            proposal_id="legacy123",
            persona_file="personas/alpha-2.md",
            proposal_path=proposal_path,
            created_at=datetime(2026, 5, 5, 3, 0, tzinfo=UTC),
            evidence_from=datetime(2026, 5, 4, 3, 0, tzinfo=UTC),
            evidence_to=datetime(2026, 5, 5, 3, 0, tzinfo=UTC),
            total_message_count=38,
            signal_score=59.5,
            base_hash=hashlib.sha256(
                evolution_path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
            persona_hash=hashlib.sha256(
                (workspace / "personas" / "alpha-2.md")
                .read_text(encoding="utf-8")
                .encode("utf-8")
            ).hexdigest(),
        )
    finally:
        ledger.close()

    result = apply_persona_evolution_proposal(
        workspace=workspace,
        state_db_path=state_db,
        proposal_id="legacy123",
        approved_by_channel="telegram",
        approved_by_chat_id="tg-owner",
        now=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
    )

    updated = evolution_path.read_text(encoding="utf-8")
    assert result.status == "applied"
    assert updated.count("whatsapp:group-a") == 1
    assert "compact market numbers land." in updated
    assert "broad takes miss." not in updated
    assert "Consider adding a Consciousness Outcome Lesson" not in updated
    assert "Proactive speakup taste pattern:" not in updated


def test_deny_persona_evolution_proposal_closes_without_file_change(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )

    result = deny_persona_evolution_proposal(
        state_db_path=state_db,
        proposal_id="abc123",
        denied_by_channel="telegram",
        denied_by_chat_id="tg-owner",
        now=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )

    ledger = PersonaEvolutionLedger(state_db)
    try:
        proposal = ledger.get_proposal("abc123")
    finally:
        ledger.close()

    assert result.status == "denied"
    assert evolution_path.read_text(encoding="utf-8") == original
    assert proposal is not None
    assert proposal["status"] == "denied"
    assert proposal["approval_channel"] == "telegram"
    assert proposal["approval_chat_id"] == "tg-owner"


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_applies_telegram_code(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=workspace,
        state_db_path=state_db,
        bus=bus,
        now=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    next_fn = AsyncMock()

    await middleware(_owner_ctx("pe-approve-abc123"), next_fn)
    confirmation = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)

    assert next_fn.await_count == 0
    assert confirmation.channel == "telegram"
    assert confirmation.chat_id == "tg-owner"
    assert "applied" in confirmation.content
    assert "personas/alpha-2.md" in confirmation.content


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_applies_reply_yes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    approval_message = (
        "Persona evolution proposal for personas/alpha-2.md\n"
        "Approve: pe-approve-abc123\n"
        "Deny: pe-deny-abc123"
    )
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=workspace,
        state_db_path=state_db,
        bus=bus,
        now=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    next_fn = AsyncMock()

    await middleware(
        _owner_ctx(
            "yes",
            reply_to_bot=True,
            reply_to_text=approval_message,
            reply_to_message_id="tg-msg-1",
        ),
        next_fn,
    )
    confirmation = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)

    assert next_fn.await_count == 0
    assert confirmation.channel == "telegram"
    assert confirmation.chat_id == "tg-owner"
    assert "applied" in confirmation.content


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_applies_compact_reply_yes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    ledger = PersonaEvolutionLedger(state_db)
    try:
        ledger.mark_notified("abc123", channel="telegram", chat_id="tg-owner")
    finally:
        ledger.close()
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=workspace,
        state_db_path=state_db,
        bus=bus,
        now=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    next_fn = AsyncMock()

    await middleware(
        _owner_ctx(
            "yes",
            reply_to_bot=True,
            reply_to_text="Persona evolution proposal for personas/alpha-2.md\nProposed change:\n- concise",
            reply_to_message_id="tg-msg-1",
        ),
        next_fn,
    )
    confirmation = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)

    assert next_fn.await_count == 0
    assert confirmation.channel == "telegram"
    assert confirmation.chat_id == "tg-owner"
    assert "applied" in confirmation.content


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_denies_reply_no(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    evolution_path = workspace / "personas" / "alpha-2.evolution.md"
    original = evolution_path.read_text(encoding="utf-8")
    state_db = tmp_path / "persona-evolution.db"
    proposal_path = tmp_path / "proposal.md"
    _record_test_proposal(
        workspace=workspace,
        state_db=state_db,
        proposal_path=proposal_path,
        proposal_id="abc123",
    )
    approval_message = (
        "Persona evolution proposal for personas/alpha-2.md\n"
        "Approve: pe-approve-abc123\n"
        "Deny: pe-deny-abc123"
    )
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=workspace,
        state_db_path=state_db,
        bus=bus,
        now=lambda: datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    )
    next_fn = AsyncMock()

    await middleware(
        _owner_ctx(
            "no",
            reply_to_bot=True,
            reply_to_text=approval_message,
            reply_to_message_id="tg-msg-1",
        ),
        next_fn,
    )
    confirmation = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)

    assert next_fn.await_count == 0
    assert "denied" in confirmation.content
    assert evolution_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_ignores_bare_yes(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=_workspace(tmp_path),
        state_db_path=tmp_path / "persona-evolution.db",
        bus=bus,
    )
    next_fn = AsyncMock()

    await middleware(_owner_ctx("yes"), next_fn)

    assert next_fn.await_count == 1
    assert bus.outbound.qsize() == 0


@pytest.mark.asyncio
async def test_persona_evolution_approval_middleware_halts_stale_code(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    middleware = PersonaEvolutionApprovalMiddleware(
        workspace=_workspace(tmp_path),
        state_db_path=tmp_path / "persona-evolution.db",
        bus=bus,
    )
    next_fn = AsyncMock()

    await middleware(_owner_ctx("pe-approve-missing"), next_fn)

    assert next_fn.await_count == 0
    assert bus.outbound.qsize() == 0
