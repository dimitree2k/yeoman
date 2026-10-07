import json

import pytest
from yeoman_gateway.history.attestations import (
    SEED_AT_MS,
    append,
    make,
    parse,
    seed_records,
    write_seed,
)
from yeoman_gateway.history.layer1 import Layer1Line
from yeoman_shared.raw_archive.paths import ProtectedPathError


def test_seed_has_arvid_owner_and_matthias():
    seeds = {r["identifiers"][0]: r for r in seed_records()}
    assert seeds["4915202777685@s.whatsapp.net"]["role"] == "assistant"
    assert seeds["491757070305@s.whatsapp.net"]["role"] == "owner"
    assert seeds["4915140189391@s.whatsapp.net"]["name"] == "Matthias Hoffmann"
    assert all(r["at_ms"] == SEED_AT_MS for r in seed_records())


def test_write_seed_once(tmp_path):
    path = write_seed(tmp_path)
    assert path == tmp_path / "owner" / "attestations.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    with pytest.raises(FileExistsError):
        write_seed(tmp_path)


def test_parse_roundtrip():
    record = make("unmerge", 5, "test", a="1@lid", b="2@s.whatsapp.net")
    att = parse(Layer1Line("owner/attestations.jsonl#4", record))
    assert (att.type, att.at_ms, att.ref) == ("unmerge", 5, "owner/attestations.jsonl#4")
    assert att.fields == {"a": "1@lid", "b": "2@s.whatsapp.net"}


@pytest.mark.parametrize(
    "record",
    [
        {"type": "merge", "at_ms": 1, "a": "1@lid"},
        {"type": "merge", "at_ms": 1, "a": "1@lid", "b": "4917632625469"},
        {"type": "teleport", "at_ms": 1},
        {"type": "contact", "at_ms": 1, "identifiers": ["1@lid"], "role": "king"},
        {"type": "contact", "at_ms": "yesterday", "identifiers": ["1@lid"]},
    ],
)
def test_parse_rejects(record):
    with pytest.raises(ValueError):
        parse(Layer1Line("owner/attestations.jsonl#1", record))


def test_parse_rejects_non_object():
    with pytest.raises(ValueError):
        parse(Layer1Line("owner/attestations.jsonl#1", None))


def test_append_validates_and_appends(tmp_path):
    path = write_seed(tmp_path)
    append(path, make("name", 7, "nickname", anchor="4915140189391@s.whatsapp.net", name="Matze"))
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["name"] == "Matze"
    with pytest.raises(ValueError):
        append(path, {"type": "name", "at_ms": 7, "anchor": "Matze", "name": "x"})


def test_append_refuses_protected_path_before_open(tmp_path, monkeypatch):
    path = tmp_path / "raw" / "owner" / "attestations.jsonl"
    monkeypatch.setattr("yeoman_gateway.history.attestations.is_protected", lambda target: True)
    with pytest.raises(ProtectedPathError):
        append(path, make("name", 7, "nickname", anchor="4915140189391@s.whatsapp.net", name="Matze"))
    assert not path.exists()


def test_attestation_windows_validate_types_and_order():
    fields = {"anchor": "1@lid", "identifier": "2@s.whatsapp.net"}
    for bounds in ({}, {"valid_from_ms": None, "valid_until_ms": None},
                   {"valid_from_ms": 100}, {"valid_until_ms": 200},
                   {"valid_from_ms": 100, "valid_until_ms": 200}):
        record = make("identifier", 5, "ownership", **fields, **bounds)
        assert parse(Layer1Line("owner/attestations.jsonl#1", record)).fields == fields | bounds
    for bounds in ({"valid_from_ms": True}, {"valid_until_ms": False},
                   {"valid_from_ms": 1.5}, {"valid_until_ms": 2.0},
                   {"valid_from_ms": "100"}, {"valid_until_ms": []},
                   {"valid_from_ms": 200, "valid_until_ms": 100},
                   {"valid_from_ms": 100, "valid_until_ms": 100}):
        with pytest.raises(ValueError):
            make("identifier", 5, "invalid window", **fields, **bounds)
    for identifier in ("bad@lid", "@s.whatsapp.net", "1x@newsletter", "1:2:3@lid", "telegram:abc",
                       "telegram:", "other:123", "1@lid\n", 123, True):
        with pytest.raises(ValueError):
            make("identifier", 5, "invalid identifier", anchor="1@lid", identifier=identifier)


