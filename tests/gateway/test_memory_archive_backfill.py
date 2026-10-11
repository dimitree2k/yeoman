"""Preserved metadata-only lineage inventory contract."""

import json


def test_legacy_lineage_inventory_is_metadata_only(tmp_path) -> None:
    """The migration inventory never reads content and never schedules model work."""
    from yeoman_gateway.knowledge._migration import import_lineage, inspect_lineage_sources

    archive = tmp_path / "inbound"
    archive.mkdir()
    (archive / "whatsapp_chat.jsonl").write_text(
        json.dumps({"chat_id": "chat@g.us", "channel": "whatsapp"})
        + "\n"
        + json.dumps(
            {
                "message_id": "m-1",
                "timestamp": 1_700_000_000_000,
                "from": "4915@s.whatsapp.net",
                "content": "legacy text",
            }
        ),
        encoding="utf-8",
    )

    inventory = inspect_lineage_sources(inbound_dir=archive)
    assert inventory.counts()["import"] == 1
    assert "legacy text" not in inventory.to_json()

    report = import_lineage(inventory, apply=True, allow_model_jobs=False)
    assert report.model_jobs_scheduled == 0
    assert report.eligible_model_jobs == inventory.eligible_model_jobs
