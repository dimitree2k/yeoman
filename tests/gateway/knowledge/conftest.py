"""Shared fixtures for the person-knowledge suite.

Everything here is synthetic and offline: temporary databases only, no provider calls,
no network, no live runtime database.  The harness drives the *public* API; it never
duplicates business logic and never reaches into the store to make a test pass.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pytest
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create as create_history_schema
from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
from yeoman_gateway.knowledge._store import KnowledgeStore
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


class _SyntheticHistoryQueries(HistoryQueries):
    """History projection with fixture-supplied, synthetic source audiences."""

    def __init__(self, snapshot: HistorySnapshot, audiences: dict[str, EvidenceAudience]):
        super().__init__(snapshot)
        self._fixture_audiences = audiences

    def audience(self, message_id: str) -> EvidenceAudience:
        provided = self._fixture_audiences.get(message_id)
        return provided if provided is not None else super().audience(message_id)


class KnowledgeHarness:
    """A fully synthetic knowledge runtime bound to a temporary database."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock()
        self.authority = FakeSourceAuthority()
        self.policy = FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER})
        v2_path = tmp_path / "knowledge-v2.db"
        legacy = KnowledgeStore(v2_path)
        legacy.set_meta("migration_complete", "1")
        legacy.close()
        knowledge_path = tmp_path / "knowledge-v3.db"
        upgrade_history_knowledge(source=v2_path, target=knowledge_path)
        self.service: KnowledgeService = open_knowledge_store(
            knowledge_path,
            workspace_id=WORKSPACE_ID,
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self.clock,
        )
        self._history_db = sqlite3.connect(":memory:")
        create_history_schema(self._history_db)
        self._snapshot = HistorySnapshot(1, (), self._history_db)
        from yeoman_gateway.history.context import _HISTORY

        self._history_token = _HISTORY.set(self._snapshot)
        self._audiences: dict[str, EvidenceAudience] = {}
        self.queries = _SyntheticHistoryQueries(self._snapshot, self._audiences)
        self.history_sources = HistoryKnowledgeSources(
            self.queries, {}, self.service.history_source_ledger, self.authority
        )
        self._scope = self.service.history_scope(self.queries, self.history_sources)
        self._scope.__enter__()
        self.provider_spy = ProviderSpy()
        self._observation_counter = 0
        self._source_counter = 0
        self._people: dict[str, str] = {}
        self._principals: dict[str, str] = {}
        self._identifier_people: dict[tuple[str, str, str], str] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._scope.__exit__(None, None, None)
        from yeoman_gateway.history.context import _HISTORY

        _HISTORY.reset(self._history_token)
        self._snapshot.close()
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
        identifiers = (Identifier(channel=channel, kind=kind, value=value), *extra)
        supported = [
            item for item in identifiers
            if item.channel == "whatsapp" and item.kind in {"phone_jid", "lid"}
        ]
        prior = next((self._identifier_people.get((item.channel, item.kind, item.value))
                      for item in supported
                      if self._identifier_people.get((item.channel, item.kind, item.value))), None)
        if prior is not None:
            row = self.queries.contact(prior)
            return PersonResolution("resolved", prior, row["display_name"] if row else name, 0, "history_identifier")
        self._observation_counter += 1
        person_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{WORKSPACE_ID}:person:{self._observation_counter}"))
        display_name = name or next((item.value for item in supported), person_id)
        self._history_db.execute(
            "INSERT INTO contacts(contact_id,kind,role,display_name,status,merged_into,source_refs) "
            "VALUES (?,'person',NULL,?,'confirmed',NULL,'[]')",
            (person_id, display_name),
        )
        for item in supported:
            kind = "pn_jid" if item.kind == "phone_jid" else "lid"
            mapping_proven = kind == "pn_jid" or mapping
            self._history_db.execute(
                "INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,first_seen_ms,last_seen_ms,ended_ms,valid_from_ms,valid_until_ms,source_refs) "
                "VALUES (?,'whatsapp',?,?,?,'observed',?,?,NULL,0,NULL,'[]')",
                (person_id, kind, item.value, "strong" if mapping_proven else "weak",
                 self.clock.now_ms(), self.clock.now_ms()),
            )
            self._identifier_people[(item.channel, item.kind, item.value)] = person_id
        self._history_db.commit()
        if supported:
            principal = self._principal_for_identifiers(tuple(supported), person_id)
            self._principals[person_id] = principal
            status = "resolved"
            reason = "history_identifier"
        else:
            status = "unresolved"
            reason = "no_history_identifier"
            person_id = None
        return PersonResolution(status, person_id, display_name if person_id else None, 0, reason)

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
        event_id = f"event-{self._source_counter}"
        occurred_ms = int(when_ms if when_ms is not None else self.clock.now_ms())
        source = SourceRef(event_id, int(revision), channel, chat, principal, occurred_ms)
        if channel != "whatsapp" or unknown_audience:
            self.authority.issue_source(
                source, EvidenceAudience.unknown() if unknown_audience else EvidenceAudience.known(members)
            )
            return source
        person_id = author_person
        phone = principal.split(":", 1)[-1]
        identifier = f"{phone}@s.whatsapp.net"
        self._history_db.execute(
            "INSERT INTO messages(message_id,channel,chat_id,native_message_id,sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,text,media_json,reply_to_native_id,mentions_json,provenance,source_refs) "
            "VALUES (?,'whatsapp',?,?,?,?,'native_identifier','in',?,'native','Synthetic source',NULL,NULL,NULL,'native','[]')",
            (event_id, chat, event_id, person_id, identifier, occurred_ms),
        )
        self._audiences[event_id] = (
            EvidenceAudience.author_only(snapshot_id=f"snap-{event_id}")
            if author_only else EvidenceAudience.known(members, snapshot_id=f"snap-{event_id}")
        )
        self._history_db.commit()
        return self.history_sources.issue(event_id)

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
        now_ms: int | None = None,
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
            now_ms=self.clock.now_ms() if now_ms is None else int(now_ms),
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
