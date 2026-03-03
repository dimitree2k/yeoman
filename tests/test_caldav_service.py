"""Tests for CalDAV service parsing and calendar listing."""

from datetime import datetime, timezone
from unittest.mock import PropertyMock

import icalendar

from yeoman.caldav.service import CalDAVService
from yeoman.caldav.types import CalendarInfo


SAMPLE_ICAL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:test-uid-123
DTSTART:20260315T100000Z
DTEND:20260315T110000Z
SUMMARY:Dentist Appointment
LOCATION:Dr. Smith, 123 Main St
DESCRIPTION:Annual checkup
END:VEVENT
END:VCALENDAR"""

SAMPLE_RECURRING_ICAL = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:test-uid-456
DTSTART:20260316T090000Z
DTEND:20260316T100000Z
SUMMARY:Team Standup
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Standup reminder
END:VALARM
END:VEVENT
END:VCALENDAR"""


def _get_vevent(ical_str: str) -> icalendar.cal.Component:
    """Parse iCal string and return the first VEVENT component."""
    cal = icalendar.Calendar.from_ical(ical_str)
    for component in cal.walk():
        if component.name == "VEVENT":
            return component
    raise ValueError("No VEVENT found in iCal string")


def test_parse_event_basic() -> None:
    """Parse a simple VEVENT and verify all fields."""
    component = _get_vevent(SAMPLE_ICAL)
    event = CalDAVService._parse_event(component, calendar_name="Personal")

    assert event.uid == "test-uid-123"
    assert event.summary == "Dentist Appointment"
    assert event.start == datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    assert event.end == datetime(2026, 3, 15, 11, 0, 0, tzinfo=timezone.utc)
    assert event.location == "Dr. Smith, 123 Main St"
    assert event.description == "Annual checkup"
    assert event.all_day is False
    assert event.recurrence is None
    assert event.reminders == []
    assert event.calendar_name == "Personal"


def test_parse_event_recurring_with_alarm() -> None:
    """Parse a VEVENT with RRULE and VALARM, verify recurrence and reminders."""
    component = _get_vevent(SAMPLE_RECURRING_ICAL)
    event = CalDAVService._parse_event(component, calendar_name="Work")

    assert event.uid == "test-uid-456"
    assert event.summary == "Team Standup"
    assert event.start == datetime(2026, 3, 16, 9, 0, 0, tzinfo=timezone.utc)
    assert event.end == datetime(2026, 3, 16, 10, 0, 0, tzinfo=timezone.utc)
    assert event.all_day is False
    assert event.calendar_name == "Work"

    # Verify recurrence rule
    assert event.recurrence is not None
    assert event.recurrence.freq == "WEEKLY"
    assert event.recurrence.interval == 1
    assert event.recurrence.until is None
    assert event.recurrence.count is None
    assert sorted(event.recurrence.by_day) == ["FR", "MO", "WE"]

    # Verify reminder
    assert len(event.reminders) == 1
    assert event.reminders[0].minutes_before == 15


class FakeCalendar:
    """Fake calendar for testing list_calendars."""

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self.url = url

    def get_property(self, prop: object) -> str | None:
        del prop
        return None


class FakePrincipal:
    """Fake principal for testing."""

    def __init__(self, calendars: list[FakeCalendar]) -> None:
        self._calendars = calendars

    def calendars(self) -> list[FakeCalendar]:
        return self._calendars


class FakeDAVClient:
    """Fake DAV client for testing."""

    def __init__(self, principal: FakePrincipal) -> None:
        self._principal = principal

    def principal(self) -> FakePrincipal:
        return self._principal


async def test_list_calendars() -> None:
    """Mock _connect to return a fake client, verify list_calendars."""
    fake_calendars = [
        FakeCalendar("Personal", "https://caldav.icloud.com/personal/"),
        FakeCalendar("Work", "https://caldav.icloud.com/work/"),
    ]
    fake_principal = FakePrincipal(fake_calendars)
    fake_client = FakeDAVClient(fake_principal)

    service = CalDAVService(username="test", app_password="test")
    service._client = fake_client  # type: ignore[assignment]

    calendars = await service.list_calendars()

    assert len(calendars) == 2
    assert calendars[0].name == "Personal"
    assert calendars[0].calendar_id == "https://caldav.icloud.com/personal/"
    assert calendars[1].name == "Work"
    assert calendars[1].calendar_id == "https://caldav.icloud.com/work/"
