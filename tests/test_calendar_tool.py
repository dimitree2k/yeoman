"""Tests for CalendarTool."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from yeoman.agent.tools.calendar import CalendarTool
from yeoman.caldav.types import CalendarInfo, EventInfo


@pytest.fixture
def mock_service():
    return AsyncMock()


@pytest.fixture
def tool(mock_service):
    return CalendarTool(mock_service)


def test_tool_schema(tool: CalendarTool):
    """Tool has correct name, description, and valid parameters schema."""
    assert tool.name == "calendar"
    assert "calendar" in tool.description.lower()
    schema = tool.parameters
    assert schema["type"] == "object"
    assert "action" in schema["properties"]
    assert "action" in schema["required"]
    actions = schema["properties"]["action"]["enum"]
    assert "list_calendars" in actions
    assert "list_events" in actions
    assert "create_event" in actions
    assert "update_event" in actions
    assert "delete_event" in actions
    assert "search_events" in actions


async def test_list_calendars(tool: CalendarTool, mock_service):
    mock_service.list_calendars.return_value = [
        CalendarInfo(name="Family", calendar_id="cal-1"),
        CalendarInfo(name="Work", calendar_id="cal-2"),
    ]
    result = await tool.execute(action="list_calendars")
    assert "Family" in result
    assert "Work" in result
    mock_service.list_calendars.assert_awaited_once()


async def test_list_events(tool: CalendarTool, mock_service):
    mock_service.list_events.return_value = [
        EventInfo(
            uid="uid-1",
            summary="Dentist",
            start=datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc),
            end=datetime(2026, 3, 15, 11, 0, tzinfo=timezone.utc),
            location="Dr. Smith",
            calendar_name="Family",
        ),
    ]
    result = await tool.execute(
        action="list_events",
        calendar="Family",
        start="2026-03-15",
        end="2026-03-16",
    )
    assert "Dentist" in result
    assert "Dr. Smith" in result
    assert "uid-1" in result
    mock_service.list_events.assert_awaited_once()


async def test_create_event(tool: CalendarTool, mock_service):
    mock_service.create_event.return_value = EventInfo(
        uid="new-uid",
        summary="Team Lunch",
        start=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
        end=datetime(2026, 3, 20, 13, 0, tzinfo=timezone.utc),
        calendar_name="Family",
    )
    result = await tool.execute(
        action="create_event",
        calendar="Family",
        summary="Team Lunch",
        start="2026-03-20T12:00:00Z",
        end="2026-03-20T13:00:00Z",
    )
    assert "Team Lunch" in result
    assert "new-uid" in result
    mock_service.create_event.assert_awaited_once()


async def test_delete_event(tool: CalendarTool, mock_service):
    result = await tool.execute(
        action="delete_event",
        calendar="Family",
        event_id="uid-to-delete",
    )
    assert "deleted" in result.lower() or "removed" in result.lower()
    mock_service.delete_event.assert_awaited_once()


async def test_unknown_action(tool: CalendarTool):
    result = await tool.execute(action="nonexistent")
    assert "unknown" in result.lower() or "error" in result.lower()


async def test_missing_required_calendar(tool: CalendarTool):
    result = await tool.execute(action="list_events")
    assert "error" in result.lower()
    assert "calendar" in result.lower()


async def test_missing_required_summary(tool: CalendarTool):
    result = await tool.execute(action="create_event", calendar="Family", start="2026-03-20T12:00:00Z")
    assert "error" in result.lower()
    assert "summary" in result.lower()
