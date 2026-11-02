"""Pure builders for a trusted Knowledge read context.

Kept in the knowledge package so background jobs can use it without importing an
adapter.  The responder re-exports :func:`build_group_read_context`; its imports and
the eval harness keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from yeoman_gateway.knowledge.models import TrustedReadContext


def build_group_read_context(
    *,
    queries: Any,
    channel: str,
    chat_id: str,
    principal: str,
    is_owner: bool,
    at_ms: int,
    policy_revision: int,
    purpose: str,
) -> TrustedReadContext | None:
    """Build a group read context from *history-proven* membership.

    Pure: the caller supplies the snapshot and the instant.  Membership is `None`
    (a refused read) unless the archive proves a known roster at ``at_ms``, which is
    exactly the proof the Knowledge read gate re-checks.  The principal is mapped to
    the roster spelling before the context is built, so an LID message sender and a
    phone-form membership name the same member.
    """
    if queries is None or not str(principal or "").strip():
        return None
    at = int(at_ms)
    audience = queries.members(chat_id=str(chat_id), at_ms=at)
    if audience.status != "known" or not audience.members:
        return None
    members = frozenset(audience.members)
    resolved = str(principal)
    if resolved not in members:
        token = resolved.partition(":")[2] or resolved
        mapped = queries.member_principal(token, members, at_ms=at)
        if mapped is not None:
            resolved = mapped
    return TrustedReadContext(
        principal_id=resolved,
        channel=str(channel),
        chat_id=str(chat_id),
        recipient_principals=members,
        membership_revision=f"{channel}:{chat_id}:{len(members)}",
        policy_revision=int(policy_revision),
        purpose=str(purpose),
        now_ms=at,
        is_direct=False,
        owner=bool(is_owner),
    )


__all__ = ["build_group_read_context"]
