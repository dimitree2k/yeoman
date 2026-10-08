"""Bounded, snapshot-local WhatsApp reads; identity never grants disclosure aliases."""

from __future__ import annotations

import json
import re
from typing import Any

from yeoman_gateway.knowledge import EvidenceAudience
from yeoman_gateway.policy.identity import canonical_user_id

from .ids import classify
from .live import HistoryPaused
from .reader import HistorySnapshot
from .resolve import _compatible

Row = dict[str, Any]
_ORDER = "sent_ms, message_id"
_VISIBLE = "channel='whatsapp' AND deleted=0"


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 500:
        raise ValueError("limit must be an integer from 1 to 500")


def _time(value: int | None) -> None:
    if value is not None and type(value) is not int:
        raise ValueError("time must be integer milliseconds or None")


class HistoryQueries:
    def __init__(self, snapshot: HistorySnapshot):
        self.snapshot = snapshot

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[Row]:
        self.snapshot.assert_current(self.snapshot.generation)
        cursor = self.snapshot.connection.execute(sql, parameters)
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor]

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
               after_ms: int | None = None) -> list[Row]:
        _limit(limit)
        _time(after_ms)
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
              (SELECT json_extract(payload_json,'$.subject') FROM message_events e
               WHERE e.channel='whatsapp' AND e.chat_id=c.chat_id AND kind='group_subject'
               ORDER BY occurred_ms DESC,event_id DESC LIMIT 1) AS subject,
              (SELECT json_extract(payload_json,'$.description') FROM message_events e
               WHERE e.channel='whatsapp' AND e.chat_id=c.chat_id AND kind='group_description'
               ORDER BY occurred_ms DESC,event_id DESC LIMIT 1) AS description
            FROM (SELECT chat_id FROM messages WHERE channel='whatsapp'
                  UNION SELECT chat_id FROM message_events WHERE channel='whatsapp') c ORDER BY chat_id
        """)

    def media(self, *, chat_id: str, limit: int, native_id: str | None = None) -> list[Row]:
        _limit(limit)
        predicate = " AND native_message_id=?" if native_id is not None else ""
        parameters = (chat_id, native_id, limit) if native_id is not None else (chat_id, limit)
        return self._rows(
            f"SELECT * FROM messages_current WHERE {_VISIBLE} AND chat_id=? AND media_json IS NOT NULL"
            f"{predicate} ORDER BY {_ORDER} LIMIT ?", parameters,
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
