"""Tests for persona evolution proposal evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.memory.service import MemoryService
from yeoman_gateway.persona_evolution import (
    chats_for_persona,
    collect_persona_evolution_evidence,
    render_persona_evolution_proposal,
)
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import MemoryConfig


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


def test_chats_for_persona_uses_effective_policy_persona() -> None:
    refs = chats_for_persona(_policy(), "personas/alpha-2.md")

    assert [(ref.channel, ref.chat_id, ref.source) for ref in refs] == [
        ("whatsapp", "group-a", "chat"),
        ("whatsapp", "group-b", "chat"),
    ]


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
