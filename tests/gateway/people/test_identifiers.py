from __future__ import annotations

import pytest
from yeoman_gateway.people.identifiers import (
    identifier_from_history,
    is_valid_value,
    normalize_phone,
)


@pytest.mark.parametrize("raw, expected", [
    ("+49 152 2336 9086", "4915223369086"),
    ("0049-152-23369086", "4915223369086"),
    ("4915223369086", "4915223369086"),
    ("(+49) 152/2336.9086", "4915223369086"),
    ("0152 23369086", None),            # national form: country unknown
    ("12", None),
    ("not a number", None),
    ("", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_history_identifiers():
    assert identifier_from_history("pn_jid", "4915223369086:12@s.whatsapp.net").value == "4915223369086"
    assert identifier_from_history("pn_jid", "4915223369086@s.whatsapp.net").kind == "phone"
    assert identifier_from_history("lid", "203075365150770@lid").kind == "wa_lid"
    assert identifier_from_history("lid", "203075365150770:3@lid").value == "203075365150770"
    assert identifier_from_history("newsletter", "1203@newsletter") is None
    assert identifier_from_history("push_name", "Vinz") is None
    assert identifier_from_history("numeric", "4915223369086").kind == "phone"
    assert identifier_from_history("numeric", "123") is None
    assert identifier_from_history("lid", "garbage") is None
    assert identifier_from_history("other", "x") is None


def test_history_identifier_keeps_times():
    ident = identifier_from_history("lid", "111@lid", valid_from_ms=1, valid_until_ms=2,
                                    first_seen_ms=3, last_seen_ms=4)
    assert (ident.valid_from_ms, ident.valid_until_ms, ident.first_seen_ms, ident.last_seen_ms) == (1, 2, 3, 4)


def test_value_checks():
    assert is_valid_value("phone", "4915223369086")
    assert not is_valid_value("phone", "+49 152")
    assert is_valid_value("wa_lid", "203075365150770")
    assert not is_valid_value("telegram_id", "abc")
    assert is_valid_value("telegram_username", "vincent_v")
    assert not is_valid_value("telegram_username", "v")
    assert is_valid_value("email", "a@b.de")
    assert not is_valid_value("email", "a@b")
    assert is_valid_value("wechat", "x")
    assert not is_valid_value("wechat", " ")
