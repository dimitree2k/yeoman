"""Security internal data models."""

from __future__ import annotations

from dataclasses import dataclass

from yeoman.core.models import SecurityDecision, SecurityResult, SecuritySeverity, SecurityStage


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleHit:
    """One matched rule hit inside the security engine."""

    tag: str
    severity: SecuritySeverity
    reason: str


__all__ = [
    "RuleHit",
    "SecurityDecision",
    "SecurityResult",
    "SecuritySeverity",
    "SecurityStage",
]
