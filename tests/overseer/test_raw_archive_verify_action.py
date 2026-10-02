from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.deterministic import (
    DeterministicExecutor,
    parse_deterministic_actions,
)
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent


class _Comms:
    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: str) -> None:
        self.sent.append(message)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


async def test_verify_action_passes_on_a_clean_archive(home: Path) -> None:
    RawArchive().append(RawEvent(channel="whatsapp", kind="message", direction="in", native={}))
    comms = _Comms()
    result = await DeterministicExecutor(comms=CascadingComms(channels=[comms])).execute(
        "verify_raw_archive", target="default"
    )
    assert result.success is True
    assert comms.sent == []


async def test_verify_action_alerts_on_problems(home: Path) -> None:
    archive = RawArchive()
    archive.append(RawEvent(channel="whatsapp", kind="message", direction="in", native={"a": 1}))
    archive.append(RawEvent(channel="whatsapp", kind="message", direction="in", native={"a": 2}))
    executor = DeterministicExecutor(comms=CascadingComms(channels=[_Comms()]))
    await executor.execute("verify_raw_archive", target="default")
    [month_file] = list((home / "data" / "raw" / "whatsapp").glob("*.jsonl"))
    month_file.write_text(month_file.read_text().splitlines()[0] + "\n")
    comms = _Comms()
    result = await DeterministicExecutor(comms=CascadingComms(channels=[comms])).execute(
        "verify_raw_archive", target="default"
    )
    assert result.success is False
    assert len(comms.sent) == 1 and "Raw archive integrity problem" in comms.sent[0]


def test_starter_runbook_parses_to_the_verify_action() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/overseer/yeoman_overseer/starter_runbooks/ops-raw-archive-verify.md"
    )
    body = path.read_text(encoding="utf-8").split("---", 2)[2]
    [action] = parse_deterministic_actions(body)
    assert action.action == "verify_raw_archive" and action.target == "default"
