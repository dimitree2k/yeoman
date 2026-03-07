import pytest
from yeoman.memory.core_blocks import CoreMemoryBlock, CoreMemoryBlockStore


def test_block_creation():
    block = CoreMemoryBlock(label="user_facts", value="Likes coffee.")
    assert block.label == "user_facts"
    assert block.value == "Likes coffee."
    assert block.max_chars == 2000


def test_store_get_set(tmp_path):
    store = CoreMemoryBlockStore(tmp_path / "core_blocks.json")
    store.set("session1", CoreMemoryBlock(label="user_facts", value="Likes coffee."))
    block = store.get("session1", "user_facts")
    assert block is not None
    assert block.value == "Likes coffee."


def test_store_list_blocks(tmp_path):
    store = CoreMemoryBlockStore(tmp_path / "core_blocks.json")
    store.set("s1", CoreMemoryBlock(label="user_facts", value="A"))
    store.set("s1", CoreMemoryBlock(label="scratchpad", value="B"))
    labels = [b.label for b in store.list_blocks("s1")]
    assert set(labels) == {"user_facts", "scratchpad"}


def test_store_persistence(tmp_path):
    path = tmp_path / "core_blocks.json"
    store1 = CoreMemoryBlockStore(path)
    store1.set("s1", CoreMemoryBlock(label="notes", value="Hello"))
    store1.save()

    store2 = CoreMemoryBlockStore(path)
    block = store2.get("s1", "notes")
    assert block is not None
    assert block.value == "Hello"


def test_block_replace():
    block = CoreMemoryBlock(label="user_facts", value="Likes coffee. Hates tea.")
    block.replace("Hates tea.", "Loves tea.")
    assert block.value == "Likes coffee. Loves tea."


def test_block_replace_missing_raises():
    block = CoreMemoryBlock(label="user_facts", value="Likes coffee.")
    with pytest.raises(ValueError, match="not found"):
        block.replace("Hates tea.", "Loves tea.")


def test_block_append_respects_max_chars():
    block = CoreMemoryBlock(label="notes", value="A" * 1990, max_chars=2000)
    with pytest.raises(ValueError, match="exceed"):
        block.append("B" * 20)


def test_block_append():
    block = CoreMemoryBlock(label="notes", value="Fact 1.", max_chars=2000)
    block.append(" Fact 2.")
    assert block.value == "Fact 1. Fact 2."
