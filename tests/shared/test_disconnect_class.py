from yeoman_shared.disconnect_class import classify_disconnect


def test_whatsapp_logout_needs_human() -> None:
    assert classify_disconnect("whatsapp", 401) == ("logged_out", "needs_human")


def test_whatsapp_restart_required_after_pairing() -> None:
    assert classify_disconnect("whatsapp", 515) == ("restart_required", "needs_restart")


def test_unknown_and_missing_codes_are_transient() -> None:
    assert classify_disconnect("whatsapp", 999) == ("unknown", "transient")
    assert classify_disconnect("whatsapp", None) == ("unknown", "transient")
    assert classify_disconnect("signal", 401) == ("unknown", "transient")


def test_telegram_revoked_token_needs_human() -> None:
    assert classify_disconnect("telegram", 401) == ("unauthorized", "needs_human")
    assert classify_disconnect("telegram", 429) == ("rate_limited", "transient")


def test_override_changes_class_only() -> None:
    assert classify_disconnect("whatsapp", 440, {440: "needs_human"}) == (
        "connection_replaced",
        "needs_human",
    )
