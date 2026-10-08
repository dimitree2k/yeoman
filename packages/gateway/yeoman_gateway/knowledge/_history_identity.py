"""Snapshot identity delegation; statement roles stay in Knowledge."""
from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterable

from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources, principal_identifier
from yeoman_gateway.knowledge._identity import IdentityEngine, PersonRow
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.authority import PolicyAuthority, SourceAuthority
from yeoman_gateway.knowledge.models import (
    EndpointResolution,
    Identifier,
    IdentifierBinding,
    KnowledgeError,
    NameObservation,
    PersonResolution,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)


@dataclass(frozen=True)
class HistoryKnowledgeScope:
    store: KnowledgeStore
    queries: HistoryQueries
    sources: HistoryKnowledgeSources
    identity: HistoryIdentityEngine


_scopes: ContextVar[tuple[HistoryKnowledgeScope, ...]] = ContextVar('knowledge_history_scopes', default=())


def current_history_scope(store: KnowledgeStore | None = None) -> HistoryKnowledgeScope | None:
    return next((scope for scope in reversed(_scopes.get()) if store is None or scope.store is store), None)


class ScopedKnowledgeAdapter:
    """Stable engine references dispatch through the current task/thread scope."""
    def __init__(self, store: KnowledgeStore, legacy: Any, kind: str, *, selected: bool):
        self.store, self.legacy, self.kind, self.selected = store, legacy, kind, selected

    def __getattr__(self, name: str) -> Any:
        scope = current_history_scope(self.store)
        if scope is not None:
            adapter = scope.identity if self.kind == 'identity' else scope.sources
            return getattr(adapter, name)
        if self.selected:
            if self.kind == 'identity' and name in IdentityEngine.__dict__ and name not in HistoryIdentityEngine.__dict__ and name not in HistoryIdentityEngine._inherited_reads:
                raise KnowledgeError('history_identity_read_only', 'use the local owner-attestation CLI')
            raise HistoryPaused('knowledge_history_scope_required')
        return getattr(self.legacy, name)


