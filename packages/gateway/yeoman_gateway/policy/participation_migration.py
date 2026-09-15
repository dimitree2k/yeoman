"""Staged migration to autonomous participation: in-memory candidates only.

Nothing in this module writes policy. It turns one chat into a *candidate* for
review, and it refuses any transformation that would broaden who may reach Arvid.

Two owner-facing guarantees shape the transformation:

* access stays exactly as it was - who-can-talk, blocked senders, allowed tools and
  reply-action vetoes are copied, never recomputed;
* an opted-in autonomous chat no longer needs ``all`` or ``mention_only`` to decide
  participation, so those legacy modes become inert *for that chat only*, while
  every non-migrated chat keeps them.

The old dynamic initiation cap (confidence-driven expansion) is retired in favour
of its base cap, and the reduction is reported rather than hidden.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Legacy knobs that no longer affect an opted-in autonomous chat. They stay in the
#: schema for non-migrated chats and are reported instead of silently dropped.
OBSOLETE_FOR_AUTONOMOUS_CHATS: tuple[str, ...] = (
    "whenToReply.mode=all",
    "whenToReply.mode=mention_only",
)


class MigrationError(ValueError):
    """The candidate would broaden access or is otherwise unsafe to stage."""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What the candidate changed, and what an operator must review."""

    channel: str
    chat_ids: tuple[str, ...]
    changed: tuple[str, ...] = ()
    obsolete_for_autonomous: tuple[str, ...] = ()
    initiation_cap_reduction: tuple[tuple[str, int, int], ...] = ()
    notes: tuple[str, ...] = ()
    candidate: dict[str, Any] = field(default_factory=dict)

    @property
    def reduced_allowance(self) -> bool:
        return bool(self.initiation_cap_reduction)


def stage_autonomous_candidate(
    policy: Mapping[str, Any],
    *,
    channel: str,
    chat_ids: Iterable[str],
    base_daily_cap: int | None = None,
    guidance: str | None = None,
    dynamic_cap: tuple[bool, int] | None = None,
) -> MigrationReport:
    """Return a validated in-memory candidate with the selected chats opted in.

    The input mapping is never mutated. When the running configuration has a
    confidence-expanded dynamic cap (``dynamic_cap=(enabled, max)``), the candidate
    records the reduced allowance: the autonomous path uses the configured daily cap
    as a hard cap and does not re-implement confidence-based expansion.
    """
    selected = tuple(dict.fromkeys(str(chat_id).strip() for chat_id in chat_ids if str(chat_id).strip()))
    if not selected:
        raise MigrationError("at least one chat must be selected")
    import copy

    candidate = copy.deepcopy(dict(policy))
    channels = candidate.get("channels")
    if not isinstance(channels, dict):
        raise MigrationError(f"policy has no channels mapping for {channel}")
    channel_policy = channels.get(channel)
    if not isinstance(channel_policy, dict):
        raise MigrationError(f"channel {channel} is not configured")
    chats = channel_policy.get("chats")
    if not isinstance(chats, dict):
        raise MigrationError(f"channel {channel} has no chats mapping")

    changed: list[str] = []
    reductions: list[tuple[str, int, int]] = []
    for chat_id in selected:
        override = chats.get(chat_id)
        if not isinstance(override, dict):
            raise MigrationError(f"chat {chat_id} is not configured; refusing to invent it")
        access_before = _access_fingerprint(override)
        participation = dict(override.get("participation") or {})
        participation["enabled"] = True
        if guidance is not None:
            participation["guidance"] = str(guidance)
        override["participation"] = participation
        changed.append(f"channels.{channel}.chats.{chat_id}.participation.enabled=true")

        spontaneity = override.get("spontaneity")
        if isinstance(spontaneity, dict) and base_daily_cap is not None:
            current_base = spontaneity.get("dailyCap")
            if current_base != base_daily_cap:
                spontaneity["dailyCap"] = int(base_daily_cap)
                changed.append(
                    f"channels.{channel}.chats.{chat_id}.spontaneity.dailyCap="
                    f"{int(base_daily_cap)}"
                )
        if dynamic_cap is not None and bool(dynamic_cap[0]):
            effective_before = int(dynamic_cap[1])
            effective_after = int(
                base_daily_cap
                if base_daily_cap is not None
                else (spontaneity.get("dailyCap") if isinstance(spontaneity, dict) else 0) or 0
            )
            if effective_before > effective_after:
                reductions.append((chat_id, effective_before, effective_after))
        access_after = _access_fingerprint(override)
        if access_after != access_before:
            # Cannot happen by construction; kept as a real guard, not decoration.
            raise MigrationError(
                f"candidate would change access for {chat_id}: {access_before} -> {access_after}"
            )
    return MigrationReport(
        channel=channel,
        chat_ids=selected,
        changed=tuple(changed),
        obsolete_for_autonomous=OBSOLETE_FOR_AUTONOMOUS_CHATS,
        initiation_cap_reduction=tuple(reductions),
        notes=(
            "Access lists, tool permissions and reply-action vetoes are copied unchanged.",
            "off, blocked senders, owner-only and allowed-sender restrictions still apply.",
            "Quiet hours keep their existing UTC interpretation.",
            "No automatic migration happens at startup; apply this candidate explicitly.",
        ),
        candidate=candidate,
    )


def _access_fingerprint(override: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that decide who may reach Arvid and what Arvid may use."""
    keys = (
        "whoCanTalk",
        "whenToReply",
        "blockedSenders",
        "allowedTools",
        "toolAccess",
        "replyBudget",
        "contactsDisclosure",
    )
    return {key: override.get(key) for key in keys if key in override}


def obsolete_knobs_for_autonomous_chat(effective: Any) -> tuple[str, ...]:
    """Legacy settings that no longer affect this opted-in chat, for inspection."""
    participation = getattr(effective, "participation", None)
    if participation is None or not bool(getattr(participation, "enabled", False)):
        return ()
    return OBSOLETE_FOR_AUTONOMOUS_CHATS


__all__ = [
    "OBSOLETE_FOR_AUTONOMOUS_CHATS",
    "MigrationError",
    "MigrationReport",
    "obsolete_knobs_for_autonomous_chat",
    "stage_autonomous_candidate",
]
