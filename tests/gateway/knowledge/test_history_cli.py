from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.knowledge._history_audience import roster_confirmation


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_metadata_only_and_no_bootstrap(tmp_path: Path, monkeypatch) -> None:
    from yeoman_gateway.app import bootstrap

    def forbidden(*args, **kwargs):
        raise AssertionError("history CLI must not bootstrap runtime")

    monkeypatch.setattr(bootstrap, "build_gateway_runtime", forbidden)
    records = _write_json(
        tmp_path / "records.json",
        {
            "events": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "channel": "whatsapp",
                    "account": "account-1",
                    "chat_id": "group-1",
                    "occurred_ms": 1_790_000_000_000,
                    "time_certainty": "exact",
                    "sender_raw": "whatsapp:proposal",
                    "principal": "whatsapp:unverified",
                    "native_evidence": [],
                    "body": "secret body must never be printed",
                }
            ]
        },
    )
    target_home = tmp_path / "history-target"

    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "history",
            "coverage",
            "--records",
            str(records),
            "--target-home",
            str(target_home),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "secret body" not in result.output
    assert "coverage" in result.output.lower()
    assert (target_home / "data" / "processing.db").is_file()


def test_roster_review_keeps_current_members_display_only(tmp_path: Path) -> None:
    records = _write_json(
        tmp_path / "records.json",
        {
            "events": [
                {
                    "event_id": "event-1",
                    "revision": 1,
                    "channel": "whatsapp",
                    "account": "account-1",
                    "chat_id": "group-1",
                    "occurred_ms": None,
                    "time_certainty": "unknown",
                    "sender_raw": "whatsapp:proposal",
                    "principal": None,
                    "native_evidence": [],
                }
            ]
        },
    )
    current = _write_json(
        tmp_path / "current.json",
        {"whatsapp/account-1/group-1": ["whatsapp:current-only"]},
    )
    target_home = tmp_path / "history-target"
    runner = CliRunner()
    coverage_result = runner.invoke(
        app,
        [
            "knowledge",
            "history",
            "coverage",
            "--records",
            str(records),
            "--target-home",
            str(target_home),
        ],
    )
    assert coverage_result.exit_code == 0, coverage_result.output

    result = runner.invoke(
        app,
        [
            "knowledge",
            "history",
            "roster-review",
            "--records",
            str(records),
            "--target-home",
            str(target_home),
            "--current-members",
            str(current),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "whatsapp:current-only" not in result.output
    assert "unknown" in result.output.lower()


def test_roster_attest_cli_uses_explicit_policy_snapshot(tmp_path: Path) -> None:
    members = ["whatsapp:owner", "whatsapp:alice"]
    confirmation = roster_confirmation(
        channel="whatsapp", account="account-1", chat_id="group-1", members=members,
        valid_from_ms=100, valid_until_ms=200,
    )
    roster = _write_json(
        tmp_path / "roster.json",
        {
            "channel": "whatsapp",
            "account": "account-1",
            "chat_id": "group-1",
            "members": members,
            "valid_from_ms": 100,
            "valid_until_ms": 200,
            "confirmation": confirmation,
        },
    )
    authorization = _write_json(
        tmp_path / "authorization.json",
        {
            "actor_principal": "whatsapp:owner",
            "authorization_ref": "policy:history-test",
            "policy_revision": 7,
            "owner": False,
        },
    )
    policy = _write_json(
        tmp_path / "policy.json",
        {"policy_revision": 7, "owners": {"whatsapp": ["owner"]}},
    )
    target_home = tmp_path / "history-target"

    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "history",
            "roster-attest",
            "--target-home",
            str(target_home),
            "--roster",
            str(roster),
            "--authorization",
            str(authorization),
            "--policy",
            str(policy),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "attested" in result.output.lower()
