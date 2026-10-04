"""Explicit CLI receipts for protected rebuilt history reads."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.knowledge._history import HistoricalJournal

from tests.gateway.test_history_reader import GROUP, NOW, READER, _add_event


def test_history_search_recent_excerpt_and_reindex_are_explicit(
    tmp_path: Path,
) -> None:
    target = tmp_path / "history"
    with HistoricalJournal(target) as journal:
        _add_event(journal)
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "authorization_ref": "trusted-read-test",
                "principal_id": READER,
                "channel": "whatsapp",
                "chat_id": GROUP,
                "recipient_principals": [READER],
                "membership_revision": "roster-r1",
                "policy_revision": 7,
                "purpose": "reply",
                "now_ms": NOW + 10_000,
                "is_direct": False,
                "owner": True,
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "policy_revision": 7,
                "memberships": [
                    {
                        "channel": "whatsapp",
                        "account": "acct-primary",
                        "chat_id": GROUP,
                        "members": [READER],
                        "revision": "roster-r1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    common = [
        "--target-home",
        str(target),
        "--channel",
        "whatsapp",
        "--account",
        "acct-primary",
        "--chat-id",
        GROUP,
        "--authorization",
        str(authorization),
        "--policy",
        str(policy),
    ]
    runner = CliRunner()

    search = runner.invoke(app, ["knowledge", "history", "search", *common, "--query", "broker"])
    assert search.exit_code == 0, search.output
    payload = json.loads(search.output)
    assert payload["metadata_receipt"]["count"] == 1
    assert payload["text_receipt"][0]["text"] == "Broker Stillhalter YTD 2025"

    recent = runner.invoke(
        app,
        ["knowledge", "history", "recent", *common, "--before-ms", str(NOW + 1)],
    )
    assert recent.exit_code == 0, recent.output
    assert json.loads(recent.output)["text_receipt"][0]["event_id"] == "wa-history-1"

    excerpt = runner.invoke(
        app,
        [
            "knowledge",
            "history",
            "excerpt",
            *common,
            "--event-id",
            "wa-history-1",
            "--revision",
            "1",
        ],
    )
    assert excerpt.exit_code == 0, excerpt.output
    assert json.loads(excerpt.output)["text_receipt"][0]["native_id"] == "native-123"

    reindex = runner.invoke(
        app, ["knowledge", "history", "reindex", "--target-home", str(target)]
    )
    assert reindex.exit_code == 0, reindex.output
    assert json.loads(reindex.output)["metadata_receipt"]["supported"] is True


def test_history_text_commands_require_explicit_trusted_inputs(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "history",
            "search",
            "--target-home",
            str(tmp_path / "history"),
            "--query",
            "broker",
            "--channel",
            "whatsapp",
            "--account",
            "acct-primary",
            "--chat-id",
            GROUP,
        ],
    )
    assert result.exit_code != 0
    help_result = CliRunner().invoke(app, ["knowledge", "history", "search", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--authorization" in help_result.output
    assert "--policy" in help_result.output
    assert "--owner" not in help_result.output
