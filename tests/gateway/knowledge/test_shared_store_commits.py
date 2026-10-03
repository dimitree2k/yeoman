"""Writes through the shared knowledge connection must not stay uncommitted.

The memory and contacts adapters join the knowledge store's single connection.  A write
they make outside a knowledge operation used to stay in an open implicit SQLite
transaction until the next operation happened to commit it: the write lock was held the
whole time (other writers got ``database is locked``) and the write was lost if that
next operation failed.  Outside an operation a shared write now commits at once; inside
one it still commits or rolls back together with it.  All data is synthetic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority


@pytest.fixture
def service(tmp_path: Path):
    opened = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="shared-store-commit-tests",
        source_authority=FakeSourceAuthority(),
        policy_authority=FakePolicyAuthority(),
    )
    try:
        yield opened
    finally:
        opened.close()


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


def test_opening_the_shared_memory_store_leaves_no_open_transaction(service) -> None:
    service.memory_store()

    assert not service._store.connection.in_transaction  # noqa: SLF001
    assert _other_writer_can_lock(service._store.db_path)  # noqa: SLF001


def test_a_shared_memory_write_outside_an_operation_is_committed(service) -> None:
    memory = service.memory_store()

    memory.set_meta("synthetic-key", "synthetic-value")

    assert _memory_meta(service._store.db_path, "synthetic-key") == "synthetic-value"  # noqa: SLF001
    assert _other_writer_can_lock(service._store.db_path)  # noqa: SLF001


def test_a_shared_contacts_write_outside_an_operation_is_committed(service) -> None:
    contacts = service.contacts_store()
    person = contacts.create_contact(display_name="Synthetic Person")

    contacts.set_owner(person.id, is_owner=True)

    db = service._store.db_path  # noqa: SLF001
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        flag = connection.execute(
            "SELECT is_owner FROM contacts WHERE id = ?", (person.id,)
        ).fetchone()[0]
    assert flag == 1
    assert _other_writer_can_lock(db)


def test_a_shared_write_inside_a_failed_operation_rolls_back_with_it(service) -> None:
    memory = service.memory_store()

    with pytest.raises(RuntimeError):
        with service._store.transaction():  # noqa: SLF001
            memory.set_meta("inside-key", "inside-value")
            raise RuntimeError("synthetic failure")

    assert _memory_meta(service._store.db_path, "inside-key") is None  # noqa: SLF001
