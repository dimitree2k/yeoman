import uuid

from yeoman_gateway.history.attestations import make, parse, seed_records
from yeoman_gateway.history.ids import Ident, classify
from yeoman_gateway.history.layer1 import Layer1Line
from yeoman_gateway.history.resolve import NAMESPACE, ContactRecord, IdentityInput, resolve

FRANK_LID, FRANK_PN = "46918273106072@lid", "4917632625469@s.whatsapp.net"


def _att(record, n=1):
    return parse(Layer1Line(f"owner/attestations.jsonl#{n}", record))


def test_frank_one_contact_from_three_spellings():
    inp = IdentityInput()
    raw = "whatsapp/2026-10.jsonl#1"
    inp.see(classify(FRANK_LID), 2000, raw)
    inp.see(classify(FRANK_PN), 2000, raw)
    inp.link(classify(FRANK_LID), classify(FRANK_PN), "native_pair", raw)
    inp.see(classify("4917632625469"), 1000, "backfill/memory.jsonl#5")
    inp.name(FRANK_PN, "Frank Taeger", 2000, raw)
    inp.contact_record(ContactRecord("945ae43e", 100, "Frank Taeger", None, "backfill/knowledge.jsonl#1"))
    inp.contact_record(ContactRecord("31025a3e", 200, None, None, "backfill/knowledge.jsonl#2"))
    inp.bind("945ae43e", classify(FRANK_PN), "backfill/knowledge.jsonl#3")
    inp.bind("31025a3e", classify(FRANK_LID), "backfill/knowledge.jsonl#4")
    res = resolve(inp)
    assert res.resolve(classify(FRANK_LID)) == ("945ae43e", "exact")
    assert res.resolve(classify("4917632625469")) == ("945ae43e", "numeric_match")
    rows = {c.contact_id: c for c in res.contacts}
    assert rows["31025a3e"].merged_into == "945ae43e"
    assert rows["945ae43e"].display_name == "Frank Taeger" and rows["945ae43e"].status == "confirmed"
    idents = {(r.kind, r.value): r for r in res.identifiers if r.contact_id == "945ae43e"}
    assert idents[("lid", FRANK_LID)].evidence == "native_pair"
    assert idents[("pn_jid", FRANK_PN)].first_seen_ms == 1000
    assert idents[("push_name", "Frank Taeger")].strength == "weak"
    assert res.review["merged_knowledge_contacts"][0]["merged"] == ["31025a3e"]


def test_ambiguous_number_stays_numeric_and_is_reviewed():
    inp = IdentityInput()
    for value in ("123456789@lid", "123456789@s.whatsapp.net", "123456789"):
        inp.see(classify(value), 1, "whatsapp/2026-10.jsonl#1")
    res = resolve(inp)
    contact, match = res.resolve(classify("123456789"))
    assert match == "exact" and contact not in (res.node_contact["123456789@lid"],
                                                res.node_contact["123456789@s.whatsapp.net"])
    assert res.review["numeric_ambiguous"] == [
        {"value": "123456789", "candidates": ["123456789@lid", "123456789@s.whatsapp.net"]}]
    assert {c.contact_id: c.status for c in res.contacts}[contact] == "provisional"