def test_author_and_legacy_attestations_round_trip():
    for ref in ("whatsapp/2026-10.jsonl#176", "backfill/memory.jsonl#9/0",
                "backfill/memory.jsonl#9/6"):
        record = make("author", 10, "owner correction", source_ref=ref, anchor="1@lid")
        assert record["attestation_version"] == 2
        att = parse(Layer1Line("owner/attestations.jsonl#3", record))
        assert (att.type, att.at_ms, att.fields) == (
            "author", 10, {"source_ref": ref, "anchor": "1@lid"})
    for ref in ("/whatsapp/x.jsonl#1", "../x.jsonl#1", "backfill/../x.jsonl#1",
                "whatsapp/x.jsonl#0", "whatsapp/x.jsonl#-1", "whatsapp/x.jsonl#01",
                "whatsapp/x.jsonl#1/-1", "whatsapp/x.jsonl#1/01", "whatsapp/x.jsonl#1/",
                "whatsapp/x.jsonl#1/0/1", "whatsapp/x.jsonl#1\n", "whatsapp/x.jsonl", 1):
        with pytest.raises(ValueError):
            make("author", 10, "invalid ref", source_ref=ref, anchor="1@lid")
    valid = make("author", 10, "correction", source_ref="whatsapp/x.jsonl#1", anchor="1@lid")
    for field in ("source_ref", "anchor", "at_ms", "note"):
        invalid = {key: value for key, value in valid.items() if key != field}
        with pytest.raises(ValueError):
            parse(Layer1Line("owner/attestations.jsonl#1", invalid))
    for fields in ({"at_ms": True}, {"at_ms": 1.0}, {"note": None}, {"note": ""},
                   {"note": "   "}, {"note": 1}, {"anchor": "telegram:453897507"},
                   {"anchor": "1@newsletter"}, {"anchor": "1@g.us"},
                   {"attestation_version": True}, {"attestation_version": 3}):
        with pytest.raises(ValueError):
            parse(Layer1Line("owner/attestations.jsonl#1", valid | fields))
    legacy = {"attestation_version": 1, "type": "message_author", "at_ms": 5,
              "by": "owner", "note": "old correction", "message_id": "M1", "anchor": "1@lid"}
    assert parse(Layer1Line("owner/attestations.jsonl#2", legacy)).fields == {
        "message_id": "M1", "anchor": "1@lid"}
    legacy.pop("attestation_version")
    assert parse(Layer1Line("owner/attestations.jsonl#2", legacy)).type == "message_author"


def test_author_precedence_across_legacy_and_source_ref():
    from itertools import permutations

    from yeoman_gateway.history import attestations

    assert callable(getattr(attestations, "resolve_author_targets", None)), "Task 3 target resolver missing"
    resolve_author_targets = attestations.resolve_author_targets
    from yeoman_gateway.history.extract import extract

    chat, a, b = '93000@g.us', '93001@lid', '93002@lid'
    lines = [Layer1Line('whatsapp/messages.jsonl#1', {
        'channel': 'whatsapp', 'kind': 'message', 'native': {'type': 'message', 'payload': {
            'chatJid': chat, 'messageId': 'SYNTHETIC', 'senderId': a, 'text': 'synthetic text'}}})]
    ex = extract(lines)
    claims = [parse(Layer1Line(ref, make(kind, at, 'synthetic decision', anchor=anchor, **fields)))
              for ref, kind, at, anchor, fields in [
                  ('owner/a.jsonl#1', 'message_author', 20, a, {'message_id': f'whatsapp:{chat}:SYNTHETIC'}),
                  ('owner/b.jsonl#1', 'author', 10, b, {'source_ref': lines[0].ref}),
                  ('owner/z.jsonl#1', 'author', 20, b, {'source_ref': lines[0].ref})]]
    expected = None
    for order in permutations(claims):
        winners, review = resolve_author_targets(order, ex.messages, ex.events)
        assert winners == {lines[0].ref: claims[2]}
        assert len(review) == 1 and review[0]['reason'] == 'conflicting_author_claims'
        assert review[0]['winner_ref'] == claims[2].ref
        assert {c['attestation_ref'] for c in review[0]['claims']} == {c.ref for c in claims}
        assert [c['anchor'] for c in review[0]['claims']].count(a) == 1
        if expected is None:
            expected = review
        assert review == expected
    winners, _ = resolve_author_targets(claims[:2], ex.messages, ex.events)
    assert winners[lines[0].ref] == claims[0]  # timestamp beats type/source-ref form
    assert ex.messages[0].sender_raw == a
    for target in (123, True, {"invalid": "target"}, ["invalid"]):
        invalid = parse(Layer1Line("owner/invalid.jsonl#1", make(
            "message_author", 1, "synthetic malformed legacy target", message_id=target, anchor=a)))
        winners, review = resolve_author_targets([invalid], ex.messages, ex.events)
        assert not winners and review[0]["reason"] == "invalid_target"
