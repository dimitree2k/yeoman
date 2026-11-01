"""Default contributor set for group-state schema v1."""

from __future__ import annotations

from ..contract import GroupStateContributor
from .arvid_actions import ArvidActionsSection
from .budgets import BudgetsSection
from .commitments import CommitmentsSection
from .conversations import ConversationsSection
from .people_activity import PeopleActivitySection
from .presence import PresenceSection


def default_contributors() -> tuple[GroupStateContributor, ...]:
    """The P1 sections in deterministic render order."""
    return (
        PresenceSection(),
        ArvidActionsSection(),
        ConversationsSection(),
        CommitmentsSection(),
        BudgetsSection(),
        PeopleActivitySection(),
    )


__all__ = ["default_contributors"]
