"""The bounded judge view of one group state."""

from __future__ import annotations

from typing import Any

from yeoman_gateway.group_state.models import GroupState, Section
from yeoman_gateway.group_state.render import render_judge_view

NOW = 10_000_000
MINUTE = 60_000
CHAT = "120000000000000042@g.us"


def _section(
    key: str,
    data: dict[str, Any],
    *,
    produced_at_ms: int = NOW - 1_000,
    ttl_ms: int = 600_000,
) -> Section:
    return Section(
        key=key,
        schema_version=1,
        trust="runtime",
        layer="live",
        produced_at_ms=produced_at_ms,
        ttl_ms=ttl_ms,
        source_cursor="m1",
        period_start_ms=None,
        period_end_ms=None,
        sources=("m1",),
        data=data,
    )


def _state(
    sections: dict[str, Section],
    *,
    revision: int = 3,
    updated_ms: int = NOW - 2 * MINUTE,
) -> GroupState:
    return GroupState(
        channel="whatsapp",
        chat_id=CHAT,
        schema_version=1,
        revision=revision,
        history_cursor="m1",
        updated_ms=updated_ms,
        sections=sections,
    )


def _presence(
    *,
    last_message_ms: int | None = NOW - 4 * MINUTE,
    arvid_last_ms: int | None = NOW - 16 * MINUTE,
) -> dict[str, Any]:
    return {
        "active_30m": [
            {"contact_id": "c1", "name": "Alice", "messages": 2},
            {"contact_id": "c2", "name": "Bob", "messages": 1},
        ],
        "active_120m": [
            {"contact_id": "c1", "name": "Alice", "messages": 2},
            {"contact_id": "c2", "name": "Bob", "messages": 1},
        ],
        "messages_30m": 3,
        "messages_120m": 5,
        "last_message_ms": last_message_ms,
        "arvid_last_ms": arvid_last_ms,
    }


def _budgets(*, limit: int | None = 6) -> dict[str, Any]:
    return {
        "judge_calls_last_hour": 2,
        "judge_hourly_limit": limit,
        "last_comment_ms": NOW - 25 * MINUTE,
        "comments_last_30m": 1,
        "reactions_last_30m": 2,
    }


def _arvid_actions() -> dict[str, Any]:
    return {
        "items": [
            {
                "kind": "judge",
                "outcome": "comment",
                "detail_code": "",
                "at_ms": NOW - 25 * MINUTE,
                "opportunity_id": "opp-synthetic-1",
            },
        ],
        "counts_24h": {
            "sent": 2,
            "comments": 1,
            "reactions": 3,
            "silences": 0,
            "failures": 0,
        },
    }


def _conversations() -> dict[str, Any]:
    return {
        "conversations": [
            {
                "conversation_id": "conversation-1",
                "start_ms": NOW - 20 * MINUTE,
                "last_ms": NOW - 2 * MINUTE,
                "state": "ended",
                "messages": 4,
                "participants": ["c1", "c2", "c3"],
                "burst": False,
                "arvid_messages": 1,
                "arvid_addressed": True,
                "thread_ids": [],
                "first_message_id": "m1",
                "last_message_id": "m4",
            },
            {
                "conversation_id": "conversation-2",
                "start_ms": NOW - 90 * MINUTE,
                "last_ms": NOW - 80 * MINUTE,
                "state": "ended",
                "messages": 2,
                "participants": ["c2"],
                "burst": False,
                "arvid_messages": 0,
                "arvid_addressed": False,
                "thread_ids": [],
                "first_message_id": "m5",
                "last_message_id": "m6",
            },
        ]
    }


def _commitments() -> dict[str, Any]:
    return {
        "decisions": [
            {"id": "d1", "created_by": "c1", "due_at_ms": NOW + MINUTE, "status": "open"}
        ],
        "polls": [
            {"poll_id": "p1", "opened_ms": NOW - MINUTE, "closed_ms": None},
            {"poll_id": "p2", "opened_ms": NOW - 2 * MINUTE, "closed_ms": None},
        ],
        "reminders": [{"job_id": "j1", "at_ms": NOW + 60 * MINUTE}],
    }


def _people_activity() -> dict[str, Any]:
    return {
        "people": [
            {
                "contact_id": "c1",
                "name": "Alice",
                "messages_24h": 3,
                "messages_7d": 12,
                "last_seen_ms": NOW - 4 * MINUTE,
                "conversations_24h": 2,
                "reactions_given_7d": 1,
                "reactions_received_7d": 2,
                "replies_to_arvid_7d": 1,
            },
            {
                "contact_id": "c2",
                "name": "Bob",
                "messages_24h": 1,
                "messages_7d": 2,
                "last_seen_ms": NOW - 2 * 60 * MINUTE,
                "conversations_24h": 1,
                "reactions_given_7d": 0,
                "reactions_received_7d": 0,
                "replies_to_arvid_7d": 0,
            },
            *[
                {
                    "contact_id": f"c{index}",
                    "name": f"Person {index}",
                    "messages_24h": 0,
                    "messages_7d": 1,
                    "last_seen_ms": NOW - index * MINUTE,
                    "conversations_24h": 0,
                    "reactions_given_7d": 0,
                    "reactions_received_7d": 0,
                    "replies_to_arvid_7d": 0,
                }
                for index in range(3, 8)
            ],
        ],
        "truncated": False,
    }


