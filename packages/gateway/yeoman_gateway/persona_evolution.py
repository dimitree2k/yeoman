"""Manual persona evolution proposal helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.memory.service import MemoryService
from yeoman_gateway.policy.persona import resolve_persona_path
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive


@dataclass(frozen=True, slots=True)
class PersonaChatRef:
    channel: str
    chat_id: str
    persona_file: str
    source: str


@dataclass(frozen=True, slots=True)
class ChatEvolutionEvidence:
    chat: PersonaChatRef
    learned_taste: list[str] = field(default_factory=list)
    recent_preferences: list[str] = field(default_factory=list)
    speakup_outcomes: dict[str, int] = field(default_factory=dict)
    speakups: list[dict[str, Any]] = field(default_factory=list)
    recent_message_count: int = 0


@dataclass(frozen=True, slots=True)
class PersonaEvolutionEvidence:
    persona_file: str
    persona_path: Path
    evolution_path: Path
    collected_at: datetime
    window_days: int
    chats: list[ChatEvolutionEvidence]
    current_evolution_text: str


def chats_for_persona(policy: PolicyConfig, persona_file: str) -> list[PersonaChatRef]:
    """Return explicit policy chats whose effective persona matches persona_file."""
    target = persona_file.strip()
    refs: list[PersonaChatRef] = []
    for channel, channel_policy in sorted(policy.channels.items()):
        channel_default = channel_policy.default.persona_file
        for chat_id, override in sorted(channel_policy.chats.items()):
            effective = override.persona_file or channel_default or policy.defaults.persona_file
            if effective == target:
                source = "chat" if override.persona_file else "channel_or_global_default"
                refs.append(
                    PersonaChatRef(
                        channel=channel,
                        chat_id=chat_id,
                        persona_file=effective,
                        source=source,
                    )
                )
    return refs


async def collect_persona_evolution_evidence(
    *,
    policy: PolicyConfig,
    workspace: Path,
    persona_file: str,
    memory: MemoryService,
    speakup_log: SpeakupLog,
    inbound_archive: InboundArchive,
    window_days: int = 14,
    per_chat_limit: int = 20,
    now: datetime | None = None,
) -> PersonaEvolutionEvidence:
    collected_at = now or datetime.now(UTC)
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    persona_path = resolve_persona_path(persona_file, workspace)
    evolution_path = persona_path.parent / f"{persona_path.stem}.evolution{persona_path.suffix}"
    current_evolution_text = (
        evolution_path.read_text(encoding="utf-8") if evolution_path.is_file() else ""
    )
    since = collected_at - timedelta(days=max(1, int(window_days)))

    chats: list[ChatEvolutionEvidence] = []
    for chat in chats_for_persona(policy, persona_file):
        taste_hits = memory.learned_chat_taste(
            channel=chat.channel,
            chat_id=chat.chat_id,
            limit=per_chat_limit,
        )
        preference_hits = memory.recent_chat_preferences(
            channel=chat.channel,
            chat_id=chat.chat_id,
            limit=per_chat_limit,
        )
        speakups = await speakup_log.history(
            chat.channel,
            chat.chat_id,
            limit=per_chat_limit,
        )
        outcome_counts: dict[str, int] = {}
        for row in speakups:
            key = str(row.get("outcome") or row.get("status") or "unknown")
            outcome_counts[key] = outcome_counts.get(key, 0) + 1
        messages = inbound_archive.lookup_messages_in_range(
            chat.channel,
            chat.chat_id,
            since,
            collected_at,
            limit=per_chat_limit,
            latest=True,
        )
        chats.append(
            ChatEvolutionEvidence(
                chat=chat,
                learned_taste=[hit.entry.content for hit in taste_hits],
                recent_preferences=[hit.entry.content for hit in preference_hits],
                speakup_outcomes=outcome_counts,
                speakups=[_safe_speakup(row) for row in speakups],
                recent_message_count=len(messages),
            )
        )

    return PersonaEvolutionEvidence(
        persona_file=persona_file,
        persona_path=persona_path,
        evolution_path=evolution_path,
        collected_at=collected_at,
        window_days=max(1, int(window_days)),
        chats=chats,
        current_evolution_text=current_evolution_text,
    )


def render_persona_evolution_proposal(evidence: PersonaEvolutionEvidence) -> str:
    """Render a private, owner-reviewable evolution proposal report."""
    lines = [
        "# Persona Evolution Proposal",
        "",
        f"persona_file: `{evidence.persona_file}`",
        f"persona_path: `{evidence.persona_path}`",
        f"evolution_path: `{evidence.evolution_path}`",
        f"collected_at: `{evidence.collected_at.isoformat()}`",
        f"window_days: `{evidence.window_days}`",
        "",
        "## Safety",
        "",
        "- This is a proposal only; no persona files were modified.",
        "- Base persona invariants must take precedence over any suggested evolution.",
        "- Raw chat messages are not included.",
        "",
        "## Evidence Summary",
        "",
    ]
    if not evidence.chats:
        lines.append("No policy chats currently resolve to this persona.")
    for chat_evidence in evidence.chats:
        chat = chat_evidence.chat
        lines.extend(
            [
                f"### {chat.channel}:{chat.chat_id}",
                "",
                f"- policy_source: `{chat.source}`",
                f"- recent_message_count: `{chat_evidence.recent_message_count}`",
                f"- speakup_outcomes: `{json.dumps(chat_evidence.speakup_outcomes, sort_keys=True)}`",
                "",
            ]
        )
        if chat_evidence.learned_taste:
            lines.append("Learned proactive taste:")
            for pattern in chat_evidence.learned_taste:
                lines.append(f"- {pattern}")
            lines.append("")
        if chat_evidence.recent_preferences:
            lines.append("Recent chat preferences:")
            for preference in chat_evidence.recent_preferences[:5]:
                lines.append(f"- {preference}")
            lines.append("")

    lines.extend(
        [
            "## Suggested Evolution Notes",
            "",
            _suggested_notes(evidence),
            "",
            "## Current Evolution File",
            "",
            "```markdown",
            evidence.current_evolution_text.strip() or "(missing)",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _suggested_notes(evidence: PersonaEvolutionEvidence) -> str:
    bullets: list[str] = []
    for chat_evidence in evidence.chats:
        sent = sum(chat_evidence.speakup_outcomes.values())
        if sent <= 0 and not chat_evidence.learned_taste:
            continue
        chat = chat_evidence.chat
        if chat_evidence.learned_taste:
            for pattern in chat_evidence.learned_taste[:3]:
                bullets.append(
                    f"- {evidence.collected_at.date()} `{chat.channel}:{chat.chat_id}`: "
                    f"Consider adding a Consciousness Outcome Lesson from {sent} recent "
                    f"speakup records and learned taste: {pattern}"
                )
        elif sent:
            bullets.append(
                f"- {evidence.collected_at.date()} `{chat.channel}:{chat.chat_id}`: "
                f"Review {sent} recent speakup records before changing persona evolution."
            )
    if not bullets:
        return "No durable persona evolution suggested from the available evidence."
    return "\n".join(bullets)


def _safe_speakup(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": row.get("created_at"),
        "committed_at": row.get("committed_at"),
        "channel": row.get("channel"),
        "chat_id": row.get("chat_id"),
        "action_type": row.get("action_type"),
        "profile": row.get("profile"),
        "status": row.get("status"),
        "outcome": row.get("outcome"),
    }
