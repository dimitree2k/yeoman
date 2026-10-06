from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware, merge_seen_chat_files
from yeoman_shared.utils.helpers import get_operational_store_path


def test_merge_unions_sources_and_keeps_earliest_known_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "seen_chats.json"
    data = tmp_path / "data-seen_chats.json"
    output = tmp_path / "ops" / "seen-chats.json"
    root_contents = '{"chats":["whatsapp:both@g.us","whatsapp:root@g.us"]}'
    data_contents = json.dumps(
        {
            "chats": ["whatsapp:both@g.us", "whatsapp:data@g.us", "whatsapp:unknown@g.us"],
            "first_seen": {
                "whatsapp:both@g.us": "2026-01-02T00:00:00+00:00",
                "whatsapp:data@g.us": "2026-01-03T00:00:00+00:00",
            },
        }
    )
    root.write_text(root_contents)
    data.write_text(data_contents)

    merge_seen_chat_files([root, data], output)
    first = output.read_text()
    merge_seen_chat_files([root, data], output)

    merged = json.loads(first)
    assert merged == {
        "chats": [
            "whatsapp:both@g.us",
            "whatsapp:data@g.us",
            "whatsapp:root@g.us",
            "whatsapp:unknown@g.us",
        ],
        "first_seen": {
            "whatsapp:both@g.us": "2026-01-02T00:00:00+00:00",
            "whatsapp:data@g.us": "2026-01-03T00:00:00+00:00",
        },
    }
    assert output.read_text() == first
    assert root.read_text() == root_contents
    assert data.read_text() == data_contents


def test_merge_picks_earliest_timestamp_from_both_sources(tmp_path: Path) -> None:
    older = tmp_path / "older.json"
    newer = tmp_path / "newer.json"
    output = tmp_path / "merged.json"
    older.write_text(json.dumps({"chats": {"whatsapp:one@g.us": "2026-01-01T00:00:00+00:00"}}))
    newer.write_text(json.dumps({"chats": {"whatsapp:one@g.us": "2026-02-01T00:00:00+00:00"}}))

    merge_seen_chat_files([newer, older], output)

    assert json.loads(output.read_text())["first_seen"] == {
        "whatsapp:one@g.us": "2026-01-01T00:00:00+00:00"
    }


def test_fresh_turn_writes_only_canonical_seen_chats(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    canonical = get_operational_store_path("seen_chats")
    middleware = NewChatNotifyMiddleware(owner_alert_resolver=lambda _channel: ["123"])
    event = InboundEvent(
        channel="whatsapp",
        chat_id="new@g.us",
        sender_id="sender",
        content="",
        timestamp=datetime(2026, 10, 6, tzinfo=UTC),
        raw_metadata={"group_name": "New group"},
    )

    middleware._maybe_notify(PipelineContext(event=event))

    assert canonical.exists()
    assert json.loads(canonical.read_text()) == {
        "chats": ["whatsapp:new@g.us"],
        "first_seen": {"whatsapp:new@g.us": "2026-10-06T00:00:00+00:00"},
    }
    assert not (tmp_path / "seen_chats.json").exists()
    assert not (tmp_path / "data" / "seen_chats.json").exists()
