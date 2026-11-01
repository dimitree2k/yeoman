"""Round-trip tests for the group-state value objects."""

from __future__ import annotations

import json

from yeoman_gateway.group_state.models import GroupState, Section


def _section(key: str = "presence", **overrides: object) -> Section:
    values: dict[str, object] = {
        "key": key,
        "schema_version": 1,
        "trust": "runtime",
        "layer": "live",
        "produced_at_ms": 1_000,
        "ttl_ms": 600_000,
        "source_cursor": "m3",
        "period_start_ms": None,
        "period_end_ms": None,
        "sources": ("m1", "m2", "m3"),
        "data": {"messages_30m": 2, "last_message_ms": 900},
    }
    values.update(overrides)
    return Section(**values)  # type: ignore[arg-type]


def _state(revision: int = 1) -> GroupState:
    return GroupState(
        channel="whatsapp",
        chat_id="group@g.us",
        schema_version=1,
        revision=revision,
        history_cursor="m3",
        updated_ms=1_200,
        sections={"presence": _section()},
    )


def test_roundtrip_json() -> None:
    state = _state()
    encoded = state.to_json()
    payload = json.loads(encoded)
    assert payload["channel"] == "whatsapp"
    assert payload["sections"]["presence"]["sources"] == ["m1", "m2", "m3"]
    assert GroupState.from_json(encoded) == state


def test_age_is_never_negative() -> None:
    assert _state().age_ms(1_500) == 300
    assert _state().age_ms(1_000) == 0


def test_sources_are_capped_at_500() -> None:
    section = _section(sources=tuple(f"m{i}" for i in range(600)))
    assert len(section.sources) == 500
    assert section.sources[-1] == "m499"


def test_roundtrip_normalizes_nested_tuples() -> None:
    section = _section(data={"ids": ("a", "b"), "nested": {"values": (1, 2)}})
    restored = Section.from_json(section.to_json())
    assert restored == section
    assert restored.data == {"ids": ["a", "b"], "nested": {"values": [1, 2]}}
