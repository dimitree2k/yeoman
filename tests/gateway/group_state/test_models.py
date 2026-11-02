"""Round-trip tests for the group-state value objects."""

from __future__ import annotations

import json

from yeoman_gateway.group_state.models import GroupState, Item, Section


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


def test_section_without_items_json_unchanged() -> None:
    # The v1 document JSON, pinned byte for byte: adding ``items`` must not change it.
    expected = (
        '{"data": {"last_message_ms": 900, "messages_30m": 2}, "key": "presence", '
        '"layer": "live", "period_end_ms": null, "period_start_ms": null, '
        '"produced_at_ms": 1000, "schema_version": 1, "source_cursor": "m3", '
        '"sources": ["m1", "m2", "m3"], "trust": "runtime", "ttl_ms": 600000}'
    )
    assert _section().to_json() == expected
    assert Section.from_json(expected).items == ()


def test_item_round_trip() -> None:
    item = Item(
        id="label-conversation-1",
        kind="label",
        data={"label": "weekend plans"},
        sources=("m1", "m2"),
        source_fingerprints={"m1": "fp1", "m2": "fp2"},
        produced_at_ms=1_100,
        ttl_ms=86_400_000,
        trust="derived",
        conversation_id="conversation-1",
        person_id=None,
    )
    section = _section(items=(item,))
    restored = Section.from_json(section.to_json())
    assert restored == section
    assert restored.items == (item,)
    assert restored.items[0].source_fingerprints == {"m1": "fp1", "m2": "fp2"}

    state = _state()
    state = GroupState(
        channel=state.channel,
        chat_id=state.chat_id,
        schema_version=2,
        revision=state.revision,
        history_cursor=state.history_cursor,
        updated_ms=state.updated_ms,
        sections={"presence": section},
    )
    assert GroupState.from_json(state.to_json()) == state


def test_required_age_uses_oldest_required_section() -> None:
    state = GroupState(
        channel="whatsapp",
        chat_id="group@g.us",
        schema_version=2,
        revision=1,
        history_cursor="m3",
        updated_ms=1_500,
        sections={
            "presence": _section("presence", produced_at_ms=1_000),
            "budgets": _section("budgets", produced_at_ms=900),
            "conversations": _section("conversations", produced_at_ms=500),
        },
    )
    assert state.required_age_ms(1_500, ("presence", "budgets")) == 600
    assert state.required_age_ms(1_500, ("conversations",)) == 1_000
    assert state.required_age_ms(1_500, ("commitments",)) is None


def test_item_expired() -> None:
    item = Item(
        id="label-1",
        kind="label",
        data={},
        sources=("m1",),
        source_fingerprints={"m1": "fp1"},
        produced_at_ms=1_000,
        ttl_ms=500,
        trust="derived",
    )
    assert item.expired(1_500) is False
    assert item.expired(1_501) is True
