import json

from hist_fixtures import _bf, sample_layer1, write_jsonl
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest, verify

runner = CliRunner()


def test_verify_reports(tmp_path):
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / "out" / "history.db"
    project([live, dev], db)
    report = verify([live, dev], db, scratch=tmp_path / "scratch")
    cov = report["coverage"]
    assert (cov["messages_with_identifier"], cov["with_contact"], cov["with_confirmed_contact"]) == (3, 3, 2)
    assert cov["meets_95_percent"] is False
    assert [p["identifiers"] for p in cov["provisional_contacts"]] == [["4915253696948"]]
    assert cov["unresolved_messages"] == []
    assert report["accounting_ok"] is True and report["deterministic"] is True
    assert report["digests"] == table_digest(db)


def test_verify_review_keeps_unmatched_purged_events(tmp_path):
    live, dev = sample_layer1(tmp_path)
    write_jsonl(dev / "backfill/unmatched.jsonl", [
        _bf("journal", "reaction", {"targetMessageId": "missing", "senderId": "4915253696948"})
    ])
    db = tmp_path / "out" / "history.db"
    built = project([live, dev], db)
    report = verify([live, dev], db, scratch=None)
    assert built["review"]["unmatched_event_payloads"]
    assert report["review"]["unmatched_event_payloads"] == built["review"]["unmatched_event_payloads"]


def test_cli_project_and_verify(tmp_path):
    live, dev = sample_layer1(tmp_path)
    db = tmp_path / "out" / "history.db"
    built = runner.invoke(app, ["history", "project", "--layer1", str(live), "--layer1", str(dev), "--db", str(db)])
    assert built.exit_code == 0, built.output
    assert json.loads(built.output)["messages"] == 4
    checked = runner.invoke(app, ["history", "verify", "--layer1", str(live), "--layer1", str(dev), "--db", str(db)])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["accounting_ok"] is True
