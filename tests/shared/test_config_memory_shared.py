"""Plan 05 / Aufgabe 5: shared memory is opt-in and provably inert when switched off."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.app.bootstrap import build_processing_store, build_shared_fact_runtime
from yeoman_shared.config.schema import Config


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.memory.db_path = str(tmp_path / "memory.db")
    config.memory.capture.enabled = False
    config.memory.embedding.enabled = False
    return config


def test_shared_defaults_are_off() -> None:
    shared = Config().memory.shared

    assert shared.enabled is False
    assert shared.extraction_enabled is False
    assert shared.extractor_version == "shared-facts-v1"
    assert shared.max_jobs_waiting == 64
    assert shared.require_known_membership is True
    # The three knobs that were declared but never enforced are gone again, so no
    # operator can trust a control that does not exist.
    for dropped in ("max_candidates_per_job", "max_audience_size", "allow_author_only_facts"):
        assert not hasattr(shared, dropped)


def test_extraction_requires_the_shared_switch() -> None:
    config = Config()
    with pytest.raises(ValueError):
        config.memory.shared.extraction_enabled = True
        type(config.memory.shared).model_validate(config.memory.shared.model_dump())


def test_max_jobs_waiting_must_be_positive() -> None:
    config = Config()
    with pytest.raises(ValueError):
        config.memory.shared.max_jobs_waiting = 0
        type(config.memory.shared).model_validate(config.memory.shared.model_dump())


def test_disabled_mode_keeps_shared_memory_inert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.processing.enabled = False

    assert config.processing.enabled is False
    assert config.memory.shared.enabled is False
    assert build_processing_store(config) is None
    assert (
        build_shared_fact_runtime(
            config, store=None, processing=None, chat_registry=None, policy=None
        )
        is None
    )
    assert not (tmp_path / "memory.db").exists()


def test_shared_off_but_memory_on_builds_nothing(tmp_path: Path) -> None:
    from unittest.mock import patch

    from yeoman_gateway.memory.service import MemoryService

    config = _config(tmp_path)
    config.memory.enabled = True
    config.processing.enabled = True
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        memory = MemoryService(workspace=workspace, config=config.memory)

    runtime = build_shared_fact_runtime(
        config, store=object(), memory=memory, processing=object()
    )

    assert runtime is None
    assert memory.store.count_fact_jobs() == 0
    memory.close()


def test_enabled_switches_build_runtime_only_with_every_switch(tmp_path: Path) -> None:
    from unittest.mock import patch

    from yeoman_gateway.memory.service import MemoryService

    config = _config(tmp_path)
    config.memory.enabled = True
    config.memory.shared.enabled = True
    config.processing.enabled = True
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        memory = MemoryService(workspace=workspace, config=config.memory)

    assert build_shared_fact_runtime(config, store=None, memory=memory) is None  # no journal, no runtime

    runtime = build_shared_fact_runtime(
        config, store=object(), memory=memory, processing=object()
    )

    assert runtime is not None
    assert runtime.extraction is None  # extraction is a separate opt-in
    assert memory.extraction is None
    memory.close()


def test_extraction_opt_in_creates_a_queue_that_is_not_started(tmp_path: Path) -> None:
    from unittest.mock import patch

    from yeoman_gateway.memory.service import MemoryService

    config = _config(tmp_path)
    config.memory.enabled = True
    config.memory.shared.enabled = True
    config.memory.shared.extraction_enabled = True
    config.processing.enabled = True
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        memory = MemoryService(workspace=workspace, config=config.memory)

    runtime = build_shared_fact_runtime(
        config, store=object(), memory=memory, processing=object()
    )

    assert runtime is not None
    assert runtime.extraction_enabled is True
    assert memory.extraction is runtime.extraction
    # No token from the runtime was consumed until start() is called.
    assert memory.store.count_fact_jobs() == 0
    memory.close()
