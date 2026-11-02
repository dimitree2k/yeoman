"""Default contributor set for group-state schema v1."""

from __future__ import annotations

from ..contract import GroupStateContributor
from .arvid_actions import ArvidActionsSection
from .budgets import BudgetsSection
from .commitments import CommitmentsSection
from .conversations import ConversationsSection
from .past import PastSection
from .people_activity import PeopleActivitySection
from .presence import PresenceSection


def default_contributors(*, include_past: bool = False) -> tuple[GroupStateContributor, ...]:
    """The deterministic sections in render order; ``past`` only with summaries on."""
    contributors: list[GroupStateContributor] = [
        PresenceSection(),
        ArvidActionsSection(),
        ConversationsSection(),
        CommitmentsSection(),
        BudgetsSection(),
        PeopleActivitySection(),
    ]
    if include_past:
        contributors.append(PastSection())
    return tuple(contributors)


__all__ = ["default_contributors"]
