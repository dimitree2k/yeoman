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


def test_telegram_identifier_stored_not_projected():
    from yeoman_gateway.history.attestations import make, parse
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import Layer1Line

    ident = classify("telegram:453897507")
    assert ident == Ident("telegram", "telegram:453897507")
    assert not ident.strong
    record = make("identifier", 5, "stored only", anchor="1@lid", identifier=ident.value)
    line = Layer1Line("owner/attestations.jsonl#1", record)
    assert parse(line).fields["identifier"] == "telegram:453897507"
    contact = make("contact", 5, "stored identifiers", identifiers=["1@lid", ident.value])
    assert parse(Layer1Line("owner/attestations.jsonl#2", contact)).fields["identifiers"] == [
        "1@lid", ident.value]
    out = extract([line, Layer1Line("owner/attestations.jsonl#2", contact),
                   Layer1Line("backfill/session.jsonl#1", {
                       "backfill_version": 1, "channel": "telegram", "kind": "message",
                       "payload": {"messageId": "T1", "text": "synthetic Telegram message"}})])
    assert not out.messages and not out.events
    assert len(out.attestations) == 2
    for value in ("telegram:", "telegram:abc", "telegram:-1", "telegram:1.5",
                  "telegram:1:2", "discord:453897507"):
        with pytest.raises(ValueError):
            make("identifier", 5, "invalid syntax", anchor="1@lid", identifier=value)
