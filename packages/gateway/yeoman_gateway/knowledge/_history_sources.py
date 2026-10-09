"""Immutable source issuance and fail-closed compatibility, owned by Knowledge."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping

from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.authority import EvidenceAudience, SourceAuthority
from yeoman_gateway.knowledge.models import LEGACY_NODE_PREFIX, KnowledgeError, SourceRef
from yeoman_gateway.policy.identity import canonical_user_id


@dataclass(frozen=True)
class HistorySourceAlias:
    issued: SourceRef
    message_id: str
    revision: int
    author_contact_id: str
    content_fingerprint: str
    audience: EvidenceAudience


@dataclass(frozen=True)
class HistorySourceRecord:
    source: SourceRef
    content_fingerprint: str
    author_contact_id: str
    audience: EvidenceAudience
    revoked: bool


def _audience_json(audience: EvidenceAudience) -> str:
    values = asdict(audience)
    values['members'] = sorted(audience.members)
    values['allowed'] = sorted(audience.allowed)
    return json.dumps(values, sort_keys=True, separators=(',', ':'))


def _audience(payload: str) -> EvidenceAudience:
    values = json.loads(payload)
    values['members'] = frozenset(values['members'])
    values['allowed'] = frozenset(values['allowed'])
    return EvidenceAudience(**values)


class HistorySourceLedger:
    def __init__(self, store: KnowledgeStore):
        if store.schema_version != 3:
            raise KnowledgeError('schema_incompatible', 'history ledger requires schema 3')
        self.store = store

    @staticmethod
    def _record(row: Any) -> HistorySourceRecord | None:
        if row is None:
            return None
        return HistorySourceRecord(SourceRef(**json.loads(row['source_json'])),
                                   row['content_fingerprint'], row['author_contact_id'],
                                   _audience(row['audience_json']), bool(row['revoked']))

    def lookup(self, event_id: str, revision: int) -> HistorySourceRecord | None:
        return self._record(self.store.query_one(
            'SELECT * FROM knowledge_history_sources WHERE event_id=? AND revision=?', (event_id, revision)))

    def current(self, message_id: str) -> HistorySourceRecord | None:
        return self._record(self.store.query_one(
            'SELECT * FROM knowledge_history_sources WHERE event_id=? ORDER BY revision DESC LIMIT 1', (message_id,)))

    def issue(self, *, message_id: str, content_fingerprint: str, channel: str, chat_id: str,
              author_principal: str, occurred_at_ms: int, author_contact_id: str,
              audience: EvidenceAudience) -> SourceRef:
        with self.store.transaction(write=True):
            previous = self.current(message_id)
            if previous is not None and previous.content_fingerprint == content_fingerprint:
                if previous.revoked:
                    raise KnowledgeError('denied_unknown_basis', 'source permanently revoked')
                return previous.source
            revision = 1 if previous is None else previous.source.revision + 1
            source = SourceRef(message_id, revision, channel, chat_id, author_principal, occurred_at_ms)
            self.store.execute("UPDATE knowledge_history_sources SET revoked=1,reason=COALESCE(reason,'superseded')"
                               ' WHERE event_id=?', (message_id,))
            self._insert(HistorySourceRecord(source, content_fingerprint, author_contact_id, audience, False))
            return source

    def _insert(self, record: HistorySourceRecord) -> None:
        self.store.execute('INSERT OR IGNORE INTO knowledge_history_sources VALUES (?,?,?,?,?,?,?,?)',
                           (*record.source.key, json.dumps(asdict(record.source), sort_keys=True),
                            record.content_fingerprint, record.author_contact_id,
                            _audience_json(record.audience), int(record.revoked), None))

    def revoke(self, event_id: str, revision: int, *, reason: str) -> None:
        with self.store.transaction(write=True):
            self.store.execute('UPDATE knowledge_history_sources SET revoked=1,reason=COALESCE(reason,?)'
                               ' WHERE event_id=? AND revision=?', (reason, event_id, revision))

    def persist_aliases(self, aliases: Mapping[tuple[str, int], HistorySourceAlias]) -> None:
        with self.store.transaction(write=True):
            for alias in aliases.values():
                self.store.execute('INSERT OR IGNORE INTO knowledge_history_source_aliases VALUES (?,?,?,?,?,?,?,?)',
                                   (*alias.issued.key, json.dumps(asdict(alias.issued), sort_keys=True),
                                    alias.message_id, alias.revision, alias.author_contact_id,
                                    alias.content_fingerprint, _audience_json(alias.audience)))
                self._insert(HistorySourceRecord(alias.issued, alias.content_fingerprint,
                                                alias.author_contact_id, alias.audience, False))

    def alias(self, key: tuple[str, int]) -> HistorySourceAlias | None:
        row = self.store.query_one('SELECT * FROM knowledge_history_source_aliases WHERE event_id=? AND revision=?', key)
        if row is None:
            return None
        return HistorySourceAlias(SourceRef(**json.loads(row['issued_json'])), row['message_id'],
                                  row['message_revision'], row['author_contact_id'],
                                  row['content_fingerprint'], _audience(row['audience_json']))


def is_legacy_node(event_id: str) -> bool:
    # Share the namespace with both legacy memory-node issuers.
    return isinstance(event_id, str) and event_id.startswith(LEGACY_NODE_PREFIX)


def legacy_node_ref(row: Mapping[str, Any]) -> SourceRef:
    return SourceRef(**{key: row[key] for key in SourceRef.__dataclass_fields__})


def _principal(value: str) -> str | None:
    ident = classify(value)
    if ident is None or ident.kind not in ('pn_jid', 'lid'):
        return None
    if ident.kind == 'pn_jid':
        return canonical_user_id('whatsapp', metadata={'sender_phone_jid': ident.value}) or None
    return f'whatsapp:{ident.value}'


def principal_identifier(principal: str) -> str | None:
    channel, _, value = principal.partition(':')
    if channel != 'whatsapp' or not value:
        return None
    if '@' not in value:
        value += '@s.whatsapp.net'
    return value if _principal(value) == principal else None


def _proof(queries: HistoryQueries, message_id: str) -> tuple[dict[str, Any], str, str, EvidenceAudience] | None:
    row = queries.message(message_id)
    if (row is None or row['sent_ms'] is None or row['time_certainty'] not in ('native', 'provider_timestamp')
            or row['direction'] != 'in' or row['sender_basis'] not in ('native_identifier', 'owner_attested')):
        return None
    principal = _principal(row['sender_identifier'] or '')
    contact = queries.resolve_identifier(row['sender_identifier'] or '', at_ms=row['sent_ms'], time_basis=row['time_certainty'])
    if (principal is None or contact is None or row['sender_contact_id'] is None
            or queries.terminal(row['sender_contact_id']) != contact):
        return None
    audience = queries.audience(message_id)
    if audience.status == 'unknown':
        return None
    return row, principal, contact, audience


class HistoryKnowledgeSources:
    def __init__(self, queries: HistoryQueries, compatibility: Mapping[tuple[str, int], HistorySourceAlias],
                 ledger: HistorySourceLedger, legacy_authority: SourceAuthority):
        self.queries, self.compatibility = queries, compatibility
        self.ledger, self.legacy_authority = ledger, legacy_authority
        self._verification: dict[tuple[str, int], tuple[Any, SourceRef | None, EvidenceAudience | None, str | None]] = {}

    def issue(self, message_id: str) -> SourceRef:
        self._verification.clear()
        proof = _proof(self.queries, message_id)
        if proof is None:
            raise KnowledgeError('denied_unknown_basis', 'history source lacks temporal attribution/audience proof')
        row, principal, contact, audience = proof
        fingerprint = self.queries.content_fingerprint(message_id)
        assert fingerprint is not None
        previous = self.ledger.current(message_id)
        if previous is not None and previous.content_fingerprint == fingerprint:
            if self.verify_source_ref(*previous.source.key) is None:
                self.ledger.revoke(*previous.source.key, reason='proof_changed')
                raise KnowledgeError('denied_unknown_basis', 'source proof changed')
        return self.ledger.issue(message_id=message_id, content_fingerprint=fingerprint,
                                 channel=row['channel'], chat_id=row['chat_id'], author_principal=principal,
                                 occurred_at_ms=row['sent_ms'], author_contact_id=contact, audience=audience)

    def _alias(self, key: tuple[str, int]) -> HistorySourceAlias | None:
        # Only the verified offline import publishes mappings; reads never publish them.
        persisted = self.ledger.alias(key)
        expected = self.compatibility.get(key)
        return persisted if expected is None or expected == persisted else None

    def _node_source(self, event_id: str, revision: int) -> SourceRef | None:
        record = self.ledger.lookup(event_id, revision)
        if record is not None and record.revoked:
            return None
        rows = self.ledger.store.query(
            "SELECT * FROM knowledge_statement_sources WHERE event_id=? AND revision=? AND status IN ('active','unknown')",
            (event_id, revision))
        try:
            refs = {legacy_node_ref(row) for row in rows}
        except (KnowledgeError, TypeError, ValueError):
            return None
        return next(iter(refs)) if len(refs) == 1 else None

    def verify_source_ref(self, event_id: str, revision: int) -> SourceRef | None:
        if is_legacy_node(event_id):
            return self._node_source(event_id, revision)
        key = event_id, revision
        self.queries.snapshot.assert_current(self.queries.snapshot.generation)
        record = self.ledger.lookup(*key)
        alias = self._alias(key)
        source = alias.issued if alias else record.source if record else None
        if source is None or source.channel != 'whatsapp':
            return self._verify_source_ref(event_id, revision, record, alias, None)
        state = (record, alias, self.queries.snapshot.generation,
                 self.queries.snapshot.connection.total_changes)
        cached = self._verification.get(key)
        if cached is not None and cached[0] == state:
            return cached[1]
        self._verification.pop(key, None)
        source = self._verify_source_ref(event_id, revision, record, alias, state)
        if key not in self._verification:
            self._verification[key] = (state, source, None, None)
        return source

    def _verify_source_ref(self, event_id: str, revision: int,
                           record: HistorySourceRecord | None, alias: HistorySourceAlias | None,
                           state: Any) -> SourceRef | None:
        key = event_id, revision
        if record is None and alias is None:
            legacy = getattr(self.legacy_authority, 'verify_source_ref')(event_id, revision)
            return legacy if legacy is not None and legacy.channel != 'whatsapp' else None
        source = alias.issued if alias is not None else record.source
        if source.channel != 'whatsapp':
            return getattr(self.legacy_authority, 'verify_source_ref')(event_id, revision)
        if record is not None and (record.revoked or record.source != source):
            return None
        message_id = alias.message_id if alias else source.event_id
        fingerprint = alias.content_fingerprint if alias else record.content_fingerprint
        owner = alias.author_contact_id if alias else record.author_contact_id
        audience = alias.audience if alias else record.audience
        proof = _proof(self.queries, message_id)
        if proof is None or fingerprint != self.queries.content_fingerprint(message_id):
            return None
        row, principal, contact, historical = proof
        if (source.author_principal != principal or source.occurred_at_ms != row['sent_ms']
                or source.chat_id != row['chat_id'] or self.queries.terminal(owner) != contact):
            return None
        if audience.status == 'unknown':
            return None
        if audience.status == 'known':
            if historical.status != 'known':
                return None
            audience = replace(audience, members=audience.members & historical.members)
        self._verification[key] = (state, source, audience, contact)
        return source

    def verify_source(self, source: SourceRef) -> bool:
        if is_legacy_node(source.event_id):
            return self._node_source(*source.key) == source
        if source.channel != 'whatsapp':
            return self.legacy_authority.verify_source(source)
        return self.verify_source_ref(*source.key) == source

    def source_revoked(self, source: SourceRef) -> bool:
        if is_legacy_node(source.event_id):
            return not self.verify_source(source)
        if source.channel != 'whatsapp':
            return self.legacy_authority.source_revoked(source)
        return not self.verify_source(source)

    def mark_source_revoked(self, source: SourceRef) -> None:
        self._verification.clear()
        if source.channel != 'whatsapp' and not is_legacy_node(source.event_id):
            self.legacy_authority.mark_source_revoked(source)
            return
        with self.ledger.store.transaction(write=True):
            alias = self._alias(source.key)
            if alias is not None:
                self.ledger.persist_aliases({source.key: alias})
            if self.ledger.lookup(*source.key) is None:
                self.ledger._insert(HistorySourceRecord(source, '', '', EvidenceAudience.unknown(), True))
            self.ledger.revoke(*source.key, reason='explicit_revocation')

    def evidence_audience(self, source: SourceRef, *, basis: str) -> EvidenceAudience | None:
        if is_legacy_node(source.event_id):
            if not self.verify_source(source):
                return None
            rows = self.ledger.store.query(
                "SELECT source_audience_json FROM knowledge_statement_sources WHERE event_id=? AND revision=? AND status IN ('active','unknown')",
                source.key)
            values = {row['source_audience_json'] for row in rows}
            if len(values) != 1:
                return None
            value = next(iter(values))
            return EvidenceAudience.author_only() if value is None else EvidenceAudience.known(set(json.loads(value)))
        if source.channel != 'whatsapp':
            return self.legacy_authority.evidence_audience(source, basis=basis)
        if not self.verify_source(source):
            return None
        return self._verification[source.key][2]

    def register_source(self, source: SourceRef, audience: EvidenceAudience) -> SourceRef:
        self._verification.clear()
        if is_legacy_node(source.event_id):
            if not self.verify_source(source):
                raise KnowledgeError('denied_unknown_basis', 'legacy note authority missing')
            return source
        if source.channel == 'whatsapp':
            issued = self.issue(source.event_id)
            if issued != source:
                raise KnowledgeError('denied_unknown_basis', 'source registration provenance mismatch')
            return issued
        return getattr(self.legacy_authority, 'register_source')(source, audience)

    def verify_observation(self, observation: Any) -> str:
        if getattr(observation, 'channel', 'whatsapp') != 'whatsapp':
            return self.legacy_authority.verify_observation(observation)
        raise KnowledgeError('history_identity_read_only', 'use the local owner-attestation CLI')

    def verify_evidence_ref(self, evidence_ref: str) -> str:
        return self.legacy_authority.verify_evidence_ref(evidence_ref)

    def author_contact(self, source: SourceRef) -> str | None:
        if is_legacy_node(source.event_id):
            return None
        if not self.verify_source(source):
            return None
        return self._verification[source.key][3]

    def permits_principal(self, source: SourceRef, principal: str, *, now_ms: int) -> bool:
        if is_legacy_node(source.event_id):
            audience = self.evidence_audience(source, basis='')
            return audience is not None and (
                (audience.status == 'author_only' and principal == source.author_principal)
                or (audience.status == 'known' and principal in audience.members))
        if source.channel != 'whatsapp':
            return self.legacy_authority.verify_source(source) and not self.legacy_authority.source_revoked(source)
        if not self.verify_source(source):
            return False
        value = principal_identifier(principal)
        if value is None:
            return False
        before = self.queries.resolve_identifier(value, at_ms=source.occurred_at_ms, time_basis='native')
        current = self.queries.resolve_identifier(value, at_ms=now_ms, time_basis='native')
        if before is None or current != before:
            return False
        audience = self.evidence_audience(source, basis='')
        if audience is None or (audience.status == 'known' and principal not in audience.members):
            return False
        if principal == source.author_principal:
            return before == self.author_contact(source)
        return audience.status == 'known' and principal in audience.members


def build_history_source_aliases(*, queries: HistoryQueries, legacy_rows: Iterable[Mapping[str, Any]],
                                 locators: Mapping[tuple[str, int], tuple[str, ...]]) -> tuple[dict[tuple[str, int], HistorySourceAlias], dict[str, int]]:
    aliases: dict[tuple[str, int], HistorySourceAlias] = {}
    blocked: set[tuple[str, int]] = set()
    counts = Counter(mapped=0, ambiguous=0, missing=0, revoked=0, native=0, event=0, revision=0)
    for row in legacy_rows:
        if is_legacy_node(row.get('event_id', '')):
            counts['legacy_node'] += 1
            continue
        try:
            source = SourceRef(**{key: row[key] for key in SourceRef.__dataclass_fields__})
        except (KnowledgeError, KeyError, TypeError, ValueError):
            counts['missing'] += 1
            continue
        if source.key in blocked:
            counts['ambiguous'] += 1
            continue
        counts['event'] += 1
        counts['native'] += bool(row.get('native_id'))
        counts['revision'] += 1
        if row.get('status') == 'revoked':
            counts['revoked'] += 1
            blocked.add(source.key)
            if aliases.pop(source.key, None) is not None:
                counts['mapped'] -= 1
            continue
        targets = locators.get(source.key, ())
        if len(targets) > 1:
            counts['ambiguous'] += 1
            continue
        if not targets:
            counts['missing'] += 1
            continue
        proof = _proof(queries, targets[0])
        fingerprint = queries.content_fingerprint(targets[0])
        owner = row.get('author_contact_id')
        if (proof is None or not owner or queries.terminal(str(owner)) != proof[2]
                or fingerprint != row.get('content_fingerprint') or source.author_principal != proof[1]
                or source.chat_id != proof[0]['chat_id'] or source.occurred_at_ms != proof[0]['sent_ms']):
            counts['missing'] += 1
            continue
        members = row.get('source_audience_json')
        try:
            audience = (EvidenceAudience.author_only(snapshot_id=row.get('snapshot_id')) if members is None
                        else EvidenceAudience.known(set(json.loads(members)), snapshot_id=row.get('snapshot_id')))
        except (TypeError, ValueError):
            counts['missing'] += 1
            continue
        if audience.status == 'known' and (proof[3].status != 'known' or audience.members != proof[3].members):
            counts['missing'] += 1
            continue
        alias = HistorySourceAlias(source, targets[0], int(row.get('message_revision', 1)), str(owner), fingerprint, audience)
        if source.key in aliases and aliases[source.key] != alias:
            aliases.pop(source.key)
            blocked.add(source.key)
            counts['mapped'] -= 1
            counts['ambiguous'] += 1
            continue
        aliases[source.key] = alias
        counts['mapped'] += 1
    return aliases, dict(counts)
