"""Identity tables of knowledge.db and contacts.db, and the chat registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..layer1 import Origin, backfill_line
from .common import compact, epoch_or_iso_to_ms, open_ro, row_dict, table_exists

Mapper = Callable[[dict[str, Any]], tuple[str, str, dict[str, Any]]]


def _contact(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    created_ms, _ = epoch_or_iso_to_ms(row.get("created_at"))
    return "contact_record", "any", {
        **compact({"contactRef": row.get("id"), "displayName": row.get("display_name"),
                   "preferredName": row.get("preferred_name"), "phoneNumber": row.get("phone_number"),
                   "createdMs": created_ms, "status": row.get("status")}),
        "isOwner": bool(row.get("is_owner")),
    }


def _identifier(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "identifier_record", row.get("channel") or "whatsapp", compact({
        "contactRef": row.get("contact_id"), "identifier": row.get("identifier"),
        "identifierKind": row.get("kind"), "source": "contact_identifiers"})


def _binding(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "identifier_record", row.get("channel") or "whatsapp", compact({
        "contactRef": row.get("person_id"), "identifier": row.get("value"),
        "identifierKind": row.get("kind"), "status": row.get("status"),
        "mappingVerified": row.get("mapping_verified"),
        "validFromMs": row.get("valid_from_ms") or None, "validUntilMs": row.get("valid_until_ms") or None,
        "source": "binding"})


def _alias(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "name_record", "any", compact({
        "contactRef": row.get("contact_id"), "name": row.get("alias"), "status": row.get("status"),
        "source": row.get("source"), "mappingRetracted": row.get("mapping_retracted") or None})


def _pair(row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    return "pair_record", row.get("channel") or "whatsapp", compact({
        "lid": row.get("lid_value"), "pnJid": row.get("phone_value"),
        "firstMs": row.get("first_observed_at_ms"), "lastMs": row.get("last_observed_at_ms")})


_TABLES: tuple[tuple[str, Mapper | None], ...] = (
    ("contacts", _contact),
    ("contact_identifiers", _identifier),
    ("knowledge_identifier_bindings", _binding),
    ("contact_aliases", _alias),
    ("contact_fields", None),
    ("knowledge_provider_pair_evidence", _pair),
    ("knowledge_provider_pair_sources", None),
)


def _identity_db(source_home: Path, db_rel: str, store: str) -> Iterator[dict[str, Any]]:
    path = source_home / db_rel
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        for table, mapper in _TABLES:
            if not table_exists(conn, table):
                continue
            for row in conn.execute(f"SELECT rowid AS _rowid, * FROM {table} ORDER BY rowid"):
                original = row_dict(row)
                origin = Origin(store, db_rel, table, str(original.pop("_rowid")))
                if mapper is None:
                    kind, channel, payload, reason = "identity_detail", "any", {}, f"not_projected:{table}"
                else:
                    (kind, channel, payload), reason = mapper(original), None
                yield backfill_line(channel=channel, kind=kind, provenance="derived_only",
                                    time_certainty="unknown", occurred_ms=None, direction=None,
                                    chat_id=None, payload=payload, origin=origin, original=original,
                                    skip_reason=reason)


def convert_knowledge(source_home: Path) -> Iterator[dict[str, Any]]:
    yield from _identity_db(source_home, "data/knowledge/knowledge.db", "knowledge")


def convert_contacts_db(source_home: Path) -> Iterator[dict[str, Any]]:
    yield from _identity_db(source_home, "data/contacts/contacts.db", "contacts_db")


def convert_chat_registry(source_home: Path) -> Iterator[dict[str, Any]]:
    db_rel = "data/inbound/chat_registry.db"
    path = source_home / db_rel
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if not table_exists(conn, "chats"):
            return
        for row in conn.execute("SELECT rowid AS _rowid, * FROM chats ORDER BY rowid"):
            original = row_dict(row)
            origin = Origin("chat_registry", db_rel, "chats", str(original.pop("_rowid")))
            yield backfill_line(channel=original.get("channel") or "whatsapp", kind="chat_record",
                                provenance="derived_only", time_certainty="unknown", occurred_ms=None,
                                direction=None, chat_id=original.get("chat_id"), payload={},
                                origin=origin, original=original, skip_reason="chat_metadata")
