"""Small real-projection contract for shared live consumer fixtures."""

from yeoman_gateway.history.queries import HistoryQueries

from tests.gateway.history_test_helpers import ProjectedHistory


async def test_fixture_projects_messages_identity_membership_and_reply_media(tmp_path):
    phone = "4915000000001@s.whatsapp.net"
    chat = "synthetic@g.us"
    case = ProjectedHistory(
        tmp_path,
        groups=[
            dict(chat_id=chat, participants=[phone], timestamp=1700000000, name="Synthetic group")
        ],
        contacts=[dict(identifiers=[phone], name="Synthetic name", timestamp=1700000000)],
        messages=[
            dict(
                chat_id=chat, message_id="m1", sender_id=phone, text="hello", timestamp=1700000001
            ),
            dict(
                chat_id=chat,
                message_id="m2",
                sender_id=phone,
                text="reply",
                timestamp=1700000002,
                reply_to_message_id="m1",
                mentions=[phone],
                media={"kind": "image"},
            ),
        ],
    )
    try:
        async with case.turn() as snapshot:
            queries = HistoryQueries(snapshot)
            assert queries.chats()[0]["subject"] == "Synthetic group"
            row = queries.native_message(chat_id=chat, native_id="m2")
            assert row["current_text"] == "reply"
            assert row["reply_to_native_id"] == "m1"
            assert "image" in row["media_json"]
            assert phone in row["mentions_json"]
            assert queries.members(chat_id=chat, at_ms=1700000002000).status == "known"
            from yeoman_gateway.people.disclosure import reader_context, render_person
            from yeoman_gateway.people.resolver import resolve_person

            from tests.gateway.people.resolver_fixtures import synced_directory

            directory = synced_directory(snapshot, tmp_path / "directory")
            try:
                reader = reader_context(
                    directory,
                    channel="whatsapp",
                    chat_id=chat,
                    principal_id="whatsapp:4915000000001",
                    is_owner=False,
                    at_ms=1700000002000,
                    history=snapshot,
                )
                person = resolve_person("Synthetic name", reader)
                assert person.person_id is not None
                assert render_person(person.person_id, reader).names == ("Synthetic name",)
            finally:
                directory.close()
    finally:
        case.close()
    assert all(snapshot._closed for snapshot in case.snapshots)
