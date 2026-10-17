"""The 28 invented cases are a normal-CI accuracy gate, independent of timing."""


def test_synthetic_gold_is_hard_gate(gold_directory, eval_module):
    p = gold_directory
    report = eval_module.evaluate(p.people, p.history, p.gold)
    assert report["queries"] == 28
    assert report["wrong_person"] == 0
    assert report["expected_outcomes"] / report["queries"] >= 0.95
    assert sum(report[k] for k in ("resolved", "ambiguous", "not_found")) == 28


def test_gold_has_exact_fixture_outcomes_and_is_read_only(gold_directory, eval_module):
    p = gold_directory
    before_people = tuple(p.case.writer.iterdump())
    before_history = tuple(p.case.hdb.iterdump())
    report = eval_module.evaluate(p.people, p.history, p.gold)
    assert report["expected_outcomes"] == 28
    assert tuple(p.case.writer.iterdump()) == before_people
    assert tuple(p.case.hdb.iterdump()) == before_history


def test_gold_scope_and_past_author_disclosure(gold_directory):
    from dataclasses import replace

    from yeoman_gateway.people.disclosure import reader_context, render_person
    from yeoman_gateway.people.resolver import resolve_person

    c = gold_directory.case
    reader = reader_context(
        c.directory,
        channel="whatsapp",
        chat_id="g1@g.us",
        principal_id="whatsapp:4915000000011",
        is_owner=False,
        at_ms=300,
        history=c.snapshot,
    )
    assert resolve_person("Past Author", reader).person_id == 13
    assert render_person(13, reader).phones == ()
    other = replace(reader, kind="other_dm", chat_id="4915000000011@s.whatsapp.net")
    assert resolve_person("Secret Person", other).status == "not_found"
