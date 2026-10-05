from hist_fixtures import make_db
from yeoman_gateway.history.convert.memory_nodes import convert_memory_nodes

FULL = """CREATE TABLE memory2_nodes (id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, kind TEXT,
  content TEXT, source_message_id TEXT, source_role TEXT, meta_json TEXT, created_at TEXT,
  is_deleted INTEGER, contact_id TEXT);"""
OLD = """CREATE TABLE memory2_nodes (id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, kind TEXT,
  content TEXT, source_message_id TEXT, source_role TEXT, meta_json TEXT, created_at TEXT,
  is_deleted INTEGER);"""
G = "4917623568044-1542142755@g.us"


def _node(**kw):
    base = {"channel": "whatsapp", "chat_id": G, "sender_id": None, "kind": "utterance", "content": "",
            "source_message_id": None, "source_role": "user", "meta_json": "{}",
            "created_at": "2026-05-28T12:26:52.418461+00:00", "is_deleted": 0}
    return {**base, **kw}


def test_memory_nodes(tmp_path):
    make_db(tmp_path / "data/memory/memory.db", FULL, {"memory2_nodes": [
        _node(id="a", sender_id="4915127589549", contact_id="c30fa618",
              content="[group_notes_batch] [4917623568044] [Image] [image_description] This image shows",
              source_message_id="ACEB758A"),
        _node(id="b", sender_id="491757070305", content="allright, und wieso abverkauf?",
              source_message_id="3EB05B47", contact_id="e31b9a09"),
        _node(id="c", source_role="assistant", content="Weil heute", meta_json='{"direction": "out"}'),
        _node(id="d", kind="preference", content="mag Kaffee"),
        _node(id="e", sender_id="1", content="weg", is_deleted=1),
    ]})
    a, b, c, d, e = list(convert_memory_nodes(tmp_path, "data/memory/memory.db", "memory"))
    assert a["provenance"] == "derived_only" and a["time_certainty"] == "capture_time_approx"
    assert a["payload"] == {"chatJid": G, "messageId": "ACEB758A", "senderId": "4915127589549",
                            "contactRef": "c30fa618", "generatedDescription": "This image shows",
                            "mediaKind": "image"}
    assert b["provenance"] == "verbatim_unverified" and b["payload"]["text"] == "allright, und wieso abverkauf?"
    assert c["direction"] == "out" and c["payload"]["fromAssistant"] is True and "senderId" not in c["payload"]
    assert (d["kind"], d["skip_reason"]) == ("memory_fact", "derived_memory_fact")
    assert (e["kind"], e["skip_reason"]) == ("message", "deleted_in_source")
    assert a["origin"] == {**a["origin"], "store": "memory", "table": "memory2_nodes", "row_key": "a"}


def test_old_backup_without_contact_id(tmp_path):
    rel = "data/memory/backups/memory-20260911-160126-pre-rebackfill.db"
    make_db(tmp_path / rel, OLD, {"memory2_nodes": [_node(id="x", sender_id="491757070305", content="hi")]})
    (line,) = convert_memory_nodes(tmp_path, rel, "memory_pre_rebackfill")
    assert line["payload"]["senderId"] == "491757070305" and "contactRef" not in line["payload"]