def _full_state(*, updated_ms: int = NOW - 2 * MINUTE) -> GroupState:
    return _state(
        {
            "presence": _section("presence", _presence()),
            "budgets": _section("budgets", _budgets()),
            "arvid_actions": _section("arvid_actions", _arvid_actions()),
            "conversations": _section("conversations", _conversations()),
            "commitments": _section("commitments", _commitments()),
            "people_activity": _section("people_activity", _people_activity()),
        },
        updated_ms=updated_ms,
    )


def test_render_none_is_no_state_fallback() -> None:
    rendered = render_judge_view(None, now_ms=NOW)
    assert rendered.text == ""
    assert rendered.revision is None
    assert rendered.age_ms is None
    assert rendered.sections == ()
    assert rendered.dropped == ()
    assert rendered.fallback == "no_state"


def test_render_stale_while_active_falls_back() -> None:
    rendered = render_judge_view(_full_state(updated_ms=NOW - 700_000), now_ms=NOW)
    assert rendered.text == ""
    assert rendered.revision == 3
    assert rendered.age_ms == 700_000
    assert rendered.sections == ()
    assert rendered.fallback == "stale"


def test_render_stale_but_quiet_still_renders() -> None:
    state = _state(
        {
            "presence": _section(
                "presence",
                _presence(
                    last_message_ms=NOW - 2 * 60 * MINUTE,
                    arvid_last_ms=NOW - 3 * 60 * MINUTE,
                ),
            ),
        },
        updated_ms=NOW - 700_000,
    )
    rendered = render_judge_view(state, now_ms=NOW)
    assert rendered.fallback is None
    assert rendered.text.startswith("[Group State] revision 3, 11 min old")
    assert "presence:" in rendered.text
    assert "Alice" in rendered.text


def test_render_drops_expired_section_by_ttl() -> None:
    state = _state(
        {
            "presence": _section("presence", _presence()),
            "budgets": _section(
                "budgets", _budgets(), produced_at_ms=NOW - 20 * MINUTE, ttl_ms=10 * MINUTE
            ),
        }
    )
    rendered = render_judge_view(state, now_ms=NOW)
    assert "budgets" in rendered.dropped
    assert "budgets" not in rendered.sections
    assert "budgets:" not in rendered.text
    assert "presence" in rendered.sections
    assert rendered.fallback is None


def test_render_budget_drops_lowest_priority_whole() -> None:
    state = _full_state()
    full = render_judge_view(state, now_ms=NOW, max_chars=100_000)
    rendered = render_judge_view(state, now_ms=NOW, max_chars=len(full.text) - 1)
    assert "people_activity" in rendered.dropped
    assert "people_activity" not in rendered.sections
    assert "presence" in rendered.sections
    assert "budgets" in rendered.sections
    assert len(rendered.text) <= len(full.text) - 1
    assert rendered.fallback is None


def test_render_unknown_limit_says_unknown() -> None:
    state = _state({"budgets": _section("budgets", _budgets(limit=None))})
    rendered = render_judge_view(state, now_ms=NOW)
    assert "judge calls 2/unknown" in rendered.text
    assert "2/0" not in rendered.text


def test_render_has_no_message_or_opportunity_ids() -> None:
    state = _full_state()
    tainted = dict(state.sections["people_activity"].data)
    tainted["text"] = "TOPSECRET_MESSAGE_TEXT_42"
    tainted["message_id"] = "whatsapp:120000000000000042@g.us:m1"
    tainted["opportunity_id"] = "opp-synthetic-deadbeef"
    state = _state({**state.sections, "people_activity": _section("people_activity", tainted)})
    rendered = render_judge_view(state, now_ms=NOW)
    assert "whatsapp:" not in rendered.text
    assert "opp-" not in rendered.text
    assert "TOPSECRET" not in rendered.text
    assert "message_id" not in rendered.text
    assert "opportunity_id" not in rendered.text


def test_render_omits_unavailable_sections() -> None:
    rendered = render_judge_view(_full_state(), now_ms=NOW, unavailable=frozenset({"commitments"}))
    assert "commitments" not in rendered.sections
    assert "commitments:" not in rendered.text
    assert "presence" in rendered.sections
    assert "presence:" in rendered.text


def test_render_counts_conversation_participants() -> None:
    rendered = render_judge_view(_full_state(), now_ms=NOW)
    line = next(item for item in rendered.text.splitlines() if item.startswith("conversations:"))
    assert "2 in window" in line
    assert "3 people" in line
    assert "Arvid involved" in line