class HistoryIdentityEngine(IdentityEngine):
    # Only these base reads operate entirely on statement-role rows or overridden reads.
    _inherited_reads = frozenset({
        'require_person', 'canonical_ids', 'people_for_statement', 'person_ids_for_statements',
        'active_bindings_of', 'searchable_aliases_of', 'address_aliases_of', 'neutral_address',
        'search_mention_name_candidates', 'search_mention_text_candidates',
        'person_by_display_or_alias',
    })

    def __init__(self, store: KnowledgeStore, *, queries: HistoryQueries,
                 authority: SourceAuthority, policy: PolicyAuthority):
        super().__init__(store, authority=authority, policy=policy)
        self.queries = queries

    def __getattribute__(self, name: str) -> Any:
        if (not name.startswith('_') and name in IdentityEngine.__dict__
                and name not in HistoryIdentityEngine.__dict__ and name not in self._inherited_reads):
            def refused(*args: Any, **kwargs: Any) -> Any:
                raise KnowledgeError('history_identity_read_only', 'use the local owner-attestation CLI')
            return refused
        return super().__getattribute__(name)

    def get_person(self, person_id: str) -> PersonRow | None:
        row = self.queries.contact(person_id)
        if row is None or row['kind'] != 'person':
            return None
        return PersonRow(row['contact_id'], row['display_name'] or '', self.queries.snapshot.generation,
                         'active', None, None, 'public')

    def canonical_id(self, person_id: str, *, skip_ops: frozenset[str] = frozenset()) -> str:
        terminal = self.queries.terminal(person_id)
        if terminal is None:
            raise KnowledgeError('unresolved', 'unknown history contact')
        return terminal

    def merged_member_ids(self, person_id: str) -> tuple[str, ...]:
        terminal = self.canonical_id(person_id)
        return tuple(row['contact_id'] for row in self.queries._rows(
            'WITH RECURSIVE members(contact_id) AS (SELECT ? UNION'
            ' SELECT c.contact_id FROM contacts c JOIN members m ON c.merged_into=m.contact_id)'
            ' SELECT contact_id FROM members ORDER BY contact_id', (terminal,)))

    def person_ids_for_query(self, person_ids: tuple[str, ...], roles: tuple[str, ...]) -> tuple[str, ...]:
        canonical = self.canonical_ids(person_ids)
        if not roles:
            return canonical
        rows = self._store.query('SELECT DISTINCT person_id FROM knowledge_statement_people'
                                 ' WHERE status=\'active\' AND role IN (SELECT value FROM json_each(?))',
                                 (json.dumps(roles),))
        allowed = {self.queries.terminal(row['person_id']) for row in rows}
        return tuple(person for person in canonical if person in allowed)

    def _binding(self, row: dict[str, Any]) -> IdentifierBinding | None:
        owner = self.queries.terminal(row['contact_id'])
        if owner is None or row['kind'] not in ('pn_jid', 'lid') or row['strength'] != 'strong':
            return None
        # There is no legacy binding_id for a history contact. Never forge an FK.
        return IdentifierBinding(owner, Identifier('whatsapp', 'phone_jid' if row['kind'] == 'pn_jid' else 'lid',
                                                   row['value'], namespace='whatsapp'),
                                 evidence_ref=f"history:identifier:{row['id']}",
                                 status='active' if row['valid_until_ms'] is None else 'ended',
                                 mapping_verified=True, valid_from_ms=max(0, row['valid_from_ms'] or 0),
                                 valid_until_ms=max(0, row['valid_until_ms'] or 0),
                                 observed_at_ms=max(0, row['first_seen_ms'] or 0),
                                 revision=self.queries.snapshot.generation)

    def bindings_of(self, person_id: str) -> tuple[IdentifierBinding, ...]:
        members = self.merged_member_ids(person_id)
        rows = self.queries._rows('SELECT * FROM identifier_history WHERE channel=\'whatsapp\''
                                  ' AND contact_id IN (SELECT value FROM json_each(?))'
                                  ' ORDER BY kind,value,valid_from_ms,id', (json.dumps(members),))
        return tuple(binding for row in rows if (binding := self._binding(row)) is not None)

    def binding_at(self, identifier: Identifier, at_ms: int) -> IdentifierBinding | None:
        if identifier.channel != 'whatsapp' or identifier.namespace not in ('whatsapp', 'default'):
            return None
        ident = classify(identifier.value)
        expected = 'pn_jid' if identifier.kind == 'phone_jid' else identifier.kind
        if ident is None or ident.kind != expected:
            return None
        owner = self.queries.resolve_identifier(identifier.value, at_ms=at_ms, time_basis='native')
        if owner is None:
            return None
        bindings = [binding for binding in self.bindings_of(owner)
                    if binding.identifier.value == identifier.value and binding.covers(at_ms)]
        return bindings[0] if len(bindings) == 1 else None

    def binding_for(self, identifier: Identifier) -> IdentifierBinding | None:
        resolution = self.resolve_identifier(identifier)
        if resolution.person_id is None:
            return None
        bindings = [binding for binding in self.active_bindings_of(resolution.person_id)
                    if binding.identifier.value == identifier.value]
        return bindings[0] if len(bindings) == 1 else None

    def binding_by_id(self, binding_id: str) -> IdentifierBinding | None:
        return None

    def resolve_identifier(self, identifier: Identifier, *, at_ms: int | None = None) -> EndpointResolution:
        ident = classify(identifier.value)
        expected = 'pn_jid' if identifier.kind == 'phone_jid' else identifier.kind
        owner = None
        if (identifier.channel == 'whatsapp' and identifier.namespace in ('whatsapp', 'default')
                and ident is not None and ident.kind == expected):
            if at_ms is None:
                rows = self.queries._rows("SELECT contact_id FROM identifier_history WHERE channel='whatsapp'"
                                          " AND kind=? AND value=? AND strength='strong' AND valid_until_ms IS NULL",
                                          (expected, identifier.value))
                owners = {self.queries.terminal(row['contact_id']) for row in rows}
                owner = next(iter(owners)) if len(owners) == 1 and None not in owners else None
            else:
                owner = self.queries.resolve_identifier(identifier.value, at_ms=at_ms, time_basis='native')
        return EndpointResolution('resolved' if owner else 'unresolved', owner,
                                  identifier if owner else None, self.queries.snapshot.generation,
                                  'history_temporal_binding' if owner else 'no_proven_binding_for_identifier')

    def person_id_for_principal(self, principal: str) -> str | None:
        value = principal_identifier(principal)
        if value is None:
            return None
        ident = classify(value)
        if ident is None:
            return None
        return self.resolve_identifier(Identifier('whatsapp', 'phone_jid' if ident.kind == 'pn_jid' else ident.kind,
                                                  value, namespace='whatsapp')).person_id

    def display_name(self, person_id: str, *, context: TrustedReadContext | None = None,
                     for_group: bool = False) -> str | None:
        person = self.get_person(person_id)
        return person.display_name or None if person else None

    def aliases_of(self, person_id: str) -> tuple[NameObservation, ...]:
        canonical = self.canonical_id(person_id)
        members = self.merged_member_ids(person_id)
        names = {self.display_name(person_id)}
        names.update(row['value'] for row in self.queries._rows(
            "SELECT value FROM identifier_history WHERE kind='push_name'"
            " AND contact_id IN (SELECT value FROM json_each(?)) AND valid_until_ms IS NULL",
            (json.dumps(members),)))
        rows = self._store.query("SELECT * FROM contact_aliases"
                                 " WHERE contact_id IN (SELECT value FROM json_each(?))",
                                 (json.dumps(members),))
        # Frozen rows supply only curation/release controls for names proven in this snapshot.
        curated = [replace(self._row_to_alias(row), person_id=canonical)
                   for row in rows if row['alias'] in names]
        controlled = {alias.name for alias in curated}
        observed = [NameObservation(canonical, name, 'history', alias_kind='platform_display',
                                    normalized_alias=' '.join(name.casefold().split()))
                    for name in sorted(name for name in names if name and name not in controlled)]
        return tuple(curated + observed)

    def aliases_of_many(self, person_ids: tuple[str, ...]) -> dict[str, tuple[NameObservation, ...]]:
        return {person: self.aliases_of(person) for person in person_ids}

    def alias_by_id(self, alias_id: int) -> NameObservation | None:
        return None

    def search_by_name(self, name: str, *, context: TrustedReadContext | None = None,
                       limit: int = 25) -> tuple[PersonResolution, ...]:
        if context is None:
            return ()
        people = tuple(person for person, _ in self.eligible_people_for_context(context))
        return self.search_mention_name_candidates(name, person_ids=people, context=context)[:limit]

    def eligible_people_for_context(self, context: TrustedReadContext, *,
                                    limit: int = 200) -> tuple[tuple[str, str | None], ...]:
        membership = self._policy.membership(context)
        if (membership is None or context.principal_id not in membership.members
                or context.recipient_principals is None
                or not context.recipient_principals <= membership.members):
            return ()
        out: dict[str, str | None] = {}
        for principal in sorted(context.recipient_principals):
            value = principal_identifier(principal)
            owner = self.queries.resolve_identifier(value, at_ms=context.now_ms, time_basis='native') if value else None
            if owner:
                out[owner] = self.display_name(owner, context=context)
        return tuple(out.items())[:limit]

    def eligible_people_for_source(self, candidate: StatementCandidate,
                                   context: TrustedCaptureContext) -> tuple[str, ...]:
        out = set()
        for source in candidate.sources:
            if not self._authority.verify_source(source):
                continue
            audience = self._authority.evidence_audience(source, basis=context.capture_basis)
            principals = {source.author_principal} | set(audience.members if audience else ())
            for principal in principals:
                value = principal_identifier(principal)
                owner = self.queries.resolve_identifier(value, at_ms=source.occurred_at_ms, time_basis='native') if value else None
                if owner:
                    out.add(owner)
        return tuple(sorted(out))

    def owners_of_identifier_value(self, value: str, *, channel: str | None = None) -> tuple[str, ...]:
        if channel not in (None, 'whatsapp'):
            return ()
        ident = classify(value)
        if ident is None:
            return ()
        owner = self.resolve_identifier(Identifier('whatsapp', 'phone_jid' if ident.kind == 'pn_jid' else ident.kind,
                                                   value, namespace='whatsapp')).person_id
        return (owner,) if owner else ()

    def resolve_endpoint(self, person_id: str, channel: str, *, prefer_kind: str | None = None,
                         at_ms: int | None = None) -> EndpointResolution:
        candidates = list(dict.fromkeys(binding.identifier for binding in self.bindings_of(person_id)
                      if binding.identifier.channel == channel
                      and (binding.status == 'active' if at_ms is None else binding.covers(at_ms))
                      and self.resolve_identifier(binding.identifier, at_ms=at_ms).person_id == self.canonical_id(person_id)))
        if prefer_kind is not None:
            preferred = [ident for ident in candidates if ident.kind == prefer_kind]
            if preferred:
                candidates = preferred
        resolved = len(candidates) == 1
        return EndpointResolution('resolved' if resolved else 'unresolved',
                                  self.canonical_id(person_id) if resolved else None,
                                  candidates[0] if resolved else None, self.queries.snapshot.generation,
                                  'history_endpoint' if resolved else 'no_unique_delivery_endpoint')

    def delivery_identifiers_for_alias(self, alias: str, *, channel: str,
                                       scope_key: str | None = None) -> tuple[Identifier, ...]:
        rows = self._store.query("SELECT DISTINCT contact_id FROM contact_aliases"
                                 " WHERE alias=? COLLATE NOCASE AND address_allowed=1"
                                 " AND status IN ('observed','confirmed') AND mapping_retracted=0"
                                 " AND valid_until_ms IS NULL", (alias,))
        identifiers = set()
        for row in rows:
            terminal = self.queries.terminal(row['contact_id'])
            if terminal is None:
                continue
            if not any(name.name.casefold() == alias.casefold()
                       for name in self.address_aliases_of(terminal, scope_key=scope_key)):
                continue
            for binding in self.active_bindings_of(terminal):
                if (binding.identifier.channel == channel
                        and self.resolve_identifier(binding.identifier).person_id == terminal):
                    identifiers.add(binding.identifier)
        return tuple(sorted(identifiers, key=lambda item: (item.kind, item.value)))

    def provider_merge_protection_reason(self, person_ids: Iterable[str], *,
                                         additional_identifiers: Iterable[Identifier] = ()) -> str | None:
        return 'history_identity_read_only'
