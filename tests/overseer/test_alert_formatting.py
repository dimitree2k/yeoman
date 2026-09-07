from yeoman_overseer.alerts.formatting import format_overseer_alert


def test_info_prefix_for_no_action_needed_message() -> None:
    message = (
        "ops-memory-prune (cron) — checks completed; prune not executed.\n"
        "- Would-prune count: 0. Nothing matched the prune criteria."
    )

    assert format_overseer_alert(message).startswith("🟢 INFO ")


def test_action_prefix_for_aborted_runbook_message() -> None:
    message = "ops-source-cleanup ABORTED. Deletion skipped per runbook safety."

    assert format_overseer_alert(message).startswith("🟡 ACTION ")


def test_critical_prefix_for_down_message() -> None:
    message = "Gateway down after repeated restart failures."

    assert format_overseer_alert(message).startswith("🔴 CRITICAL ")


def test_existing_prefix_is_preserved() -> None:
    message = "🟡 ACTION ops-source-cleanup ABORTED."

    assert format_overseer_alert(message) == message
