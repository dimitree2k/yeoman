from __future__ import annotations

import json
from pathlib import Path

import pytest
from yeoman_gateway.evaluation.harness import (
    RecordingClient,
    StaticClient,
    build_today_judge_context,
    make_judge,
)
from yeoman_gateway.evaluation.scenarios import load_scenario_file

CAST = {
    "people": {
        "frank": {"name": "Frank", "sender_id": "491700000001@s.whatsapp.net"},
        "kevin": {"name": "Kevin", "sender_id": "491700000003@s.whatsapp.net"},
    },
    "chats": {
        "a": {"chat_id": "120000000000000001@g.us", "members": ["frank"]},
        "b": {"chat_id": "120000000000000002@g.us", "members": ["frank", "kevin"]},
    },
}


def _write(tmp_path: Path, scenarios: list[dict]) -> Path:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"cast": CAST, "scenarios": scenarios}), encoding="utf-8")
    return path


def _scenario(**overrides: object) -> dict:
    data = {
        "id": "T1",
        "title": "t",
        "category": "discretion",
        "modes": ["A"],
        "requires": [],
        "now": "2026-10-05T18:00:00Z",
        "messages": [
            ["a1", "a", "frank", "2026-09-20T19:00:00Z", "Unser Hund ist gestorben."],
            ["b0", "b", "kevin", "2026-10-05T14:00:00Z", "zu alt für das Fenster"],
            ["b1", "b", "kevin", "2026-10-05T17:58:00Z", "Hundewitz 😂"],
        ],
        "trigger": {"chat": "b", "kind": "inbound", "source_ids": ["b1"]},
        "expected": {"actions": ["silence"]},
    }
    data.update(overrides)
    return data


def test_scenario_file_loads_with_cast(tmp_path: Path) -> None:
    [scenario] = load_scenario_file(_write(tmp_path, [_scenario()]))
    assert scenario.chats["b"].chat_id == "120000000000000002@g.us"
    assert scenario.messages[0].sender == "frank"
    assert scenario.trigger.source_ids == ("b1",)


def test_unknown_feature_in_requires_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="awarness"):
        load_scenario_file(_write(tmp_path, [_scenario(requires=["awarness"])]))


def test_unknown_sender_or_chat_is_rejected(tmp_path: Path) -> None:
    bad = _scenario(messages=[["x", "zz", "frank", "2026-10-05T17:58:00Z", "t"]])
    with pytest.raises(ValueError, match="unknown chat"):
        load_scenario_file(_write(tmp_path, [bad]))


async def test_today_view_is_same_chat_and_window_bounded(tmp_path: Path) -> None:
    [scenario] = load_scenario_file(_write(tmp_path, [_scenario()]))
    _opportunity, context, build_ms = await build_today_judge_context(scenario)
    ids = [message["event_id"] for message in context["messages"]]
    assert ids == ["b1"]  # a1 is another chat; b0 is older than 120 minutes.
    assert build_ms >= 0


async def test_static_and_recording_clients_drive_the_real_judge(tmp_path: Path) -> None:
    [scenario] = load_scenario_file(_write(tmp_path, [_scenario()]))
    opportunity, context, _ = await build_today_judge_context(scenario)
    client = RecordingClient(
        StaticClient('{"action": "silence", "intent": "initiate", "reason": "q"}')
    )
    decision = await make_judge(client, allowed_emojis=("👍",)).decide(opportunity, context)
    assert decision.action == "silence"
    assert client.last is not None and client.last.content.startswith("{")
