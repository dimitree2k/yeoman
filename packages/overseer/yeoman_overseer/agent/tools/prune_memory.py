"""Retired memory pruning is unavailable after the History migration."""
from __future__ import annotations


def prune_memory(
    *,
    age_days: int | None = None,
    salience_below: float | None = None,
    domain: str | None = None,
    ctx: object,
) -> dict:
    return {'ok': False, 'error': 'legacy history prune refused; use Knowledge maintenance'}
