from __future__ import annotations

import json

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent, WhatsAppChannel
from yeoman_gateway.storage.chat_registry import ChatRegistry
from yeoman_shared.config.schema import WhatsAppConfig


@pytest.mark.asyncio
async def test_ingest_event_uses_policy_comment_as_group_registry_name(
    tmp_path, monkeypatch
) -> None:
    yeoman_home = tmp_path / "yeoman-home"
    monkeypatch.setenv("YEOMAN_HOME", str(yeoman_home))
    yeoman_home.mkdir()
    (yeoman_home / "policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "channels": {
                    "whatsapp": {
                        "chats": {
                            "120363400000000100@g.us": {
                                "comment": "Ente",
                            }
                        }
                    }
                },
            }
        )
    )
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, bridge_url="ws://localhost:3001", bridge_token="secret"),
        MessageBus(),
    )

    event = InboundEvent(
        message_id="m-reg-policy-name-1",
        chat_jid="120363400000000100@g.us",
        participant_jid="111@s.whatsapp.net",
        sender_id="111",
        sender_phone_jid=None,
        is_group=True,
        text="hello named registry",
        timestamp=103,
        mentioned_jids=[],
        mentioned_bot=False,
        reply_to_bot=False,
        reply_to_message_id=None,
        reply_to_participant=None,
        reply_to_text=None,
        media_kind=None,
        media_type=None,
        media_path=None,
        media_bytes=None,
        media_file_name=None,
        media_description=None,
        voice_transcript=None,
    )

    await channel._ingest_inbound_event(event)

    registry = ChatRegistry()
    row = registry.get_chat("whatsapp", "120363400000000100@g.us")
    registry.close()

    assert row is not None
    assert row["chat_type"] == "group"
    assert row["readable_name"] == "Ente"


def test_bridge_metadata_subject_replaces_existing_registry_fallback(tmp_path) -> None:
    registry = ChatRegistry(db_path=tmp_path / "chat_registry.db")
    registry.register_chat(
        channel="whatsapp",
        chat_id="120363400000000100@g.us",
        chat_type="group",
        readable_name="Ente",
    )

    registry.sync_from_bridge_metadata(
        "whatsapp",
        [
            {
                "chatJid": "120363400000000100@g.us",
                "subject": "Real WhatsApp Subject",
            }
        ],
    )

    row = registry.get_chat("whatsapp", "120363400000000100@g.us")
    registry.close()

    assert row is not None
    assert row["readable_name"] == "Real WhatsApp Subject"


class _DeniedVerdict:
    denied = True
    reason = "blocked_sender"
    decision = None


class _DenyingGate:
    def admit(self, request):  # noqa: ANN001 - stub
        return _DeniedVerdict()


@pytest.mark.asyncio
async def test_blocked_message_is_still_archived(tmp_path, monkeypatch) -> None:
    """The owner wants a complete inbound record: a refused message is recorded too."""
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    yeoman_home = tmp_path / "yeoman-home"
    monkeypatch.setenv("YEOMAN_HOME", str(yeoman_home))
    yeoman_home.mkdir()
    channel = WhatsAppChannel(
        WhatsAppConfig(enabled=True, bridge_url="ws://localhost:3001", bridge_token="secret"),
        MessageBus(),
    )
    archive = InboundArchive(db_path=tmp_path / "archive.db", retention_days=None)
    channel.inbound_archive = archive
    channel._processing_gate = _DenyingGate()

    event = InboundEvent(
        message_id="m-blocked-1",
        chat_jid="blocked@s.whatsapp.net",
        participant_jid="999@s.whatsapp.net",
        sender_id="999",
        sender_phone_jid="999@s.whatsapp.net",
        is_group=False,
        text="this sender is blocked",
        timestamp=104,
        mentioned_jids=[],
        mentioned_bot=False,
        reply_to_bot=False,
        reply_to_message_id=None,
        reply_to_participant=None,
        reply_to_text=None,
        media_kind=None,
        media_type=None,
        media_path=None,
        media_bytes=None,
        media_file_name=None,
        media_description=None,
        voice_transcript=None,
    )

    await channel._ingest_inbound_event(event)

    rows = archive._conn.execute(
        "SELECT chat_id, message_id, text FROM inbound_messages WHERE message_id = ?",
        ("m-blocked-1",),
    ).fetchall()
    archive.close()
    assert len(rows) == 1
    assert rows[0]["text"] == "this sender is blocked"
