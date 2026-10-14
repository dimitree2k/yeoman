"""Bounded, snapshot-local WhatsApp reads; identity never grants disclosure aliases."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from yeoman_gateway.knowledge import EvidenceAudience
from yeoman_gateway.policy.identity import canonical_user_id

from .ids import classify
from .live import HistoryPaused
from .reader import HistorySnapshot
from .resolve import _compatible

Row = dict[str, Any]
_ORDER = "sent_ms, message_id"
_VISIBLE = "channel='whatsapp' AND deleted=0"


@dataclass(frozen=True)
class PersonLookupCandidate:
    display_name: str
    group_names: tuple[str, ...]


@dataclass(frozen=True)
class PersonLookupResult:
    status: Literal['resolved', 'ambiguous', 'not_found']
    person_id: str | None = None
    candidates: tuple[PersonLookupCandidate, ...] = ()


def _name_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return tuple(re.findall(r"[^\W\d_]+|\d+", normalized, flags=re.UNICODE))


def _token_matches(query: str, token: str) -> bool:
    return token == query or (query.isalpha() and len(query) >= 3 and token.startswith(query))


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 500:
        raise ValueError("limit must be an integer from 1 to 500")


def _time(value: int | None) -> None:
    if value is not None and type(value) is not int:
        raise ValueError("time must be integer milliseconds or None")


def _principal_identifiers(principal: str) -> tuple[str, ...]:
    if not isinstance(principal, str):
        return ()
    value = principal.removeprefix('whatsapp:')
    if value.isascii() and value.isdigit():
        return (value + '@s.whatsapp.net', value + '@lid')
    ident = classify(value)
    if ident and ident.value == value and ident.kind in ('pn_jid', 'lid'):
        number = ident.value.split('@', 1)[0]
        if number.isascii() and number.isdigit():
            return (ident.value,)
    return ()


class HistoryQueries:
    def __init__(self, snapshot: HistorySnapshot):
        self.snapshot = snapshot
        self._audiences: dict[str, EvidenceAudience] = {}
        self._audience_state = (snapshot.generation, snapshot.connection.total_changes)
        self._person_names_cache: dict[tuple[tuple[str, ...], int], dict[str, set[str]]] = {}
        self._person_names_state = self._audience_state

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Row]:
        self.snapshot.assert_current(self.snapshot.generation)
        cursor = self.snapshot.connection.execute(sql, parameters)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor]

    def content_fingerprint(self, message_id: str) -> str | None:
        rows = self._rows("SELECT * FROM messages_current WHERE message_id=? AND channel='whatsapp'", (message_id,))
        if not rows:
            return None
        row = rows[0]
        fields = ("message_id", "channel", "chat_id", "native_message_id", "direction",
                  "sent_ms", "time_certainty", "text", "current_text", "media_json",
                  "reply_to_native_id", "mentions_json", "provenance", "deleted")
        payload = {key: row[key] for key in fields}
        for key in ("media_json", "mentions_json"):
            payload[key] = json.loads(payload[key]) if payload[key] is not None else None
        events = self._rows(
            "SELECT event_id,native_event_id,kind,occurred_ms,time_certainty,provenance,payload_json"
            " FROM message_events WHERE target_message_id=? AND kind IN ('edit','delete')"
            " ORDER BY occurred_ms,event_id", (message_id,))
        for event in events:
            event["payload_json"] = json.loads(event["payload_json"])
        payload["events"] = events
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()

    def window(self, *, chat_id: str, after_ms: int, before_ms: int, limit: int) -> list[Row]:
        _limit(limit)
        _time(after_ms)
        _time(before_ms)
        return self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=?"
            f" AND sent_ms>? AND sent_ms<? ORDER BY {_ORDER} LIMIT ?",
            (chat_id, after_ms, before_ms, limit),
        )

    def recent(self, *, chat_id: str, limit: int, before_id: str | None = None,
               after_ms: int | None = None, until_ms: int | None = None) -> list[Row]:
        _limit(limit)
        _time(after_ms)
        _time(until_ms)
        conditions, parameters = "", (chat_id,)
        if before_id is not None:
            anchor = self.message(before_id)
            if anchor is None or anchor["chat_id"] != chat_id:
                return []
            # SQLite NULL sorts first, separate from all timed messages.
            if anchor["sent_ms"] is None:
                conditions += " AND sent_ms IS NULL AND message_id<?"
                parameters += (before_id,)
            else:
                conditions += " AND (sent_ms IS NULL OR sent_ms<? OR (sent_ms=? AND message_id<?))"
                parameters += (anchor["sent_ms"], anchor["sent_ms"], before_id)
        if after_ms is not None:
            conditions += " AND sent_ms>?"
            parameters += (after_ms,)
        if until_ms is not None:
            # An as-of read: later rows must not take the newest-first limit.
            conditions += " AND sent_ms<=?"
            parameters += (until_ms,)
        rows = self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=?{conditions}"
            " ORDER BY sent_ms DESC, message_id DESC LIMIT ?", parameters + (limit,),
        )
        return rows[::-1]

    def reply_window(self, *, chat_id: str, native_id: str, before: int, after: int) -> list[Row]:
        if (type(before) is not int or type(after) is not int or before < 0 or after < 0
                or before + after + 1 > 500):
            raise ValueError("reply window must contain 1 to 500 rows")
        anchor = self.native_message(chat_id=chat_id, native_id=native_id)
        if anchor is None:
            return []
        earlier = self.recent(chat_id=chat_id, limit=before, before_id=anchor["message_id"]) if before else []
        if anchor["sent_ms"] is None:
            predicate = "((sent_ms IS NULL AND message_id>?) OR sent_ms IS NOT NULL)"
            parameters = (anchor["message_id"],)
        else:
            predicate = "(sent_ms>? OR (sent_ms=? AND message_id>?))"
            parameters = (anchor["sent_ms"], anchor["sent_ms"], anchor["message_id"])
        later = self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=? AND {predicate}"
            f" ORDER BY {_ORDER} LIMIT ?", (chat_id, *parameters, after),
        ) if after else []
        return [*earlier, anchor, *later]

    def search(self, *, chat_ids: tuple[str, ...], query: str, limit: int,
               after_ms: int | None = None, before_ms: int | None = None) -> list[Row]:
        _limit(limit)
        _time(after_ms)
        _time(before_ms)
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        if not chat_ids or not terms:
            return []
        match = " ".join(f'"{term}"*' for term in terms)
        conditions = ""
        parameters: tuple[Any, ...] = (*chat_ids, match)
        for operator, moment in ((">", after_ms), ("<", before_ms)):
            if moment is not None:
                conditions += f" AND sent_ms{operator}?"
                parameters += (moment,)
        return self._rows(
            f"SELECT m.* FROM messages_fts JOIN messages_current m"
            f" ON m.message_id=messages_fts.message_id WHERE {_VISIBLE}"
            f" AND m.chat_id IN ({','.join('?' for _ in chat_ids)})"
            f" AND messages_fts MATCH ?{conditions}"
            " ORDER BY bm25(messages_fts), sent_ms DESC, m.message_id LIMIT ?",
            (*parameters, limit),
        )

    def message(self, message_id: str) -> Row | None:
        rows = self._rows(f"SELECT * FROM messages_current WHERE {_VISIBLE} AND message_id=?", (message_id,))
        return rows[0] if rows else None

    def native_message(self, *, chat_id: str, native_id: str) -> Row | None:
        rows = self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=? AND native_message_id=?"
            " ORDER BY message_id LIMIT 2", (chat_id, native_id),
        )
        return rows[0] if len(rows) == 1 else None

    def chats(self) -> list[Row]:
        return self._rows("""
            SELECT 'whatsapp' AS channel, chat_id,
              (SELECT COALESCE(json_extract(payload_json,'$.value'), json_extract(payload_json,'$.subject')) FROM message_events e
               WHERE e.channel='whatsapp' AND e.chat_id=c.chat_id AND kind='group_subject'
               ORDER BY occurred_ms DESC,event_id DESC LIMIT 1) AS subject,
              (SELECT COALESCE(json_extract(payload_json,'$.value'), json_extract(payload_json,'$.description')) FROM message_events e
               WHERE e.channel='whatsapp' AND e.chat_id=c.chat_id AND kind='group_description'
               ORDER BY occurred_ms DESC,event_id DESC LIMIT 1) AS description
            FROM (SELECT chat_id FROM messages WHERE channel='whatsapp'
                  UNION SELECT chat_id FROM message_events WHERE channel='whatsapp') c ORDER BY chat_id
        """)

    def principal_contact(self, principal: str, at_ms: int) -> str | None:
        """Resolve live WhatsApp principal forms to one temporal terminal contact."""
        if at_ms is None:
            return None
        try:
            contacts = set()
            for value in _principal_identifiers(principal):
                contact = self.resolve_identifier(value, at_ms=at_ms, time_basis='native')
                if contact:
                    contacts.add(self.terminal(contact))
            return contacts.pop() if len(contacts) == 1 else None
        except HistoryPaused:
            return None

    def is_owner_direct_chat(self, principal: str, chat_id: str, at_ms: int, *,
                             is_owner: bool, channel: str = 'whatsapp') -> bool:
        """Require authenticated ownership and the same temporal direct-chat counterpart."""
        if not is_owner or channel != 'whatsapp':
            return False
        ident = classify(chat_id)
        if ident is None or ident.kind not in ('pn_jid', 'lid'):
            return False
        owner = self.principal_contact(principal, at_ms)
        if owner is None:
            return False
        try:
            counterpart = self.resolve_identifier(ident.value, at_ms=at_ms, time_basis='native')
            return counterpart is not None and self.terminal(counterpart) == owner
        except HistoryPaused:
            return False

    def owner_member_chat_ids(self, principal_id: str, now_ms: int) -> tuple[str, ...]:
        """Return group chats with a proven current roster containing this principal."""
        _time(now_ms)
        owner = self.principal_contact(principal_id, now_ms)
        if owner is None:
            return ()
        result = []
        for row in self.chats():
            chat_id = str(row['chat_id'])
            ident = classify(chat_id)
            if ident is None or ident.kind != 'group':
                continue
            audience = self.members(chat_id=chat_id, at_ms=now_ms)
            if audience.status == 'known' and any(
                self.principal_contact(member, now_ms) == owner for member in audience.members
            ):
                result.append(chat_id)
        return tuple(result)

    def resolve_person(self, name: str, chat_ids: tuple[str, ...], at_ms: int) -> PersonLookupResult:
        """Resolve eligible names, preferring full names and whole tokens over prefixes."""
        _time(at_ms)
        query = _name_tokens(name) if isinstance(name, str) else ()
        if not query or not chat_ids:
            return PersonLookupResult('not_found')
        people = self._eligible_person_names(chat_ids, at_ms)
        aliases = [(tokens, ids) for alias, ids in people.items()
                   if (tokens := _name_tokens(alias))]
        candidates: set[str] = set()
        for matches in (
            lambda tokens: tokens == query,
            lambda tokens: all(word in tokens for word in query),
            lambda tokens: all(any(_token_matches(word, token) for token in tokens) for word in query),
        ):
            candidates = {person for tokens, ids in aliases if matches(tokens) for person in ids}
            if candidates:
                break
        if len(candidates) == 1:
            return PersonLookupResult('resolved', next(iter(candidates)))
        if not candidates:
            return PersonLookupResult('not_found')
        groups = {str(row['chat_id']): str(row['subject'] or 'Unnamed group') for row in self.chats()}
        candidate_groups: dict[str, set[str]] = {person: set() for person in candidates}
        for chat_id in chat_ids:
            scoped = self._eligible_person_names((chat_id,), at_ms)
            for ids in scoped.values():
                for person in candidates.intersection(ids):
                    candidate_groups[person].add(groups.get(chat_id, 'Unnamed group'))
        choices = [PersonLookupCandidate(
            str((self.contact(person) or {}).get('display_name') or 'Unnamed person'),
            tuple(sorted(candidate_groups[person], key=str.casefold)),
        ) for person in candidates]
        choices.sort(key=lambda candidate: (candidate.display_name.casefold(), candidate.group_names))
        return PersonLookupResult('ambiguous', candidates=tuple(choices[:8]))

    def _eligible_person_names(self, chat_ids: tuple[str, ...], at_ms: int) -> dict[str, set[str]]:
        self.snapshot.assert_current(self.snapshot.generation)
        state = (self.snapshot.generation, self.snapshot.connection.total_changes)
        if state != self._person_names_state:
            self._person_names_cache.clear()
            self._person_names_state = state
        cache_key = (tuple(sorted(set(chat_ids))), at_ms)
        cached = self._person_names_cache.get(cache_key)
        if cached is not None:
            return cached
        # Only names of people proven in each selected current group roster are candidates.
        contacts: set[str] = set()
        for chat_id in cache_key[0]:
            roster = self.members(chat_id=chat_id, at_ms=at_ms)
            if roster.status != 'known':
                continue
            for principal in roster.members:
                channel, _, value = principal.partition(':')
                if channel != 'whatsapp' or not value:
                    continue
                ident = classify(value)
                if ident is None or ident.kind not in ('pn_jid', 'lid'):
                    ident = classify(value + '@s.whatsapp.net')
                if ident is None or ident.kind not in ('pn_jid', 'lid'):
                    continue
                contact_id = self.resolve_identifier(ident.value, at_ms=at_ms, time_basis='native')
                terminal = self.terminal(contact_id) if contact_id else None
                if terminal:
                    contacts.add(terminal)
        # The owner’s current membership authorizes the chat scope; a person need not
        # still be a current member to resolve their confirmed historical authorship.
        scoped_authors = self._rows(
            f"SELECT DISTINCT sender_contact_id FROM messages_current WHERE {_VISIBLE}"
            f" AND chat_id IN ({','.join('?' for _ in cache_key[0])})"
            " AND sender_contact_id IS NOT NULL"
            " AND sender_basis IN ('native_identifier','owner_attested')",
            cache_key[0],
        ) if cache_key[0] else []
        for row in scoped_authors:
            terminal = self.terminal(str(row['sender_contact_id']))
            if terminal:
                contacts.add(terminal)
        if not contacts:
            self._person_names_cache[cache_key] = {}
            return {}
        contact_rows = self._rows(
            "SELECT contact_id,kind,status,display_name,merged_into FROM contacts"
            " WHERE contact_id IN (SELECT value FROM json_each(?))"
            " OR merged_into IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(contacts)), json.dumps(sorted(contacts))),
        )
        eligible = {str(row['contact_id']) for row in contact_rows
                    if row['kind'] == 'person' and row['status'] == 'confirmed' and not row['merged_into']}
        if not eligible:
            self._person_names_cache[cache_key] = {}
            return {}
        names: dict[str, set[str]] = {}
        for row in contact_rows:
            terminal = self.terminal(str(row['contact_id']))
            if (terminal in eligible and row['kind'] == 'person' and row['status'] == 'confirmed'
                    and row['display_name']):
                alias = ' '.join(str(row['display_name']).casefold().split())
                names.setdefault(alias, set()).add(terminal)
        # Weak push-name observations are usable as lookup aliases only while current.
        alias_rows = self._rows(
            "SELECT contact_id,value,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND kind='push_name' AND strength='weak'"
            " AND contact_id IN (SELECT contact_id FROM contacts"
            " WHERE contact_id IN (SELECT value FROM json_each(?))"
            " OR merged_into IN (SELECT value FROM json_each(?)))",
            (json.dumps(sorted(eligible)), json.dumps(sorted(eligible))),
        )
        for row in alias_rows:
            if _compatible(row['valid_from_ms'], row['valid_until_ms'], at_ms, 'native'):
                alias = ' '.join(str(row['value']).casefold().split())
                terminal = self.terminal(str(row['contact_id']))
                if terminal in eligible:
                    names.setdefault(alias, set()).add(terminal)
        self._person_names_cache[cache_key] = names
        return names

    def resolve_people_in_text(self, content: str, chat_ids: tuple[str, ...], at_ms: int
                               ) -> tuple[tuple[str, str], ...]:
        """Resolve longest name spans through the same resolver; never select an ambiguity."""
        if not isinstance(content, str) or not content.strip():
            return ()
        people = self._eligible_person_names(chat_ids, at_ms)
        width = max((len(_name_tokens(alias)) for alias in people), default=0)
        words = _name_tokens(content)
        found: list[tuple[str, str]] = []
        index = 0
        while index < len(words):
            for size in range(min(width, len(words) - index), 0, -1):
                name = ' '.join(words[index:index + size])
                result = self.resolve_person(name, chat_ids, at_ms)
                if result.status == 'ambiguous':
                    return ()
                if result.person_id is not None:
                    found.append((name, result.person_id))
                    index += size
                    break
            else:
                index += 1
        return tuple(sorted(set(found)))

    def person_names(self, contact_id: str, at_ms: int) -> tuple[str, ...]:
        """Return current exact display/push names for one confirmed person."""
        _time(at_ms)
        terminal = self.terminal(contact_id)
        if terminal is None:
            return ()
        rows = self._rows(
            "SELECT contact_id,kind,status,display_name,merged_into FROM contacts"
            " WHERE contact_id=? OR merged_into=?", (terminal, terminal),
        )
        if not any(row['contact_id'] == terminal and row['kind'] == 'person'
                   and row['status'] == 'confirmed' and row['merged_into'] is None for row in rows):
            return ()
        names = {str(row['display_name']) for row in rows
                 if row['kind'] == 'person' and row['status'] == 'confirmed' and row['display_name']}
        aliases = self._rows(
            "SELECT value,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND kind='push_name' AND strength='weak'"
            " AND contact_id IN (SELECT contact_id FROM contacts WHERE contact_id=? OR merged_into=?)",
            (terminal, terminal),
        )
        names.update(str(row['value']) for row in aliases
                     if _compatible(row['valid_from_ms'], row['valid_until_ms'], at_ms, 'native'))
        return tuple(sorted(names, key=str.casefold))

    def search_person(self, *, chat_ids: tuple[str, ...], contact_id: str,
                      at_ms: int, limit: int, query: str = '') -> list[Row]:
        """Find messages by or explicitly naming one resolved contact in selected chats."""
        _limit(limit)
        _time(at_ms)
        terminal = self.terminal(contact_id)
        names = self.person_names(contact_id, at_ms)
        if not chat_ids or terminal is None:
            return []
        # Apostrophes and possessives remain valid name boundaries (e.g. "Alex' Aktien").
        patterns = [re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.IGNORECASE)
                    for name in names if name.strip()]
        authors = self._rows(
            "WITH RECURSIVE aliases(contact_id) AS (SELECT ? UNION"
            " SELECT c.contact_id FROM contacts c JOIN aliases a ON c.merged_into=a.contact_id)"
            f" SELECT m.* FROM messages_current m WHERE {_VISIBLE}"
            f" AND m.chat_id IN ({','.join('?' for _ in chat_ids)})"
            " AND m.sender_basis IN ('native_identifier','owner_attested')"
            " AND m.sender_contact_id IN (SELECT contact_id FROM aliases)",
            (terminal, *chat_ids),
        )
        name_terms = [re.findall(r"[^\W_]+", name, re.UNICODE) for name in names]
        name_terms = [terms for terms in name_terms if terms]
        mentioned = []
        if name_terms:
            fts_query = ' OR '.join('(' + ' '.join(f'"{term}"*' for term in terms) + ')'
                                    for terms in name_terms)
            mentioned = self._rows(
                "SELECT m.* FROM messages_fts JOIN messages_current m"
                " ON m.message_id=messages_fts.message_id"
                f" WHERE {_VISIBLE} AND m.chat_id IN ({','.join('?' for _ in chat_ids)})"
                " AND messages_fts MATCH ?",
                (*chat_ids, fts_query),
            )
        mention_ids = self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE}"
            f" AND chat_id IN ({','.join('?' for _ in chat_ids)}) AND mentions_json IS NOT NULL",
            chat_ids,
        )
        by_id = {str(row['message_id']): row for row in (*authors, *mentioned, *mention_ids)}
        matches = []
        terms = _name_tokens(query)
        for row in by_id.values():
            author = (self.terminal(str(row['sender_contact_id']))
                      if row['sender_contact_id'] and row['sender_basis'] in ('native_identifier', 'owner_attested')
                      else None)
            mentioned = any(pattern.search(str(row['current_text'] or '')) for pattern in patterns)
            if not mentioned:
                try:
                    mention_values = json.loads(row['mentions_json'] or '[]')
                except (TypeError, ValueError):
                    mention_values = []
                mentioned = any(isinstance(value, str) and self.resolve_identifier(
                    value, at_ms=row['sent_ms'], time_basis=row['time_certainty']) == terminal
                    for value in mention_values) if row['sent_ms'] is not None else False
            if author == terminal or mentioned:
                tokens = _name_tokens(str(row['current_text'] or ''))
                score = sum(any(token.startswith(term) for token in tokens) for term in terms)
                row['query_match_count'] = score
                matches.append((score, row))
        matches.sort(key=lambda item: (-item[0], -(item[1]['sent_ms'] or 0), item[1]['message_id']))
        return [row for _, row in matches[:limit]]

    def media(self, *, chat_id: str, limit: int, native_id: str | None = None) -> list[Row]:
        _limit(limit)
        predicate = " AND native_message_id=?" if native_id is not None else ""
        parameters = (chat_id, native_id, limit) if native_id is not None else (chat_id, limit)
        return self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=? AND media_json IS NOT NULL"
            f"{predicate} ORDER BY {_ORDER} LIMIT ?", parameters,
        )

    def reactions(self, *, message_ids: tuple[str, ...], until_ms: int, limit: int = 500) -> list[Row]:
        """Reaction events (set and removed) on these messages, recorded at or before ``until_ms``.

        Events without a recorded time are left out: an as-of read cannot place them.
        """
        _limit(limit)
        _time(until_ms)
        if not message_ids:
            return []
        if len(message_ids) > 500:
            raise ValueError("at most 500 message ids")
        return self._rows(
            "SELECT event_id, target_message_id, actor_contact_id, actor_identifier, occurred_ms,"
            " json_extract(payload_json, '$.emoji') AS emoji,"
            " COALESCE(json_extract(payload_json, '$.removed'), 0) AS removed"
            " FROM message_events WHERE channel='whatsapp' AND kind='reaction'"
            f" AND target_message_id IN ({','.join('?' for _ in message_ids)})"
            " AND occurred_ms IS NOT NULL AND occurred_ms<=?"
            " ORDER BY occurred_ms, event_id LIMIT ?",
            (*message_ids, until_ms, limit),
        )

    def terminal(self, contact_id: str) -> str | None:
        seen: set[str] = set()
        while True:
            if contact_id in seen:
                raise HistoryPaused("contact_redirect_cycle")
            seen.add(contact_id)
            rows = self._rows("SELECT merged_into FROM contacts WHERE contact_id=?", (contact_id,))
            if not rows:
                return None
            if not rows[0]["merged_into"]:
                return contact_id
            contact_id = rows[0]["merged_into"]

    def contact(self, contact_id: str) -> Row | None:
        terminal = self.terminal(contact_id)
        if terminal is None:
            return None
        return self._rows("SELECT * FROM contacts WHERE contact_id=?", (terminal,))[0]

    def resolve_identifier(self, value: str, *, at_ms: int | None, time_basis: str) -> str | None:
        _time(at_ms)
        ident = classify(value)
        if ident is None or not ident.strong:
            return None
        rows = self._rows(
            "SELECT contact_id,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND kind=? AND value=? AND strength='strong'",
            (ident.kind, ident.value),
        )
        owners = {self.terminal(row["contact_id"]) for row in rows
                  if _compatible(row["valid_from_ms"], row["valid_until_ms"], at_ms, time_basis)}
        return next(iter(owners)) if len(owners) == 1 and None not in owners else None

    def _participant(self, values: Any, at_ms: int) -> str | None:
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            return None
        idents = [classify(v) for v in values]
        if any(ident is None or ident.kind not in ("lid", "pn_jid") for ident in idents):
            return None
        # Platform viewers exist even when person attribution is unresolved.
        phones = {canonical_user_id("whatsapp", metadata={"sender_phone_jid": ident.value})
                  for ident in idents if ident.kind == "pn_jid"}
        if phones:
            return next(iter(phones)) if len(phones) == 1 and "" not in phones else None
        lids = {ident.value for ident in idents}
        return f"whatsapp:{next(iter(lids))}" if len(lids) == 1 else None

    def members(self, *, chat_id: str, at_ms: int | None) -> EvidenceAudience:
        _time(at_ms)
        if at_ms is None:
            return EvidenceAudience.unknown()
        if self._rows(
            "SELECT 1 FROM message_events WHERE channel='whatsapp' AND chat_id=?"
            " AND kind IN ('member_snapshot','member_add','member_remove')"
            " AND (occurred_ms IS NULL OR time_certainty='unknown') LIMIT 1", (chat_id,),
        ):
            return EvidenceAudience.unknown()
        snapshots = self._rows(
            "SELECT occurred_ms FROM message_events WHERE channel='whatsapp' AND chat_id=?"
            " AND kind='member_snapshot' AND occurred_ms<=?"
            " ORDER BY occurred_ms DESC,event_id DESC LIMIT 1", (chat_id, at_ms),
        )
        if not snapshots:
            return EvidenceAudience.unknown()
        latest = snapshots[0]["occurred_ms"]
        # Future approximate removals can narrow this interval before their capture.
        events = self._rows(
            "SELECT * FROM message_events WHERE channel='whatsapp' AND chat_id=?"
            " AND kind IN ('member_snapshot','member_add','member_remove')"
            " AND occurred_ms>=? ORDER BY occurred_ms,event_id", (chat_id, latest),
        )
        rosters: dict[int, list[Row]] = {}
        for event in events:
            event["payload"] = json.loads(event["payload_json"])
            if event["kind"] == "member_snapshot":
                rosters.setdefault(event["occurred_ms"], []).append(event)

        parsed_values: dict[str, tuple[str, str, str] | None] = {}

        def participant(values: Any) -> tuple[str, frozenset[str]] | None:
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                return None
            items = []
            for value in values:
                if value not in parsed_values:
                    ident = classify(value)
                    parsed_values[value] = None
                    if ident is not None and ident.kind in ("lid", "pn_jid"):
                        principal = (canonical_user_id("whatsapp", metadata={"sender_phone_jid": ident.value})
                                     if ident.kind == "pn_jid" else f"whatsapp:{ident.value}")
                        if principal:
                            parsed_values[value] = ident.kind, ident.value, principal
                item = parsed_values[value]
                if item is None:
                    return None
                items.append(item)
            phones = {principal for kind, _, principal in items if kind == "pn_jid"}
            identifiers = frozenset(value for _, value, _ in items)
            if len(phones) > 1 or (not phones and len(identifiers) != 1):
                return None
            return next(iter(phones)) if phones else items[0][2], identifiers

        def entries(event: Row) -> list[tuple[str, frozenset[str]]] | None:
            if "entries" in event:
                return event["entries"]
            event["entries"] = None
            payload = event["payload"]
            if (event["time_certainty"] not in ("native", "provider_timestamp", "capture_time_approx")
                    or event["provenance"] != "native" or not isinstance(payload, dict)
                    or not isinstance(payload.get("participants"), list)
                    or (event["kind"] == "member_snapshot" and payload.get("complete") is not True)):
                return None
            result = []
            for values in payload["participants"]:
                entry = participant(values)
                if entry is None:
                    return None
                result.append(entry)
            event["entries"] = result
            return result

        def roster_proven(rows: list[Row]) -> bool:
            proofs = []
            for event in rows:
                values = entries(event)
                if values is None:
                    return False
                proofs.append({principal for principal, _ in values})
            return all(proof == proofs[0] for proof in proofs)

        base = rosters[latest]
        if not roster_proven(base):
            return EvidenceAudience.unknown()
        boundary = next((moment for moment, rows in rosters.items()
                         if moment > at_ms and roster_proven(rows)), None)
        relevant = [e for e in events if e["occurred_ms"] <= at_ms or
                    (e["kind"] == "member_remove" and e["time_certainty"] == "capture_time_approx"
                     and (boundary is None or e["occurred_ms"] < boundary))]
        for event in relevant:
            if entries(event) is None:
                return EvidenceAudience.unknown()
            if event["kind"] != "member_snapshot" and event["occurred_ms"] == latest:
                return EvidenceAudience.unknown()

        values = {value for e in relevant for _, identifiers in entries(e) for value in identifiers}
        # json_each keeps both reads bounded independently of roster size and redirect depth.
        ownership = self._rows(
            "SELECT value,contact_id,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND kind IN ('lid','pn_jid') AND strength='strong'"
            " AND value IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(values)),),
        )
        contacts = self._rows(
            "WITH RECURSIVE reachable(contact_id,merged_into) AS ("
            " SELECT contact_id,merged_into FROM contacts"
            " WHERE contact_id IN (SELECT value FROM json_each(?)) UNION"
            " SELECT c.contact_id,c.merged_into FROM contacts c"
            " JOIN reachable r ON c.contact_id=r.merged_into)"
            " SELECT contact_id,merged_into FROM reachable",
            (json.dumps(sorted({row["contact_id"] for row in ownership})),),
        )
        redirects = {row["contact_id"]: row["merged_into"] for row in contacts}
        by_value: dict[str, list[Row]] = {}
        for row in ownership:
            by_value.setdefault(row["value"], []).append(row)

        def owners(identifiers: frozenset[str], event: Row) -> set[str]:
            result: set[str] = set()
            for value in identifiers:
                for row in by_value.get(value, ()):
                    if not _compatible(row["valid_from_ms"], row["valid_until_ms"],
                                       event["occurred_ms"], event["time_certainty"]):
                        continue
                    contact_id = row["contact_id"]
                    seen: set[str] = set()
                    while contact_id in redirects and redirects[contact_id] is not None:
                        if contact_id in seen:
                            raise HistoryPaused("contact_redirect_cycle")
                        seen.add(contact_id)
                        contact_id = redirects[contact_id]
                    if contact_id in redirects:
                        result.add(contact_id)
            return result

        members: dict[str, tuple[set[str], str | None]] = {}

        def add(event: Row) -> None:
            for principal, values in entries(event):
                identifiers = set(values)
                contacts = owners(values, event)
                owner = next(iter(contacts)) if len(contacts) == 1 else None
                if principal in members:
                    previous, previous_owner = members[principal]
                    identifiers |= previous
                    if previous_owner != owner:
                        owner = None
                members[principal] = identifiers, owner

        def remove(event: Row) -> None:
            removed_values: set[str] = set()
            removed_owners: set[str] = set()
            removed_principals: set[str] = set()
            for principal, identifiers in entries(event):
                removed_values.update(identifiers)
                removed_owners.update(owners(identifiers, event))
                removed_principals.add(principal)
            for principal, (identifiers, owner) in tuple(members.items()):
                if (principal in removed_principals or identifiers & removed_values
                        or (owner is not None and owner in removed_owners)):
                    del members[principal]

        for event in base:
            add(event)
        for event in relevant:
            if event["kind"] == "member_snapshot" or event["occurred_ms"] > at_ms:
                continue
            if event["kind"] == "member_add":
                add(event)
            else:
                remove(event)
        for event in relevant:
            if event["kind"] == "member_remove" and event["occurred_ms"] > at_ms:
                remove(event)
        return EvidenceAudience.known(set(members), snapshot_id=base[0]["event_id"])

    def audience(self, message_id: str) -> EvidenceAudience:
        self.snapshot.assert_current(self.snapshot.generation)
        state = (self.snapshot.generation, self.snapshot.connection.total_changes)
        # Production snapshots are immutable; direct-SQL fixtures can change between reads.
        if state != self._audience_state:
            self._audiences.clear()
            self._audience_state = state
        if message_id not in self._audiences:
            self._audiences[message_id] = self._audience(message_id)
        return self._audiences[message_id]

    def _audience(self, message_id: str) -> EvidenceAudience:
        row = self.message(message_id)
        if (row is None or row["sent_ms"] is None
                or row["time_certainty"] not in ("native", "provider_timestamp")):
            return EvidenceAudience.unknown()
        chat = classify(row["chat_id"])
        if chat is not None and chat.kind == "group":
            return self.members(chat_id=row["chat_id"], at_ms=row["sent_ms"])
        if chat is None or not chat.strong or row["sender_basis"] not in ("native_identifier", "owner_attested"):
            return EvidenceAudience.unknown()
        values = (row["chat_id"], row["sender_identifier"])
        if any(self.resolve_identifier(value, at_ms=row["sent_ms"], time_basis=row["time_certainty"]) is None
               for value in values):
            return EvidenceAudience.unknown()
        endpoints = [self._participant([value], row["sent_ms"]) for value in values]
        if None in endpoints:
            return EvidenceAudience.unknown()
        if len(set(endpoints)) == 1:
            return EvidenceAudience.author_only()
        return EvidenceAudience.known(set(endpoints))

    def mention(self, value: str, *, chat_id: str, at_ms: int) -> str | None:
        _time(at_ms)
        owner = self.resolve_identifier(value, at_ms=at_ms, time_basis="native")
        if owner is None:
            return None
        proof = self.members(chat_id=chat_id, at_ms=at_ms)
        if proof.status != "known":
            return None
        rows = self._rows(
            "WITH RECURSIVE aliases(contact_id) AS (SELECT ? UNION"
            " SELECT c.contact_id FROM contacts c JOIN aliases a ON c.merged_into=a.contact_id)"
            " SELECT value,contact_id,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND kind='pn_jid' AND strength='strong'"
            " AND contact_id IN (SELECT contact_id FROM aliases)", (owner,),
        )
        phones = {r["value"] for r in rows if self.terminal(r["contact_id"]) == owner
                  and _compatible(r["valid_from_ms"], r["valid_until_ms"], at_ms, "native")
                  and self.resolve_identifier(r["value"], at_ms=at_ms, time_basis="native") == owner}
        if len(phones) != 1:
            return None
        phone = next(iter(phones))
        principal = canonical_user_id("whatsapp", metadata={"sender_phone_jid": phone})
        ident = classify(value)
        if not principal or (principal not in proof.members and f"whatsapp:{ident.value}" not in proof.members):
            return None
        return phone
