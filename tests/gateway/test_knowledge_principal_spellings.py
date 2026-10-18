"""Author spellings stay syntactic: one identifier, several exact forms."""

from yeoman_gateway.knowledge._retrieval import principal_spellings

OWNER_LID = "whatsapp:20002@lid"
OWNER_PHONE_FORM = "whatsapp:10002"


def test_principal_spellings_are_syntactic_not_fuzzy():
    forms = principal_spellings(OWNER_LID)
    assert set(forms) == {
        "20002", "+20002", "20002@lid", "20002@s.whatsapp.net",
        "whatsapp:20002", "whatsapp:+20002", "whatsapp:20002@lid",
        "whatsapp:20002@s.whatsapp.net",
    }
    # No prefix, substring or neighbouring number may appear: only the same identifier.
    assert not {form for form in forms if "1000" in form or "2000" in form and "20002" not in form}
    assert principal_spellings("telegram:12345") == ("telegram:12345",)
    assert principal_spellings("whatsapp:") == ()
