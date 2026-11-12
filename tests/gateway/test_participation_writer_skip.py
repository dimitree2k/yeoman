"""The Writer may decline a comment that would only retell the chat."""

from yeoman_gateway.adapters.responder_llm import (
    _PARTICIPATION_DRAFT_RULES,
    PARTICIPATION_SKIP_TOKEN,
    _declined_draft,
)


def test_skip_token_empties_the_draft() -> None:
    assert _declined_draft(PARTICIPATION_SKIP_TOKEN) == ""
    assert _declined_draft(f"  {PARTICIPATION_SKIP_TOKEN}\n") == ""


def test_real_draft_and_none_pass_unchanged() -> None:
    assert _declined_draft("Lötkolben ab 30 €.") == "Lötkolben ab 30 €."
    assert _declined_draft(None) is None


def test_writer_prompt_offers_the_skip_token() -> None:
    prompt = _PARTICIPATION_DRAFT_RULES
    assert PARTICIPATION_SKIP_TOKEN in prompt
    assert "one version" in prompt
