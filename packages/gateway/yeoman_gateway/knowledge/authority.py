"""Trusted authority adapters and the protocols the knowledge core depends on.

Knowledge never invents authorization.  Owner rights, tool rights, capture permission
and the target audience come from Policy; proven platform mappings and archived
evidence come from the channel/archive owners.  These adapters are the only place where
that boundary is crossed, and the composition root injects exactly one implementation
of each protocol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    SourceRef,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
    ValidationError,
)


@dataclass(frozen=True, slots=True)
class EvidenceAudience:
    """Who may read one archived source revision.

    ``status`` is one of ``known`` (a proven member list), ``author_only`` (the author
    is the only reader) or ``unknown`` (no proven basis - the caller must fail closed).
    """

    status: str
    members: frozenset[str] = frozenset()
    snapshot_id: str | None = None
    policy_revision: str | None = None
    allowed: frozenset[str] = frozenset()
    explicit: bool = False

    @classmethod
    def known(
        cls,
        members: frozenset[str] | set[str],
        *,
        snapshot_id: str | None = None,
        policy_revision: str | int | None = None,
    ) -> "EvidenceAudience":
        return cls(
            status="known",
            members=frozenset(members),
            snapshot_id=snapshot_id,
            policy_revision=(
                str(policy_revision) if policy_revision is not None else None
            ),
        )

    @classmethod
    def author_only(
        cls,
        *,
        snapshot_id: str | None = None,
        policy_revision: str | int | None = None,
    ) -> "EvidenceAudience":
        return cls(
            status="author_only",
            snapshot_id=snapshot_id,
            policy_revision=(
                str(policy_revision) if policy_revision is not None else None
            ),
        )

    @classmethod
    def unknown(cls) -> "EvidenceAudience":
        return cls(status="unknown")


@dataclass(frozen=True, slots=True)
class PolicyMembership:
    """Current, proven membership of a chat plus its revision."""

    members: frozenset[str]
    revision: str


@runtime_checkable
class SourceAuthority(Protocol):
    """Proof owner for platform observations and archived evidence."""

    def verify_observation(self, observation: TrustedIdentityObservation) -> str: ...

    def verify_evidence_ref(self, evidence_ref: str) -> str: ...

    def verify_source(self, source: SourceRef) -> bool: ...

    def source_revoked(self, source: SourceRef) -> bool: ...

    def mark_source_revoked(self, source: SourceRef) -> None: ...

    def evidence_audience(self, source: SourceRef, *, basis: str) -> EvidenceAudience | None: ...


@runtime_checkable
class PolicyAuthority(Protocol):
    """Authorization owner: admin rights, capture permission and membership."""

    def current_policy_revision(self) -> int: ...

    def require_admin(self, context: TrustedAdminContext) -> str: ...

    def require_capture(self, context: TrustedCaptureContext) -> str: ...

    def membership(self, context: TrustedReadContext) -> PolicyMembership | None: ...


# ── fakes used by tests and offline checks ───────────────────────────────────


@dataclass
class FakeSourceAuthority:
    """Deterministic source/evidence authority for offline tests.

    Nothing is trusted unless it was explicitly issued here, which is what makes the
    "forged evidence" acceptance cases meaningful.
    """

    observations: dict[str, TrustedIdentityObservation] = field(default_factory=dict)
    evidence_refs: set[str] = field(default_factory=set)
    sources: dict[tuple[str, int], SourceRef] = field(default_factory=dict)
    audiences: dict[tuple[str, int], EvidenceAudience] = field(default_factory=dict)
    revoked: set[tuple[str, int]] = field(default_factory=set)

    def issue_observation(self, observation: TrustedIdentityObservation) -> str:
        self.observations[observation.evidence_ref] = observation
        self.evidence_refs.add(observation.evidence_ref)
        return observation.evidence_ref

    def issue_evidence_ref(self, reference: str) -> str:
        self.evidence_refs.add(str(reference))
        return str(reference)

    def issue_source(
        self, source: SourceRef, audience: EvidenceAudience | None = None
    ) -> SourceRef:
        self.sources[source.key] = source
        if audience is not None:
            self.audiences[source.key] = audience
        return source

    def revoke_source(self, source: SourceRef) -> None:
        self.revoked.add(source.key)

    def verify_observation(self, observation: TrustedIdentityObservation) -> str:
        known = self.observations.get(observation.evidence_ref)
        if known is None:
            raise KnowledgeError(
                "unauthorized", "identity observation evidence was never issued"
            )
        if known.identifiers != observation.identifiers:
            raise KnowledgeError("unauthorized", "observation does not match its evidence")
        return observation.evidence_ref

    def verify_evidence_ref(self, evidence_ref: str) -> str:
        if str(evidence_ref) not in self.evidence_refs:
            raise KnowledgeError("unauthorized", "evidence reference was never issued")
        return str(evidence_ref)

    def verify_source(self, source: SourceRef) -> bool:
        known = self.sources.get(source.key)
        if known is None:
            return False
        return known == source

    def source_revoked(self, source: SourceRef) -> bool:
        return source.key in self.revoked

    def mark_source_revoked(self, source: SourceRef) -> None:
        """Record a revocation decided by the knowledge lifecycle."""
        self.revoked.add(source.key)

    def evidence_audience(self, source: SourceRef, *, basis: str) -> EvidenceAudience | None:
        return self.audiences.get(source.key)

    def verify_source_ref(self, event_id: str, revision: int) -> SourceRef | None:
        return self.sources.get((str(event_id), int(revision)))


@dataclass
class FakePolicyAuthority:
    """Deterministic authorization authority for offline tests."""

    revision: int = 1
    admins: set[str] = field(default_factory=set)
    capture_actors: set[str] = field(default_factory=set)
    memberships: dict[str, PolicyMembership] = field(default_factory=dict)
    admin_refs: set[str] = field(default_factory=lambda: {"admin-ref-1"})
    capture_refs: set[str] = field(default_factory=lambda: {"cap-ref-1"})

    @staticmethod
    def scope_key(context: TrustedReadContext) -> str:
        return context.scope_key()

    def set_members(
        self, context: TrustedReadContext, members: set[str], *, revision: str
    ) -> None:
        self.memberships[self.scope_key(context)] = PolicyMembership(
            members=frozenset(members), revision=str(revision)
        )

    def issue_capture(self, request_id: str) -> str:
        """Register one capture request as authorized by Policy."""
        self.capture_refs.add(str(request_id))
        return str(request_id)

    def issue_admin(self, authorization_ref: str) -> str:
        """Register one admin authorization reference as valid."""
        self.admin_refs.add(str(authorization_ref))
        return str(authorization_ref)

    def current_policy_revision(self) -> int:
        return int(self.revision)

    def require_admin(self, context: TrustedAdminContext) -> str:
        if context.actor_principal not in self.admins:
            raise KnowledgeError("unauthorized", "actor is not an administrator")
        if int(context.policy_revision) != int(self.revision):
            raise KnowledgeError("stale_revision", "policy revision changed")
        if self.admin_refs and not (
            context.authorization_ref in self.admin_refs
            or str(context.authorization_ref).startswith("policy:")
        ):
            raise KnowledgeError("unauthorized", "authorization reference is not valid")
        return context.authorization_ref

    def require_capture(self, context: TrustedCaptureContext) -> str:
        if not context.authorized:
            raise KnowledgeError("unauthorized", "capture is not authorized")
        if context.actor_principal and context.actor_principal not in self.capture_actors:
            raise KnowledgeError("unauthorized", "actor may not capture")
        if int(context.policy_revision) != int(self.revision):
            raise KnowledgeError("stale_revision", "policy revision changed")
        if context.admin_initiated:
            if not context.actor_principal or (
                self.admins and context.actor_principal not in self.admins
            ):
                raise KnowledgeError("unauthorized", "actor may not capture administratively")
            return context.request_id
        if self.capture_refs and context.request_id not in self.capture_refs:
            raise KnowledgeError("unauthorized", "capture request was never issued")
        return context.request_id

    def membership(self, context: TrustedReadContext) -> PolicyMembership | None:
        return self.memberships.get(self.scope_key(context))


@dataclass
class FakeClock:
    """A fixed, advanceable clock so time-based behaviour is deterministic."""

    value_ms: int = 1_700_000_000_000

    def now_ms(self) -> int:
        return int(self.value_ms)

    def advance(self, delta_ms: int) -> int:
        self.value_ms = int(self.value_ms) + int(delta_ms)
        return self.value_ms


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


def require_context_type(context: Any, expected: type, name: str) -> Any:
    if not isinstance(context, expected):
        raise ValidationError(f"{name} must be a {expected.__name__}")
    return context
