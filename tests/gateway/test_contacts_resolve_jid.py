from __future__ import annotations

from yeoman_gateway.contacts.service import ContactsService


def test_resolve_jid_to_name_returns_display_name(tmp_path) -> None:
    svc = ContactsService(db_path=tmp_path / "contacts.db")
    contact_id = svc.ensure_contact(
        channel="whatsapp",
        identifier="491234567890@s.whatsapp.net",
        kind="phone_jid",
        push_name="Alice",
    )
    svc.update_display_name(contact_id, "Alice Wonder")

    result = svc.resolve_jid_to_name("491234567890@s.whatsapp.net")
    assert result == "Alice Wonder"


def test_resolve_jid_to_name_returns_none_for_unknown(tmp_path) -> None:
    svc = ContactsService(db_path=tmp_path / "contacts.db")

    result = svc.resolve_jid_to_name("unknown@s.whatsapp.net")
    assert result is None
