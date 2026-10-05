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
