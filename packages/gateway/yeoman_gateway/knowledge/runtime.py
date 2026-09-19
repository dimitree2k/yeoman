"""Runtime authority adapters: the only bridge from Knowledge to Policy and the archive.

Composition-root code builds exactly one :class:`RuntimeKnowledgePolicy` and one
:class:`RuntimeKnowledgeSources` and injects them into ``open_knowledge_store``.
Knowledge itself never imports the policy engine, the chat registry or the processing
store: it only consumes these two protocols.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from yeoman_gateway.knowledge.authority import EvidenceAudience, PolicyMembership
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    SourceRef,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
)


def _as_principal_set(raw: Any) -> frozenset[str]:
    """Normalize the proven participant list of a registry record."""
    if raw is None:
        return frozenset()
    if isinstance(raw, Mapping):
        raw = raw.get("participants") or raw.get("members") or []
    members: set[str] = set()
    for item in raw or ():
        if isinstance(item, str):
            members.add(item)
            continue
        for attribute in ("principal_id", "id", "jid", "lid", "user_id"):
            value = getattr(item, attribute, None)
            if value:
                members.add(str(value))
                break
        else:
            if isinstance(item, Mapping):
                for attribute in ("principal_id", "id", "jid", "lid", "user_id"):
                    if item.get(attribute):
                        members.add(str(item[attribute]))
                        break
    return frozenset(member for member in members if member)


@dataclass
class RuntimeKnowledgePolicy:
    """Authorization adapter over the live Policy engine and the chat registry.

    ``policy_revision`` must change whenever owner rights, tool rights or capture
    permission change; the adapter reads it from the engine so a stale read context is
    detected instead of being trusted.
    """

    engine: Any
    chat_registry: Any | None = None
    policy_revision: int = 1
    admin_principals: frozenset[str] = frozenset()
    capture_actors: frozenset[str] = frozenset()
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000)

    # ── PolicyAuthority ──────────────────────────────────────────────────────

    def current_policy_revision(self) -> int:
        return int(self.policy_revision)

    def require_admin(self, context: TrustedAdminContext) -> str:
        if not context.owner:
            raise KnowledgeError("unauthorized", "admin context lacks owner authority")
        if int(context.policy_revision) != int(self.policy_revision):
            raise KnowledgeError("stale_revision", "policy revision changed")
        if self.admin_principals and context.actor_principal not in self.admin_principals:
            raise KnowledgeError("unauthorized", "actor is not an administrator")
        if self.admin_principals:
            self._require_policy_owner(context.actor_principal)
        return context.authorization_ref

    def _require_policy_owner(self, principal: str) -> None:
        """Ask the policy engine, not the caller, whether this principal is an owner."""
        is_owner = getattr(self.engine, "is_owner", None)
        if is_owner is None:
            return
        for candidate in _principal_candidates(principal):
            try:
                if is_owner(candidate):
                    return
            except Exception:  # pragma: no cover - defensive
                continue
        raise KnowledgeError("unauthorized", "owner status is not confirmed by policy")

    def require_capture(self, context: TrustedCaptureContext) -> str:
        if not context.authorized:
            raise KnowledgeError("unauthorized", "capture is not authorized")
        if int(context.policy_revision) != int(self.policy_revision):
            raise KnowledgeError("stale_revision", "policy revision changed")
        if self.capture_actors and context.actor_principal:
            if context.actor_principal not in self.capture_actors:
                raise KnowledgeError("unauthorized", "actor may not capture")
        return context.request_id

    def membership(self, context: TrustedReadContext) -> PolicyMembership | None:
        if self.chat_registry is None:
            return None
        record = self.chat_registry.get_chat(context.channel, context.chat_id)
        if not isinstance(record, Mapping):
            return None
        metadata = record.get("metadata")
        raw = metadata.get("participants") if isinstance(metadata, Mapping) else None
        members = _as_principal_set(raw)
        if not members and context.is_direct:
            return None
        if not members:
            return None
        revision = str(
            record.get("last_sync_at")
            or record.get("last_seen_at")
            or f"{context.channel}:{context.chat_id}:{len(members)}"
        )
        return PolicyMembership(members=members, revision=revision)


def _principal_candidates(principal: str) -> tuple[str, ...]:
    token = str(principal or "").strip()
    if not token:
        return ()
    channel, _, value = token.partition(":")
    if not value:
        return (token,)
    out = [token, f"{channel}:{value}"]
    if channel == "whatsapp":
        local = value.split("@", 1)[0]
        out.extend(
            [
                local,
                f"{local}@s.whatsapp.net",
                f"{local}@lid",
                f"whatsapp:{local}",
            ]
        )
    return tuple(dict.fromkeys(out))


@dataclass
class ArchiveEvidence:
    """One proven archived event revision with its audience snapshot."""

    source: SourceRef
    audience: EvidenceAudience
    revoked: bool = False


@dataclass
class RuntimeKnowledgeSources:
    """Proof adapter over the inbound archive/processing store.

    A source is only "proven" when it was registered here from the archive owner's own
    metadata.  Audiences come from the membership snapshot recorded with the evidence -
    never from a model and never guessed from a name.
    """

    observations: dict[str, TrustedIdentityObservation] = field(default_factory=dict)
    evidence_refs: set[str] = field(default_factory=set)
    archive: dict[tuple[str, int], ArchiveEvidence] = field(default_factory=dict)

    # ── registration by the archive owner ────────────────────────────────────

    def observe(self, observation: TrustedIdentityObservation) -> str:
        """Register a channel-verified identity observation."""
        self.observations[observation.evidence_ref] = observation
        self.evidence_refs.add(observation.evidence_ref)
        return observation.evidence_ref

    def issue_evidence_ref(self, reference: str) -> str:
        self.evidence_refs.add(str(reference))
        return str(reference)

    def register_source(
        self,
        source: SourceRef,
        audience: EvidenceAudience,
        *,
        revoked: bool = False,
    ) -> SourceRef:
        self.archive[source.key] = ArchiveEvidence(
            source=source, audience=audience, revoked=revoked
        )
        return source

    def register_sources(
        self, entries: Iterable[tuple[SourceRef, EvidenceAudience]]
    ) -> tuple[SourceRef, ...]:
        return tuple(self.register_source(source, audience) for source, audience in entries)

    def revoke(self, source: SourceRef) -> None:
        existing = self.archive.get(source.key)
        if existing is None:
            return
        self.archive[source.key] = ArchiveEvidence(
            source=existing.source, audience=existing.audience, revoked=True
        )

    # ── SourceAuthority ──────────────────────────────────────────────────────

    def verify_observation(self, observation: TrustedIdentityObservation) -> str:
        known = self.observations.get(observation.evidence_ref)
        if known is None:
            raise KnowledgeError("unauthorized", "identity observation was never issued")
        if known.identifiers != observation.identifiers:
            raise KnowledgeError("unauthorized", "observation does not match its evidence")
        return observation.evidence_ref

    def verify_evidence_ref(self, evidence_ref: str) -> str:
        if str(evidence_ref) not in self.evidence_refs:
            raise KnowledgeError("unauthorized", "evidence reference was never issued")
        return str(evidence_ref)

    def verify_source(self, source: SourceRef) -> bool:
        entry = self.archive.get(source.key)
        if entry is None:
            return False
        return entry.source == source

    def verify_source_ref(self, event_id: str, revision: int) -> SourceRef | None:
        entry = self.archive.get((str(event_id), int(revision)))
        return None if entry is None else entry.source

    def source_revoked(self, source: SourceRef) -> bool:
        entry = self.archive.get(source.key)
        return bool(entry is not None and entry.revoked)

    def mark_source_revoked(self, source: SourceRef) -> None:
        self.revoke(source)

    def evidence_audience(self, source: SourceRef, *, basis: str) -> EvidenceAudience | None:
        entry = self.archive.get(source.key)
        return None if entry is None else entry.audience
