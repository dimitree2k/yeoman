from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.a2a.relay import _RelayStore
from yeoman_gateway.agent.tools.a2a_research import A2AResearchStore, PendingResearch
from yeoman_gateway.media.document_cache import DocumentCache


def _fd_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def _assert_rollback_and_closed(store) -> None:
    with store._connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS closure_probe (value TEXT)")
    try:
        with store._connect() as connection:
            connection.execute("INSERT INTO closure_probe VALUES ('rolled back')")
            raise RuntimeError("rollback probe")
    except RuntimeError as exc:
        assert str(exc) == "rollback probe"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM closure_probe").fetchone()[0] == 0


@pytest.mark.parametrize("store_kind", ["relay", "research", "document"])
def test_store_connections_close_without_gc(tmp_path, store_kind: str) -> None:
    path = tmp_path / f"{store_kind}.db"
    if store_kind == "relay":
        store = _RelayStore(path)

        def operation(i: int) -> None:
            task_id = f"task-{i}"
            store.remember("peer", {"id": task_id}, "ctx", [])
            assert store.get("peer", task_id) is not None
    elif store_kind == "research":
        store = A2AResearchStore(path)

        def operation(i: int) -> None:
            task_id = f"task-{i}"
            store.put(PendingResearch(task_id, "worker", "research", "ctx", (), "telegram", "chat", f"effect-{i}"))
            assert store.pending()
            store.delete(task_id, effect_id=f"effect-{i}")
    else:
        store = DocumentCache(path)

        def operation(i: int) -> None:
            item_id = store.record_media_item(
                channel="telegram", chat_id="chat", message_id=f"message-{i}",
                sender_id=None, sender_name=None, kind="document", mime_type="text/plain",
                file_name="file.txt", local_path=tmp_path / "file.txt", size_bytes=1,
            )
            store.save_extraction(media_item_id=item_id, mode="text", content="ok")
            assert store.get_extraction(item_id, "text") is not None

    baseline = _fd_count()
    _assert_rollback_and_closed(store)
    assert _fd_count() == baseline
    for i in range(50):
        operation(i)
    assert _fd_count() == baseline
