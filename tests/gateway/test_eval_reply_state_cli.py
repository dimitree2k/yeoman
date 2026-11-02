"""CLI entry points and the blind owner review."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.evaluation.reply_state_run import (
    GRADER_ROUTE,
    REPLY_ROUTE,
)

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120000000000000042@g.us"
T0 = 1_790_000_000_000
MINUTE = 60_000
RUNNER = CliRunner()


def _history(tmp_path: Path) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    history.add_group(
        chat_id=CHAT,
        participants=[f"1000{index}@s.whatsapp.net" for index in range(3)],
        timestamp=(T0 - MINUTE) / 1000,
        name="Synthetic Review Group",
    )
    for index in range(10):
        history.add_message(
            chat_id=CHAT,
            message_id=f"m{index}",
            text=f"synthetic message {index}",
            timestamp=(T0 + index * 1000) / 1000,
            sender_id=f"1000{index % 3}@s.whatsapp.net",
            participant=f"1000{index % 3}@s.whatsapp.net",
            sender_name=f"p{index % 3}",
        )
    history.add_message(
        chat_id=CHAT,
        message_id="item",
        text="the item message",
        timestamp=(T0 + 10_000) / 1000,
        sender_id="10000@s.whatsapp.net",
        participant="10000@s.whatsapp.net",
        sender_name="anna",
    )
    history.snapshot().close()
    return history


def _install_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))
    history = _history(tmp_path)
    (home / "data" / "history").mkdir(parents=True)
    shutil.copy(history.path, home / "data" / "history" / "history.db")
    history.close()
    return home


def _run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "items.jsonl").write_text(
        json.dumps(
            {
                "item_id": "reply:item",
                "kind": "reply",
                "chat_id": CHAT,
                "at_ms": T0 + 10_000,
                "stratum": "addressed-answered",
                "hard_case": True,
                "history_id": "item",
                "native_id": "item",
                "reply_to_history_id": None,
                "reply_to_native_id": None,
            }
        )
        + "\n"
    )
    with (directory / "candidates.jsonl").open("w", encoding="utf-8") as handle:
        for arm, text in (
            ("A", "CANDIDATE-A-VOICE"),
            ("S", "CANDIDATE-S-VOICE"),
            ("P", "CANDIDATE-P-VOICE"),
            ("A2", "CANDIDATE-A2-VOICE"),
        ):
            handle.write(json.dumps({"item_id": "reply:item", "arm": arm, "text": text}) + "\n")
    return directory


def test_review_file_blind_and_key_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_home(tmp_path, monkeypatch)
    directory = _run_dir(tmp_path)
    out = tmp_path / "review.md"

    result = RUNNER.invoke(
        app,
        [
            "eval",
            "reply-state-review",
            str(directory),
            "--sample",
            "1",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    review = out.read_text()
    key = json.loads(Path(f"{out}.key.json").read_text())
    assert "Synthetic Review Group" in review
    assert "the item message" in review
    assert "CANDIDATE-A-VOICE" in review
    assert "CANDIDATE-S-VOICE" in review
    assert "Better: X / Y / same" in review
    assert "S_vs_A" not in review and "arm" not in review
    assert key["sample"] == 1
    assert key["items"][0]["item_id"] == "reply:item"
    assert {key["items"][0]["x"], key["items"][0]["y"]} == {"A", "S"}
    assert "CANDIDATE" not in json.dumps(key)


def test_review_file_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_home(tmp_path, monkeypatch)
    directory = _run_dir(tmp_path)
    out = tmp_path / "review.md"

    result = RUNNER.invoke(
        app,
        ["eval", "reply-state-review", str(directory), "--sample", "1", "--out", str(out)],
    )

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{out}.key.json").stat().st_mode) == 0o600


def test_cli_dry_estimate_prints_calls_per_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_gateway.cli.core as cli_core
    import yeoman_gateway.cli.eval_commands as commands
    import yeoman_gateway.evaluation.participation_context_ab as ab
    import yeoman_gateway.evaluation.reply_state_run as run_module
    import yeoman_gateway.history.live as history_live
    from yeoman_shared.config import loader
    from yeoman_shared.config.schema import Config

    class FakeHistory:
        def __init__(self, path: Path) -> None:
            self.path = path

        def close(self) -> None:
            return None

    class FakeEngine:
        def resolve_policy(self, channel: str, chat_id: str) -> SimpleNamespace:
            return SimpleNamespace(persona_file=None)

        def persona_text(self, persona_file: str | None) -> None:
            return None

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def prepare(self, run, chat_ids=None):
            from yeoman_gateway.evaluation.reply_state_run import PlannedCalls

            return SimpleNamespace(
                planned=PlannedCalls(
                    routes={REPLY_ROUTE: 4, GRADER_ROUTE: 8},
                    prompt_tokens=1_000,
                    completion_tokens=200,
                    items=1,
                ),
                replies=(),
                writer=(),
                selection=SimpleNamespace(shortfall={"replies": 0, "writer": 0}),
            )

        def set_clients(self, clients) -> None:
            return None

    history_file = tmp_path / "history.db"
    history_file.write_text("")
    monkeypatch.setattr(history_live, "history_db_path", lambda: history_file)
    monkeypatch.setattr(ab, "FrozenHistory", FakeHistory)
    monkeypatch.setattr(loader, "load_config", lambda: Config())
    monkeypatch.setattr(run_module, "ReplyStateRunner", FakeRunner)
    monkeypatch.setattr(
        commands,
        "_participation_replay_setup",
        lambda config: SimpleNamespace(
            replay_args={
                "builder": None,
                "guidance_for": lambda channel, chat_id: "",
                "conversation_bounds": None,
                "window_bounds": None,
            }
        ),
    )
    monkeypatch.setattr(cli_core, "make_policy_engine", lambda config: (FakeEngine(), None))

    result = RUNNER.invoke(app, ["eval", "reply-state", "--days", "30"])

    assert result.exit_code == 0, result.output
    assert f"calls {REPLY_ROUTE}: 4" in result.output
    assert f"calls {GRADER_ROUTE}: 8" in result.output
    assert "no model calls; rerun with --yes" in result.output


def test_knowledge_snapshot_is_consistent_and_includes_the_wal(tmp_path: Path) -> None:
    import sqlite3

    from yeoman_gateway.cli.eval_commands import snapshot_knowledge_db

    live_path = tmp_path / "live.db"
    live = sqlite3.connect(live_path)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE t (v INTEGER)")
    live.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    live.commit()  # still only in the WAL: nothing is checkpointed
    (tmp_path / "snap").mkdir()

    copy_path = snapshot_knowledge_db(live_path, tmp_path / "snap")
    live.execute("INSERT INTO t VALUES (100)")  # a write after the snapshot
    live.commit()

    copy = sqlite3.connect(f"{copy_path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
        live.close()
