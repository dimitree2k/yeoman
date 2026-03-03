"""CalDAV data types."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CalendarInfo:
    """Summary of a calendar."""
    name: str
    calendar_id: str
    color: str | None = None
    description: str | None = None


@dataclass
class RecurrenceRule:
    """Recurrence rule for an event."""
    freq: str  # DAILY, WEEKLY, MONTHLY, YEARLY
    interval: int = 1
    until: datetime | None = None
    count: int | None = None
    by_day: list[str] = field(default_factory=list)  # MO, TU, WE, ...


@dataclass
class Reminder:
    """An alarm/reminder for an event."""
    minutes_before: int


@dataclass
class EventInfo:
    """Summary of a calendar event."""
    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None
    all_day: bool = False
    recurrence: RecurrenceRule | None = None
    reminders: list[Reminder] = field(default_factory=list)
    calendar_name: str = ""
