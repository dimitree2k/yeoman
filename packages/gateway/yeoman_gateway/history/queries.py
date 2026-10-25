"""Bounded, snapshot-local WhatsApp reads; identity never grants disclosure aliases."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from yeoman_gateway.knowledge import EvidenceAudience
from yeoman_gateway.policy.identity import canonical_user_id

from .ids import classify
from .live import HistoryPaused
from .reader import HistorySnapshot
from .resolve import _compatible

if TYPE_CHECKING:
    from yeoman_gateway.people.bridges import HistoryPerson

Row = dict[str, Any]
_ORDER = "sent_ms, message_id"
_VISIBLE = "channel='whatsapp' AND deleted=0"






def _name_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return tuple(re.findall(r"[^\W\d_]+|\d+", normalized, flags=re.UNICODE))




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
        self._membership_identifiers: dict[str, tuple[str, str, str] | None] = {}
        self._membership_participants: dict[tuple[str, ...], tuple[str, frozenset[str]]] = {}
        self._audience_state = (snapshot.generation, snapshot.connection.total_changes)

    def _rows(self, sql: str, parameters: tuple[Any, ...] | dict[str, Any] = ()) -> list[Row]:
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
        identifiers = self._rows(
            "WITH RECURSIVE aliases(contact_id) AS (SELECT ? UNION"
            " SELECT c.contact_id FROM contacts c JOIN aliases a ON c.merged_into=a.contact_id)"
            " SELECT kind,value,valid_from_ms,valid_until_ms FROM identifier_history"
            " WHERE channel='whatsapp' AND strength='strong' AND kind IN ('pn_jid','lid')"
            " AND contact_id IN (SELECT contact_id FROM aliases)", (owner,))
        candidates = set()
        for row in identifiers:
            if _compatible(row['valid_from_ms'],row['valid_until_ms'],now_ms,'native'):
                value = str(row['value'])
                candidates.add('whatsapp:' + value)
                if row['kind'] == 'pn_jid':
                    candidates.add('whatsapp:' + value.split('@')[0])
        owned = {candidate for candidate in candidates if self.principal_contact(candidate,now_ms) == owner}
        result = []
        for row in self.chats():
            chat_id = str(row['chat_id'])
            ident = classify(chat_id)
            if ident is None or ident.kind != 'group':
                continue
            audience = self.members(chat_id=chat_id, at_ms=now_ms)
            if audience.status == 'known' and owned.intersection(audience.members):
                result.append(chat_id)
        return tuple(result)





    def search_person(self, *, chat_ids: tuple[str, ...], person: HistoryPerson,
                      at_ms: int, limit: int, query: str = '',
                      after_ms: int | None = None) -> list[Row]:
        """Select temporal authors/mentions in SQL; materialize only the ranked result limit."""
        _limit(limit)
        _time(at_ms)
        _time(after_ms)
        self.snapshot.assert_current(self.snapshot.generation)
        if not chat_ids:
            return []
        identifiers = json.dumps([(r.value, r.valid_from_ms, r.valid_until_ms) for r in person.identifiers])
        contacts = json.dumps([(r.value, r.valid_from_ms, r.valid_until_ms) for r in person.contact_intervals])
        patterns = [re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.IGNORECASE)
                    for name in person.names if name.strip()]
        terms = _name_tokens(query)
        # These functions do text matching/ranking only. Identity and temporal proof stay in SQL.
        def named(text: str | None) -> bool:
            return any(pattern.search(text or '') for pattern in patterns)

        def score(text: str | None) -> int:
            tokens = _name_tokens(text or '')
            return sum(any(token.startswith(term) for token in tokens) for term in terms)

        connection = self.snapshot.connection
        connection.create_function('_person_named', 1, named, deterministic=True)
        connection.create_function('_person_score', 1, score, deterministic=True)
        name_terms = [re.findall(r'[^\W_]+', name, re.UNICODE) for name in person.names]
        fts = ' OR '.join('(' + ' '.join(f'"{word}"*' for word in words) + ')'
                          for words in name_terms if words)
        source = "m.channel='whatsapp' AND m.deleted=0 AND m.chat_id IN (SELECT value FROM json_each(:chats)) AND (m.sent_ms IS NULL OR m.sent_ms<=:at_ms)"
        named_sql = ("SELECT m.message_id FROM messages_fts JOIN messages_current m"
                     " ON m.message_id=messages_fts.message_id WHERE messages_fts MATCH :fts"
                     " AND _person_named(m.current_text) AND " + source) if fts else "SELECT NULL AS message_id WHERE 0"
        # Unknown/derived times cannot place a bounded identifier; unbounded ownership is safe.
        valid_author = """((i.start IS NULL AND i.end IS NULL) OR
             (m.sent_ms IS NOT NULL AND m.time_certainty IN ('native','provider_timestamp')
              AND (i.start IS NULL OR i.start<=m.sent_ms) AND (i.end IS NULL OR m.sent_ms<i.end)))"""
        sql = f"""
          WITH ids(value,start,end) AS (
            SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(:identifiers)),
          contacts(value,start,end) AS (
            SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(:contacts)),
          authored(message_id) AS (
            SELECT m.message_id FROM ids i JOIN messages_current m ON m.sender_identifier=i.value
            WHERE {source} AND m.sender_basis IN ('native_identifier','owner_attested') AND {valid_author}
            UNION
            SELECT m.message_id FROM contacts i JOIN messages_current m ON m.sender_contact_id=i.value
            WHERE {source} AND m.sender_basis='owner_attested' AND coalesce(m.sender_identifier,'')='' AND {valid_author}),
          named(message_id) AS ({named_sql}),
          mention_values(message_id,value,sent_ms,time_certainty) AS (
            SELECT m.message_id,ltrim(mention.value,'@'),m.sent_ms,m.time_certainty
            FROM messages_current m JOIN json_each(m.mentions_json) mention
            WHERE {source} AND json_type(m.mentions_json)='array' AND typeof(mention.value)='text'
              AND m.sent_ms IS NOT NULL AND m.time_certainty IN ('native','provider_timestamp')),
          explicit(message_id) AS (
            SELECT m.message_id FROM mention_values m JOIN ids i ON
              (m.value=i.value OR
               (instr(m.value,':')>0 AND instr(m.value,'@')>instr(m.value,':')+1
                AND substr(m.value,1,instr(m.value,':')-1)=substr(i.value,1,instr(i.value,'@')-1)
                AND substr(m.value,instr(m.value,'@'))=substr(i.value,instr(i.value,'@'))
                AND substr(m.value,instr(m.value,':')+1,instr(m.value,'@')-instr(m.value,':')-1) NOT GLOB '*[^0-9]*'))
            WHERE (i.start IS NULL OR i.start<=m.sent_ms) AND (i.end IS NULL OR m.sent_ms<i.end)),
          candidates(message_id) AS (SELECT message_id FROM authored UNION SELECT message_id FROM named UNION SELECT message_id FROM explicit)
          SELECT m.*, (m.message_id IN (SELECT message_id FROM authored)) AS person_authored,
                 _person_score(m.current_text) AS query_match_count
          FROM candidates c JOIN messages_current m ON m.message_id=c.message_id
          WHERE m.channel='whatsapp' AND m.deleted=0
            AND m.chat_id IN (SELECT value FROM json_each(:chats))
            AND (m.sent_ms IS NULL OR m.sent_ms<=:at_ms)
            AND (:after_ms IS NULL OR m.sent_ms>:after_ms)
          ORDER BY query_match_count DESC, m.sent_ms DESC, m.message_id LIMIT :limit
        """
        result = self._rows(sql, {'identifiers': identifiers, 'contacts': contacts, 'fts': fts,
                                  'chats': json.dumps(chat_ids), 'at_ms': at_ms, 'after_ms': after_ms,
                                  'limit': limit})
        self.snapshot.assert_current(self.snapshot.generation)
        return result

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

    def assistant_contact_ids(self) -> frozenset[str]:
        """Arvid's own contact ids: the assistant role contact and contacts merged into it."""
        rows = self._rows(
            "SELECT contact_id FROM contacts WHERE role='assistant'"
            " OR merged_into IN (SELECT contact_id FROM contacts WHERE role='assistant')",
            (),
        )
        return frozenset(str(row["contact_id"]) for row in rows if row["contact_id"])

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

    def member_principal(
        self, identifier: str, members: frozenset[str], *, at_ms: int
    ) -> str | None:
        """The one roster principal a sender identifier belongs to, or ``None``.

        A roster names a member by its phone form when it knows one, while a message can
        arrive from the same member's LID. The sender's own form is tried first; otherwise
        the strong history contact behind it must name exactly one member through its
        other identifiers valid at ``at_ms``.
        """
        direct = self._participant([identifier], at_ms)
        if direct is not None and direct in members:
            return direct
        contact = self.resolve_identifier(identifier, at_ms=at_ms, time_basis="native")
        if contact is None:
            return None
        rows = self._rows(
            "WITH RECURSIVE aliases(contact_id) AS (SELECT ? UNION"
            " SELECT c.contact_id FROM contacts c JOIN aliases a ON c.merged_into=a.contact_id)"
            " SELECT value FROM identifier_history WHERE channel='whatsapp'"
            " AND kind IN ('lid','pn_jid') AND strength='strong'"
            " AND contact_id IN (SELECT contact_id FROM aliases)"
            " AND (valid_from_ms IS NULL OR valid_from_ms<=?)"
            " AND (valid_until_ms IS NULL OR ?<valid_until_ms)",
            (contact, at_ms, at_ms),
        )
        found = {
            principal
            for row in rows
            if (principal := self._participant([str(row["value"])], at_ms)) in members
        }
        return next(iter(found)) if len(found) == 1 else None

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

        parsed_values = self._membership_identifiers

        def participant(values: Any) -> tuple[str, frozenset[str]] | None:
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                return None
            key = tuple(values)
            if key in self._membership_participants:
                return self._membership_participants[key]
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
            result = next(iter(phones)) if phones else items[0][2], identifiers
            self._membership_participants[key] = result
            return result

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

    def participant_terms(self, *, chat_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Names and phone numbers of everyone history saw in one chat, Arvid excluded.

        Senders, reaction actors and roster entries, resolved through every identifier
        history ever recorded for them. A deny list for generated text, never a roster.
        """
        names, phones, _ = self.participant_deny_terms(chat_id=chat_id)
        return names, phones

    def participant_deny_terms(
        self, *, chat_id: str
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """:meth:`participant_terms` plus the participants' history contact ids.

        The contact ids let a caller add what the person directory knows about the same
        people (owner nicknames, preferred names) to the deny list.
        """
        rows = self._rows(
            "SELECT sender_contact_id AS contact_id, sender_identifier AS identifier"
            " FROM messages WHERE channel='whatsapp' AND chat_id=? AND direction='in'"
            " UNION SELECT actor_contact_id, actor_identifier FROM message_events"
            " WHERE channel='whatsapp' AND chat_id=?", (chat_id, chat_id),
        )
        identifiers = {str(row["identifier"]) for row in rows if row["identifier"]}
        for event in self._rows(
            "SELECT payload_json FROM message_events WHERE channel='whatsapp' AND chat_id=?"
            " AND kind IN ('member_snapshot','member_add','member_remove')", (chat_id,),
        ):
            try:
                participants = json.loads(event["payload_json"]).get("participants")
            except (TypeError, ValueError, AttributeError):
                continue
            for entry in participants if isinstance(participants, list) else ():
                values = entry if isinstance(entry, list) else [entry]
                identifiers.update(str(value) for value in values if isinstance(value, str))
        contacts = {str(row["contact_id"]) for row in rows if row["contact_id"]}
        if identifiers:
            contacts.update(str(row["contact_id"]) for row in self._rows(
                "SELECT DISTINCT contact_id FROM identifier_history WHERE channel='whatsapp'"
                " AND value IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(identifiers)),),
            ))
        terminals = {terminal for item in contacts if (terminal := self.terminal(item))}
        people = self._rows(
            "WITH RECURSIVE aliases(contact_id) AS (SELECT value FROM json_each(?) UNION"
            " SELECT c.contact_id FROM contacts c JOIN aliases a ON c.merged_into=a.contact_id)"
            " SELECT c.contact_id, c.role, c.display_name FROM contacts c"
            " JOIN aliases a ON a.contact_id=c.contact_id",
            (json.dumps(sorted(terminals)),),
        )
        assistants = {str(row["contact_id"]) for row in people if row["role"] == "assistant"}
        kept = [row for row in people if str(row["contact_id"]) not in assistants]
        names = {str(row["display_name"]).strip() for row in kept if row["display_name"]}
        known = self._rows(
            "SELECT kind, value FROM identifier_history WHERE channel='whatsapp'"
            " AND contact_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(str(row["contact_id"]) for row in kept)),),
        )
        names.update(str(row["value"]).strip() for row in known if row["kind"] == "push_name")
        phones = {
            str(row["value"]).split("@", 1)[0] for row in known if row["kind"] in ("pn_jid", "numeric")
        }
        phones.update(
            value.split("@", 1)[0] for value in identifiers if value.endswith("@s.whatsapp.net")
        )
        return (
            tuple(sorted((name for name in names if name), key=str.casefold)),
            tuple(sorted(phone for phone in phones if phone.isdigit())),
            tuple(sorted(str(row["contact_id"]) for row in kept)),
        )

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
