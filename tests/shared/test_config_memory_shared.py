"""Plan 05 / Aufgabe 5: shared memory is opt-in and provably inert when switched off."""

from __future__ import annotations

import pytest
from yeoman_shared.config.schema import Config


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






