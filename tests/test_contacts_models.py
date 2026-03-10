"""Tests for contacts domain models."""

from yeoman.contacts.models import Contact, ContactIdentifier, ContactAlias, ContactField


class TestContactModels:
    def test_contact_creation(self) -> None:
        c = Contact(id="uuid-1", display_name="Alex", phone_number="+491521234567")
        assert c.display_name == "Alex"
        assert c.is_owner is False

    def test_contact_identifier(self) -> None:
        ci = ContactIdentifier(
            contact_id="uuid-1", channel="whatsapp",
            identifier="491521234567@s.whatsapp.net", kind="phone_jid",
        )
        assert ci.channel == "whatsapp"

    def test_contact_alias(self) -> None:
        ca = ContactAlias(
            contact_id="uuid-1", alias="AlexGrrrrr", source="pushname",
        )
        assert ca.source == "pushname"

    def test_contact_field(self) -> None:
        cf = ContactField(
            contact_id="uuid-1", kind="email",
            value="alex@bmw.de", label="work",
        )
        assert cf.kind == "email"
        assert cf.label == "work"