def test_wrong_binding_merges_until_owner_unmerges():
    owner_pn, test_pn, test_lid = "491757070305@s.whatsapp.net", "4915550000000@s.whatsapp.net", "99999999999999@lid"
    inp = IdentityInput()
    inp.contact_record(ContactRecord("O", 1, "Dimi", None, "backfill/knowledge.jsonl#1"))
    inp.contact_record(ContactRecord("T", 2, "Test", None, "backfill/knowledge.jsonl#2"))
    inp.bind("O", classify(owner_pn), "backfill/knowledge.jsonl#3")
    inp.bind("T", classify(test_pn), "backfill/knowledge.jsonl#4")
    inp.bind("O", classify(test_lid), "backfill/knowledge.jsonl#5")
    inp.link(classify(test_lid), classify(test_pn), "native_pair", "whatsapp/2026-10.jsonl#9")
    merged = resolve(inp)
    assert merged.resolve(classify(owner_pn))[0] == merged.resolve(classify(test_pn))[0] == "O"
    assert merged.review["merged_knowledge_contacts"]
    inp.attestations.append(_att(make("unmerge", 5, "test contact is not me", a=owner_pn, b=test_pn)))
    split = resolve(inp)
    assert split.resolve(classify(owner_pn))[0] == "O"
    assert split.resolve(classify(test_pn))[0] == "T"
    assert split.review["blocked_by_unmerge"]


def test_later_merge_or_unmerge_wins_for_the_same_pair():
    a, b = FRANK_PN, "4915550000000@s.whatsapp.net"
    for earlier, latest, should_merge in (("merge", "unmerge", False), ("unmerge", "merge", True)):
        inp = IdentityInput()
        inp.see(classify(a), 1, "raw#1")
        inp.see(classify(b), 2, "raw#2")
        inp.attestations.extend([
            _att(make(earlier, 10, "earlier intent", a=a, b=b), 10),
            _att(make(latest, 20, "later intent", a=a, b=b), 20),
        ])
        resolved = resolve(inp)
        assert (resolved.resolve(classify(a))[0] == resolved.resolve(classify(b))[0]) is should_merge


def test_identifier_ended_is_reported_as_metadata_not_applied():
    inp = IdentityInput()
    inp.see(classify(FRANK_PN), 100, "raw#1")
    att = _att(make("identifier_ended", 200, "recorded end", identifier=FRANK_PN, ended_ms=150), 1)
    inp.attestations.append(att)
    resolved = resolve(inp)
    assert resolved.identifiers[0].ended_ms == 150
    assert resolved.review["identifier_ended_not_applied"] == [{
        "ref": att.ref, "identifier": FRANK_PN, "ended_ms": 150,
        "resolution": "not applied to resolution",
    }]


def test_seed_attestations_create_arvid_owner_matthias():
    inp = IdentityInput()
    inp.attestations.extend(_att(r, n) for n, r in enumerate(seed_records(), start=1))
    res = resolve(inp)
    arvid = str(uuid.uuid5(NAMESPACE, "4915202777685@s.whatsapp.net"))
    assert res.role_contact["assistant"] == arvid
    assert res.resolve(Ident("assistant", "service:speakup")) == (arvid, "exact")
    names = {c.display_name: c for c in res.contacts}
    assert names["Matthias Hoffmann"].status == "confirmed" and names["Dimi"].role == "owner"
    assert {r.evidence for r in res.identifiers} == {"owner_attested"}
    assert res.contact_for_anchor("4915140189391@s.whatsapp.net") == names["Matthias Hoffmann"].contact_id


def test_channel_group_provisional_and_determinism():
    inp = IdentityInput()
    inp.see(classify("120363398765432101@newsletter"), 5, "backfill/session_jsonl.jsonl#1")
    inp.see(classify("120363407395534152@g.us"), None, "whatsapp/2026-10.jsonl#2")
    inp.see(classify("120363407395534152"), 6, "backfill/journal.jsonl#3")
    inp.see(classify("4915111111111"), 7, "backfill/memory.jsonl#4")
    inp.name("4915111111111", "Tom", 7, "backfill/memory.jsonl#4")
    res = resolve(inp)
    kinds = {c.contact_id: c for c in res.contacts}
    channel = res.node_contact["120363398765432101@newsletter"]
    assert kinds[channel].kind == "channel"
    assert res.resolve(classify("120363407395534152")) == (None, "group")
    tom = res.resolve(classify("4915111111111"))[0]
    assert kinds[tom].status == "provisional" and kinds[tom].display_name == "Tom"
    assert resolve(inp) == res
