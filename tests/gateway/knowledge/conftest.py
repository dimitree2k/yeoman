"""Shared fixtures for the person-knowledge suite.

Everything here is synthetic and offline: temporary databases only, no provider calls,
no network, no live runtime database.  The harness drives the *public* API; it never
duplicates business logic and never reaches into the store to make a test pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
from yeoman_gateway.knowledge.api import KnowledgeService, open_knowledge_store
from yeoman_gateway.knowledge.authority import (
    EvidenceAudience,
    FakeClock,
    FakePolicyAuthority,
    FakeSourceAuthority,
)
from yeoman_gateway.knowledge.models import (
    CaptureResult,
    Identifier,
    KnowledgeContext,
    PersonLinkCandidate,
    PersonResolution,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    StatementSummary,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
)

WORKSPACE_ID = "test-workspace"

#: Synthetic principals.  Deliberately channel-qualified and obviously fake.
OWNER = "whatsapp:4910000000001"
TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
MARIA = "whatsapp:4910000000004"
NEW_MEMBER = "whatsapp:4910000000005"


@dataclass
class ProviderSpy:
    """Records every piece of text that would reach an embedding/provider boundary."""

    seen: list[str] = field(default_factory=list)

    def observe(self, text: str) -> None:
        self.seen.append(str(text))

    def saw(self, needle: str) -> bool:
        return any(needle in item for item in self.seen)

    def clear(self) -> None:
        self.seen.clear()


class KnowledgeHarness:
    """A fully synthetic knowledge runtime bound to a temporary database."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.authority = FakeSourceAuthority()
        self.policy = FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER})
        self.service: KnowledgeService = open_knowledge_store(
            tmp_path / "knowledge.db",
            workspace_id=WORKSPACE_ID,
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self.clock,
        )
        self.provider_spy = ProviderSpy()
        self._observation_counter = 0
        self._source_counter = 0
        self._people: dict[str, str] = {}
        self._principals: dict[str, str] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self.service.close()

    # ── people ───────────────────────────────────────────────────────────────

    def observe(
        self,
        channel: str,
        kind: str,
        value: str,
        *,
        mapping: bool = False,
        name: str | None = None,
        extra: tuple[Identifier, ...] = (),
        context: TrustedReadContext | None = None,
    ) -> PersonResolution:
        """Issue a verified observation and resolve it through the public API."""
        self._observation_counter += 1
        identifiers = (Identifier(channel=channel, kind=kind, value=value), *extra)
        observation = TrustedIdentityObservation(
            identifiers=identifiers,
            evidence_ref=f"obs-{self._observation_counter}",
            observed_name=name,
            observed_at_ms=self.clock.now_ms(),
            mapping_verified=mapping,
        )
        self.authority.issue_observation(observation)
        resolved = self.service.resolve_person(observation, context=context)
        if resolved.person_id and resolved.person_id not in self._principals:
            self._principals[resolved.person_id] = self._principal_for_identifiers(
                identifiers, resolved.person_id
            )
        return resolved

    @staticmethod
    def _principal_for_identifiers(
        identifiers: tuple[Identifier, ...], fallback: str
    ) -> str:
        """The security principal of a synthetic person, derived from its identifiers."""
        for identifier in identifiers:
            if identifier.channel == "whatsapp" and identifier.kind == "phone_jid":
                return f"whatsapp:{identifier.value.split('@', 1)[0]}"
            if identifier.channel == "telegram" and identifier.kind == "telegram_id":
                return f"telegram:{identifier.value}"
        return f"whatsapp:{fallback}"

    #: Deterministic synthetic principals in creation order.  No real values.
    SYNTHETIC_PRINCIPALS = (
        "whatsapp:4910000000002",
        "whatsapp:4910000000003",
        "whatsapp:4910000000004",
        "whatsapp:4910000000005",
        "whatsapp:4910000000006",
        "whatsapp:4910000000007",
        "whatsapp:4910000000008",
        "whatsapp:4910000000009",
        "whatsapp:4910000000010",
        "whatsapp:4910000000011",
    )

    def person(self, name: str) -> str:
        """Create one synthetic person with a distinct trusted identifier."""
        if name in self._people:
            return self._people[name]
        index = len(self._people)
        if index < len(self.SYNTHETIC_PRINCIPALS):
            principal = self.SYNTHETIC_PRINCIPALS[index]
        else:  # pragma: no cover - defensive, still synthetic
            principal = f"whatsapp:49199999{index:05d}"
        return self.person_for(principal, name)

    def person_for(self, principal: str, name: str) -> str:
        """Create a person whose proven identifier maps to an explicit principal."""
        number = principal.split(":", 1)[-1]
        resolved = self.observe("whatsapp", "phone_jid", f"{number}@s.whatsapp.net", name=name)
        assert resolved.person_id, resolved.reason
        self._people[name] = resolved.person_id
        self._principals[resolved.person_id] = principal
        return resolved.person_id

    def principal_for(self, person_id: str) -> str:
        """The security principal of a synthetic person."""
        known = self._principals.get(person_id)
        if known is None:
            raise AssertionError(f"no synthetic principal for {person_id}")
        return known

    def two_people(self) -> tuple[str, str]:
        return self.person("Tom"), self.person("Alex")

    # ── sources and statements ───────────────────────────────────────────────

    def source(
        self,
        author_person: str,
        *,
        chat: str = "group-a",
        channel: str = "whatsapp",
        audience: set[str] | None = None,
        revision: int = 1,
        author_only: bool = False,
        unknown_audience: bool = False,
        when_ms: int | None = None,
    ) -> SourceRef:
        """Issue an archived source revision with a proven audience."""
        self._source_counter += 1
        principal = self.principal_for(author_person)
        members = set(audience) if audience is not None else {principal}
        source = SourceRef(
            event_id=f"event-{self._source_counter}",
            revision=int(revision),
            channel=channel,
            chat_id=chat,
            author_principal=principal,
            occurred_at_ms=int(when_ms if when_ms is not None else self.clock.now_ms()),
        )
        if unknown_audience:
            self.authority.issue_source(source, EvidenceAudience.unknown())
        elif author_only:
            self.authority.issue_source(source, EvidenceAudience.author_only())
        else:
            self.authority.issue_source(
                source,
                EvidenceAudience.known(members, snapshot_id=f"snap-{source.event_id}"),
            )
        return source

    def capture_context(self, *sources: SourceRef, basis: str = "user_message") -> TrustedCaptureContext:
        request_id = f"cap-{'/'.join(item.event_id for item in sources) or 'empty'}"
        self.policy.issue_capture(request_id)
        return TrustedCaptureContext(
            request_id=request_id,
            policy_revision=self.policy.revision,
            capture_basis=basis,
            authorized_sources=tuple(sources),
            actor_principal=OWNER,
            authorized=True,
        )

    def candidate(
        self,
        content: str,
        source: SourceRef,
        *,
        subjects: tuple[str, ...] = (),
        participants: tuple[str, ...] = (),
        reported_speakers: tuple[str, ...] = (),
        extractor_version: str = "test-extractor-1",
        confidence: float = 0.5,
        unresolved: tuple[str, ...] = (),
        valid_until_ms: int | None = None,
        sources: tuple[SourceRef, ...] | None = None,
    ) -> StatementCandidate:
        extra = sources if sources is not None else (source,)
        links = [
            PersonLinkCandidate(
                person_id=person_id, role=role, source=source, attribution="extracted"
            )
            for role, people in (
                ("subject", subjects),
                ("participant", participants),
                ("reported_speaker", reported_speakers),
            )
            for person_id in people
        ]
        return StatementCandidate(
            content=content,
            sources=tuple(extra),
            people=tuple(links),
            extractor_version=extractor_version,
            confidence=confidence,
            valid_until_ms=valid_until_ms,
            unresolved_mentions=unresolved,
        )

    def capture_text(
        self,
        text: str,
        source: SourceRef,
        *,
        subjects: tuple[str, ...] = (),
        participants: tuple[str, ...] = (),
        reported_speakers: tuple[str, ...] = (),
        confidence: float = 0.5,
    ) -> CaptureResult:
        candidate = self.candidate(
            text,
            source,
            subjects=subjects,
            participants=participants,
            reported_speakers=reported_speakers,
            confidence=confidence,
        )
        return self.service.capture(candidate, context=self.capture_context(source))

    # ── reads ────────────────────────────────────────────────────────────────

    def admin_context(self) -> TrustedAdminContext:
        return TrustedAdminContext(
            actor_principal=OWNER,
            policy_revision=self.policy.revision,
            authorization_ref="admin-ref-1",
            owner=True,
        )

    def read_context(
        self,
        reader: str,
        *,
        chat: str = "group-a",
        channel: str = "whatsapp",
        recipients: set[str] | None = None,
        membership_known: bool = True,
        purpose: str = "reply",
        is_direct: bool = False,
        owner: bool = False,
        policy_revision: int | None = None,
    ) -> TrustedReadContext:
        principal = reader if ":" in reader else self.principal_for(reader)
        members = set(recipients) if recipients is not None else {principal}
        revision = f"mem-{chat}-{len(members)}"
        if membership_known:
            self.policy.set_members(
                TrustedReadContext(
                    principal_id=principal,
                    channel=channel,
                    chat_id=chat,
                    recipient_principals=frozenset(members),
                    membership_revision=revision,
                    policy_revision=self.policy.revision,
                    purpose=purpose,
                    now_ms=self.clock.now_ms(),
                ),
                members,
                revision=revision,
            )
        context = TrustedReadContext(
            principal_id=principal,
            channel=channel,
            chat_id=chat,
            recipient_principals=frozenset(members) if membership_known else None,
            membership_revision=revision if membership_known else None,
            policy_revision=(
                self.policy.revision if policy_revision is None else policy_revision
            ),
            purpose=purpose,
            now_ms=self.clock.now_ms(),
            is_direct=is_direct,
            owner=owner,
        )
        return context

    def add_recipient(self, context: TrustedReadContext, person_id: str) -> TrustedReadContext:
        """Simulate a membership change: the same chat now has one more member."""
        principal = self.principal_for(person_id)
        members = set(context.recipient_principals or frozenset()) | {principal}
        revision = f"{context.membership_revision}+{principal}"
        self.policy.set_members(context, members, revision=revision)
        from dataclasses import replace

        return replace(
            context,
            recipient_principals=frozenset(members),
            membership_revision=revision,
            now_ms=self.clock.now_ms(),
        )

    def recall_person(
        self,
        person: str,
        context: TrustedReadContext,
        *,
        roles: tuple[str, ...] = (),
        text: str = "",
        limit: int = 10,
    ) -> KnowledgeContext:
        query = RecallQuery(text=text, person_ids=(person,), roles=roles, limit=limit)
        result = self.service.recall(query, context=context)
        self.provider_spy.observe(result.text)
        return result

    def recall_text(self, text: str, context: TrustedReadContext, *, limit: int = 10) -> KnowledgeContext:
        result = self.service.recall(
            RecallQuery(text=text, limit=limit), context=context
        )
        self.provider_spy.observe(result.text)
        return result

    def statement(self, statement_id: str) -> StatementSummary:
        return self.service.inspect_statement(statement_id, context=self.admin_context())

    def links(self, statement_id: str) -> tuple[PersonLinkCandidate, ...]:
        return self.statement(statement_id).people

    # ── identity introspection helpers ───────────────────────────────────────

    def identity_revision(self) -> int:
        return self.service.stats(context=self.admin_context()).identity_revision

    def acl_epoch(self) -> int:
        return self.service.stats(context=self.admin_context()).acl_epoch

    def resolve_original(self, person_id: str) -> PersonResolution:
        """Canonical person for an original id, resolved through proven bindings."""
        canonical = self.service.person_for_principal(self.principal_for(person_id))
        return PersonResolution(
            status="resolved" if canonical else "unresolved",
            person_id=canonical,
            display_name=self.service.display_name(person_id),
            identity_revision=self.identity_revision(),
            reason="canonical",
        )

    def original_bindings(self) -> tuple[tuple[str, str], ...]:
        """All identifier bindings as (canonical person id, identifier value)."""
        rows = self.service._store.query(  # noqa: SLF001 - white-box identity assertion
            "SELECT person_id, channel, kind, value FROM knowledge_identifier_bindings"
            " ORDER BY value"
        )
        return tuple(
            (str(row["person_id"]), f"{row['channel']}:{row['kind']}:{row['value']}")
            for row in rows
        )

    def snapshot_counts(self) -> dict[str, int]:
        return self.service._store.counts()  # noqa: SLF001 - white-box rollback assertion

    def fail_next_commit(self) -> None:
        self.service._store.fail_next_commit = True  # noqa: SLF001 - fault injection

    def security_fingerprint(self) -> str:
        """Content fingerprint of every authorization-bearing row in the store."""
        import hashlib
        import json

        payload: dict[str, list[list[object]]] = {}
        for table in (
            "memory2_fact_principals",
            "knowledge_statement_principals",
            "memory2_facts",
            "contacts",
        ):
            rows = self.service._store.query(  # noqa: SLF001 - white-box ACL assertion
                f"SELECT * FROM {table} ORDER BY 1"
            )
            payload[table] = [list(row) for row in rows]
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def active_statements_for_source(self, source: SourceRef) -> tuple[str, ...]:
        return self.service.active_statement_ids_for_source(source)


@pytest.fixture
def knowledge_harness(tmp_path: Path) -> Iterator[KnowledgeHarness]:
    harness = KnowledgeHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def harness_factory(tmp_path: Path) -> Iterator[Any]:
    created: list[KnowledgeHarness] = []

    def make(name: str = "h") -> KnowledgeHarness:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        harness = KnowledgeHarness(directory)
        created.append(harness)
        return harness

    yield make
    for harness in created:
        harness.close()
