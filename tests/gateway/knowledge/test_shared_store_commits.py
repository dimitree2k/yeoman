"""Low-level KnowledgeStore commit and rollback boundaries, using synthetic rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._store import KnowledgeStore


@pytest.fixture
def service(tmp_path: Path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    try:
        yield store
    finally:
        store.close()


def _other_writer_can_lock(db: Path) -> bool:
    connection = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def _memory_meta(db: Path, key: str) -> str | None:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT value FROM memory2_meta WHERE key = ?", (key,)
        ).fetchone()
    return None if row is None else str(row[0])


def test_opening_the_knowledge_store_leaves_no_open_transaction(service) -> None:
    assert not service.connection.in_transaction  # noqa: SLF001
    assert _other_writer_can_lock(service.db_path)  # noqa: SLF001


def test_a_low_level_write_outside_an_operation_is_committed(service) -> None:
    service.execute("INSERT INTO memory2_meta(key,value) VALUES (?,?)", ("synthetic-key", "synthetic-value"))
    service.commit_if_idle()

    assert _memory_meta(service.db_path, "synthetic-key") == "synthetic-value"  # noqa: SLF001
    assert _other_writer_can_lock(service.db_path)  # noqa: SLF001


def test_a_low_level_contacts_write_outside_an_operation_is_committed(service) -> None:
    person = "synthetic-person"
    service.execute("INSERT INTO contacts(id,display_name,created_at,updated_at,is_owner) VALUES (?,?,?,?,?)",
                    (person, "Synthetic Person", "date", "date", 1))
    service.commit_if_idle()

    db = service.db_path  # noqa: SLF001
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        flag = connection.execute(
            "SELECT is_owner FROM contacts WHERE id = ?", (person,)
        ).fetchone()[0]
    assert flag == 1
    assert _other_writer_can_lock(db)


def test_a_low_level_write_inside_a_failed_operation_rolls_back_with_it(service) -> None:
    with pytest.raises(RuntimeError):
        with service.transaction():
            service.execute("INSERT INTO memory2_meta(key,value) VALUES (?,?)", ("inside-key", "inside-value"))
            raise RuntimeError("synthetic failure")

    assert _memory_meta(service.db_path, "inside-key") is None  # noqa: SLF001
