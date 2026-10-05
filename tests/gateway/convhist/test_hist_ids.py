import pytest
from yeoman_gateway.history.ids import Ident, classify, numeric_part


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("46918273106072@lid", Ident("lid", "46918273106072@lid")),
        ("4917632625469@s.whatsapp.net", Ident("pn_jid", "4917632625469@s.whatsapp.net")),
        ("4917632625469@c.us", Ident("pn_jid", "4917632625469@s.whatsapp.net")),
        ("4917632625469:12@s.whatsapp.net", Ident("pn_jid", "4917632625469@s.whatsapp.net")),
        ("46918273106072:5@lid", Ident("lid", "46918273106072@lid")),
        ("120363398765432101@newsletter", Ident("newsletter", "120363398765432101@newsletter")),
        ("4917623568044-1542142755@g.us", Ident("group", "4917623568044-1542142755@g.us")),
        ("4917623568044-1542142755", Ident("group", "4917623568044-1542142755@g.us")),
        ("120363407395534152@g.us", Ident("group", "120363407395534152@g.us")),
        ("4917632625469", Ident("numeric", "4917632625469")),
        ("+4917632625469", Ident("numeric", "4917632625469")),
        (4917632625469, Ident("numeric", "4917632625469")),
        ("service:speakup", Ident("assistant", "service:speakup")),
        ("DietmarDude", Ident("other", "DietmarDude")),
    ],
)
def test_classify(raw, expected):
    assert classify(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_classify_empty(raw):
    assert classify(raw) is None


def test_strong_kinds():
    assert classify("1@lid").strong and classify("1@s.whatsapp.net").strong
    assert classify("1@newsletter").strong
    assert not classify("12345678").strong
    assert not classify("1-2@g.us").strong


def test_numeric_part():
    assert numeric_part("4917632625469@s.whatsapp.net") == "4917632625469"
    assert numeric_part("46918273106072@lid") == "46918273106072"
    assert numeric_part("4917623568044-1542142755@g.us") == "4917623568044-1542142755"
