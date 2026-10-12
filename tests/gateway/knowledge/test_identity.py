"""P1.2: verified identifiers, name priority and endpoint selection.

All values are synthetic.  The point of these tests is that identity comes from proven
platform evidence, never from a name, and that typos in a namespace never merge people.
"""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    Identifier,
)


def test_identifier_namespace_must_not_be_empty():
    from yeoman_gateway.knowledge.models import ValidationError

    with pytest.raises(ValidationError):
        from yeoman_gateway.knowledge.models import Identifier

        Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net", "")


def test_identifier_derives_the_documented_default_namespace():
    """Compatibility callers may omit it; the value is explicit and stable, not guessed."""
    from yeoman_gateway.knowledge.models import DEFAULT_NAMESPACE

    assert Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net").namespace == (
        DEFAULT_NAMESPACE
    )
    assert Identifier(
        "whatsapp", "phone_jid", "49111111111@s.whatsapp.net", "Account-A"
    ).namespace == "account-a"


def test_identifier_namespace_rejects_control_characters():
    from yeoman_gateway.knowledge.models import ValidationError

    with pytest.raises(ValidationError):
        Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net", "acc\x00unt")


def test_identifier_values_are_never_silently_truncated():
    from yeoman_gateway.knowledge.models import ValidationError

    with pytest.raises(ValidationError):
        Identifier("telegram", "telegram_username", "u" * 600)
    with pytest.raises(ValidationError):
        Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net extra")


def test_names_reject_control_characters_and_overlong_values():
    from yeoman_gateway.knowledge.models import MAX_NAME_LENGTH, ValidationError, validate_name

    with pytest.raises(ValidationError):
        validate_name("ok\x07bad")
    with pytest.raises(ValidationError):
        validate_name("n" * (MAX_NAME_LENGTH + 1))
    # Exactly at the bound is still accepted, and unchanged.
    at_bound = "n" * MAX_NAME_LENGTH
    assert validate_name(at_bound) == at_bound


def test_alias_normalization_is_search_only_and_bounded():
    from yeoman_gateway.knowledge.models import (
        MAX_NAME_LENGTH,
        ValidationError,
        normalize_alias_value,
    )

    assert normalize_alias_value("  Vinzent   K. ") == "vinzent k."
    with pytest.raises(ValidationError):
        normalize_alias_value("n" * (MAX_NAME_LENGTH + 1))
    with pytest.raises(ValidationError):
        normalize_alias_value("bad\x00name")
    with pytest.raises(ValidationError):
        normalize_alias_value("   ")


def test_attribute_values_reject_overlong_and_control_input():
    from yeoman_gateway.knowledge.models import (
        MAX_ATTRIBUTE_VALUE_LENGTH,
        AttributeValue,
        ValidationError,
        validate_attribute_key,
    )

    assert AttributeValue("Köln", precision="exact").value_key == "köln"
    with pytest.raises(ValidationError):
        AttributeValue("v" * (MAX_ATTRIBUTE_VALUE_LENGTH + 1))
    with pytest.raises(ValidationError):
        AttributeValue("bad\x1fvalue")
    with pytest.raises(ValidationError):
        validate_attribute_key("profession")


def test_statement_candidate_rejects_unknown_time_basis_and_precision():
    from yeoman_gateway.knowledge.models import (
        SourceRef,
        StatementCandidate,
        ValidationError,
    )

    source = SourceRef(
        event_id="event-t",
        revision=1,
        channel="whatsapp",
        chat_id="group-a",
        author_principal="whatsapp:4910000000002",
        occurred_at_ms=1,
    )
    with pytest.raises(ValidationError):
        StatementCandidate(content="x", sources=(source,), time_basis="whenever")
    with pytest.raises(ValidationError):
        StatementCandidate(content="x", sources=(source,), time_precision="probably")


def test_identifier_binding_covers_only_a_proven_period():
    from yeoman_gateway.knowledge.models import IdentifierBinding

    binding = IdentifierBinding(
        person_id="p",
        identifier=Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net"),
        evidence_ref="ref",
        valid_from_ms=100,
        valid_until_ms=200,
    )
    assert binding.covers(150) is True
    assert binding.covers(99) is False
    assert binding.covers(200) is False  # the end is exclusive

    unknown_start = IdentifierBinding(
        person_id="p",
        identifier=Identifier("whatsapp", "phone_jid", "49111111111@s.whatsapp.net"),
        evidence_ref="ref",
        valid_from_ms=0,
    )
    # An unknown start authorizes nothing retroactively.
    assert unknown_start.covers(150) is False
