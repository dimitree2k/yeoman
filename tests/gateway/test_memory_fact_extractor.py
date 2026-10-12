"""Shared live screening and model JSON decoding contracts."""
from types import SimpleNamespace

import pytest
from yeoman_gateway.knowledge._json_extract import _extract_json
from yeoman_gateway.knowledge._screening import rescreen_stored_facts


def test_screens_refuse_what_the_real_backfill_stored() -> None:
    """Every example below was stored as a fact by the first real backfill."""
    from yeoman_gateway.knowledge._screening import screen_content

    refused = {
        "Ich habe Claude erklärt, wer Carsten ist, was er so will und was sein skill ist.":
            "conversation_reference",
        "Der Autor würde schauen, ob er sich das nach dem ersten Mal nochmal antun will.":
            "conversation_reference",
        "Der Fragesteller trinkt aktuell nur alkoholfreies Bier und Wasser.":
            "conversation_reference",
        "Wir haben besprochen, dass es am Freitag losgeht.": "conversation_reference",
        "Vielleicht ist das Treffen am Montag.": "hedged_statement",
        "Der Kurs könnte auf 200 steigen.": "hedged_statement",
        'Der Autor sagt: "noch tests".': "meta_statement",
    }
    for content, reason in refused.items():
        verdict = screen_content(content)
        assert verdict.rejected, f"should be refused: {content}"
        assert verdict.reason == reason, f"{content} -> {verdict.reason}"

    kept = [
        "Die private Krankenkasse beträgt 354€.",
        "GoPro wird mit Starman Optical verschmolzen.",
        "Der Stammtisch ist donnerstags.",
        "Wir haben einen Termin am Freitag.",  # a real agreement, not a conversation record
    ]
    for content in kept:
        assert screen_content(content).accepted, f"should be kept: {content}"


@pytest.mark.parametrize("text, expected", [
    ('{"statements": []}', {"statements": []}),
    ('```json\n{"statements": []}\n```', {"statements": []}),
    ('```\n[1, 2]\n```', [1, 2]),
    ('not json', None),
])
def test_model_json_response(text, expected):
    assert _extract_json(text) == expected


def test_rescreen_dry_run_and_apply_use_the_same_screen():
    good = SimpleNamespace(fact_id="good", content="Der Stammtisch ist donnerstags.", revoked_at_ms=None)
    noise = SimpleNamespace(fact_id="noise", content="Ich habe Claude erklärt, wer Carsten ist.", revoked_at_ms=None)
    retired = SimpleNamespace(fact_id="retired", content="Noise", revoked_at_ms=1)

    class StoredFacts:
        def list_facts(self, *, chat_scope_key):
            assert chat_scope_key == "synthetic-chat"
            return [good, noise, retired]

        def redact_fact(self, fact_id, *, now_ms):
            assert fact_id == noise.fact_id
            noise.revoked_at_ms = now_ms
            return True

    store = StoredFacts()
    dry = rescreen_stored_facts(store, chat_scope_key="synthetic-chat", dry_run=True, now_ms=2)
    assert (dry.checked, dry.kept, dry.revoked) == (2, 1, ("noise",))
    assert noise.revoked_at_ms is None
    applied = rescreen_stored_facts(store, chat_scope_key="synthetic-chat", dry_run=False, now_ms=3)
    assert (applied.checked, applied.kept, applied.revoked) == (2, 1, ("noise",))
    assert noise.revoked_at_ms == 3 and good.revoked_at_ms is None
    assert rescreen_stored_facts(store, chat_scope_key="synthetic-chat").revoked == ()
