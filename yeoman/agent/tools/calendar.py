"""Calendar tool for managing Apple Calendar events via CalDAV."""

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from yeoman.agent.tools.base import Tool
from yeoman.caldav.service import CalDAVService
from yeoman.caldav.types import EventInfo, RecurrenceRule, Reminder


class CalendarTool(Tool):
    """Tool to manage Apple Calendar events."""

    def __init__(self, caldav_service: CalDAVService):
        self._service = caldav_service

    @property
    def name(self) -> str:
        return "calendar"

    @property
    def description(self) -> str:
        return (
            "Manage Apple Calendar events. Actions: list_calendars, list_events, "
            "create_event, update_event, delete_event, search_events."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_calendars",
                        "list_events",
                        "create_event",
                        "update_event",
                        "delete_event",
                        "search_events",
                    ],
                    "description": "Action to perform",
                },
                "calendar": {
                    "type": "string",
                    "description": "Calendar display name",
                },
                "event_id": {
                    "type": "string",
                    "description": "Event UID (for update/delete)",
                },
                "summary": {
                    "type": "string",
                    "description": "Event title/summary",
                },
                "start": {
                    "type": "string",
                    "description": "Start date/time in ISO 8601 format",
                },
                "end": {
                    "type": "string",
                    "description": "End date/time in ISO 8601 format",
                },
                "location": {
                    "type": "string",
                    "description": "Event location",
                },
                "description": {
                    "type": "string",
                    "description": "Event description/notes",
                },
                "all_day": {
                    "type": "boolean",
                    "description": "Whether the event is all-day",
                },
                "recurrence": {
                    "type": "object",
                    "description": "Recurrence rule: {freq, interval, until, count, by_day}",
                    "properties": {
                        "freq": {
                            "type": "string",
                            "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"],
                        },
                        "interval": {"type": "integer"},
                        "until": {"type": "string"},
                        "count": {"type": "integer"},
                        "by_day": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "reminders": {
                    "type": "array",
                    "description": "List of reminders: [{minutes_before: N}, ...]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "minutes_before": {"type": "integer"},
                        },
                    },
                },
                "apply_to": {
                    "type": "string",
                    "enum": ["this", "future", "all"],
                    "description": "For recurring events: which occurrences to affect",
                },
                "query": {
                    "type": "string",
                    "description": "Search query text (for search_events)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str = "",
        calendar: str = "",
        event_id: str = "",
        summary: str = "",
        start: str = "",
        end: str = "",
        location: str = "",
        description: str = "",
        all_day: bool = False,
        recurrence: dict[str, Any] | None = None,
        reminders: list[dict[str, Any]] | None = None,
        apply_to: str = "this",
        query: str = "",
        **kwargs: Any,
    ) -> str:
        match action:
            case "list_calendars":
                return await self._list_calendars()
            case "list_events":
                return await self._list_events(calendar, start, end)
            case "create_event":
                return await self._create_event(
                    calendar=calendar,
                    summary=summary,
                    start_str=start,
                    end_str=end,
                    location=location,
                    description=description,
                    all_day=all_day,
                    recurrence_dict=recurrence,
                    reminders_list=reminders,
                )
            case "update_event":
                return await self._update_event(
                    calendar=calendar,
                    event_id=event_id,
                    summary=summary,
                    start_str=start,
                    end_str=end,
                    location=location,
                    description=description,
                    all_day=all_day,
                    apply_to=apply_to,
                )
            case "delete_event":
                return await self._delete_event(
                    calendar=calendar,
                    event_id=event_id,
                    apply_to=apply_to,
                )
            case "search_events":
                return await self._search_events(
                    query=query,
                    calendar=calendar,
                    start_str=start,
                    end_str=end,
                )
            case _:
                return f"Error: Unknown action '{action}'"

    # ------------------------------------------------------------------
    # Date parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(s: str) -> datetime:
        """Parse an ISO 8601 date or datetime string.

        Tries multiple formats and defaults to UTC when no timezone is present.
        Raises ValueError on parse failure.
        """
        value = s.strip()
        if not value:
            raise ValueError("Empty date string")

        # Handle trailing Z (UTC shorthand)
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        # Try full datetime with timezone via fromisoformat
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

        # Try date-only formats
        for fmt in ("%Y-%m-%d",):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        raise ValueError(f"Cannot parse date/time: '{s}'")

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _list_calendars(self) -> str:
        try:
            calendars = await self._service.list_calendars()
        except Exception as e:
            logger.error("Failed to list calendars: {}", e)
            return f"Error: Failed to list calendars ({e})"

        if not calendars:
            return "No calendars found."

        lines = []
        for cal in calendars:
            parts = [f"- {cal.name}"]
            if cal.color:
                parts.append(f"  (color: {cal.color})")
            lines.append("".join(parts))
        return "Calendars:\n" + "\n".join(lines)

    async def _list_events(
        self,
        calendar: str,
        start_str: str,
        end_str: str,
    ) -> str:
        if not calendar:
            return "Error: 'calendar' is required for list_events"

        try:
            now = datetime.now(tz=timezone.utc)
            start = self._parse_dt(start_str) if start_str else now.replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            end = self._parse_dt(end_str) if end_str else start + timedelta(days=7)
        except ValueError as e:
            return f"Error: Invalid date — {e}"

        try:
            events = await self._service.list_events(calendar, start, end)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Failed to list events: {}", e)
            return f"Error: Failed to list events ({e})"

        if not events:
            return f"No events in '{calendar}' for the requested range."

        return self._format_events(events)

    async def _create_event(
        self,
        *,
        calendar: str,
        summary: str,
        start_str: str,
        end_str: str,
        location: str,
        description: str,
        all_day: bool,
        recurrence_dict: dict[str, Any] | None,
        reminders_list: list[dict[str, Any]] | None,
    ) -> str:
        if not calendar:
            return "Error: 'calendar' is required for create_event"
        if not summary:
            return "Error: 'summary' is required for create_event"
        if not start_str:
            return "Error: 'start' is required for create_event"

        try:
            start = self._parse_dt(start_str)
        except ValueError as e:
            return f"Error: Invalid start date — {e}"

        try:
            end = self._parse_dt(end_str) if end_str else start + timedelta(hours=1)
        except ValueError as e:
            return f"Error: Invalid end date — {e}"

        # Convert recurrence dict to RecurrenceRule
        recurrence: RecurrenceRule | None = None
        if recurrence_dict and isinstance(recurrence_dict, dict):
            freq = recurrence_dict.get("freq", "")
            if not freq:
                return "Error: recurrence.freq is required"
            until: datetime | None = None
            until_str = recurrence_dict.get("until")
            if until_str:
                try:
                    until = self._parse_dt(str(until_str))
                except ValueError as e:
                    return f"Error: Invalid recurrence.until — {e}"
            recurrence = RecurrenceRule(
                freq=str(freq).upper(),
                interval=int(recurrence_dict.get("interval", 1)),
                until=until,
                count=int(recurrence_dict["count"]) if recurrence_dict.get("count") else None,
                by_day=list(recurrence_dict.get("by_day", [])),
            )

        # Convert reminders list to Reminder objects
        reminders: list[Reminder] | None = None
        if reminders_list and isinstance(reminders_list, list):
            reminders = []
            for item in reminders_list:
                if isinstance(item, dict) and "minutes_before" in item:
                    reminders.append(Reminder(minutes_before=int(item["minutes_before"])))

        try:
            event = await self._service.create_event(
                calendar=calendar,
                summary=summary,
                start=start,
                end=end,
                location=location or None,
                description=description or None,
                all_day=all_day,
                recurrence=recurrence,
                reminders=reminders,
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Failed to create event: {}", e)
            return f"Error: Failed to create event ({e})"

        return f"Event created:\n{self._format_event(event)}"

    async def _update_event(
        self,
        *,
        calendar: str,
        event_id: str,
        summary: str,
        start_str: str,
        end_str: str,
        location: str,
        description: str,
        all_day: bool,
        apply_to: str,
    ) -> str:
        if not calendar:
            return "Error: 'calendar' is required for update_event"
        if not event_id:
            return "Error: 'event_id' is required for update_event"

        changes: dict[str, object] = {}
        if summary:
            changes["summary"] = summary
        if start_str:
            try:
                changes["start"] = self._parse_dt(start_str)
            except ValueError as e:
                return f"Error: Invalid start date — {e}"
        if end_str:
            try:
                changes["end"] = self._parse_dt(end_str)
            except ValueError as e:
                return f"Error: Invalid end date — {e}"
        if location:
            changes["location"] = location
        if description:
            changes["description"] = description

        if not changes:
            return "Error: No changes specified for update_event"

        try:
            event = await self._service.update_event(
                calendar=calendar,
                event_id=event_id,
                apply_to=apply_to,
                **changes,
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Failed to update event: {}", e)
            return f"Error: Failed to update event ({e})"

        return f"Event updated:\n{self._format_event(event)}"

    async def _delete_event(
        self,
        *,
        calendar: str,
        event_id: str,
        apply_to: str,
    ) -> str:
        if not calendar:
            return "Error: 'calendar' is required for delete_event"
        if not event_id:
            return "Error: 'event_id' is required for delete_event"

        try:
            await self._service.delete_event(
                calendar=calendar,
                event_id=event_id,
                apply_to=apply_to,
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Failed to delete event: {}", e)
            return f"Error: Failed to delete event ({e})"

        return f"Event '{event_id}' deleted from '{calendar}'."

    async def _search_events(
        self,
        *,
        query: str,
        calendar: str,
        start_str: str,
        end_str: str,
    ) -> str:
        if not query:
            return "Error: 'query' is required for search_events"

        start: datetime | None = None
        end: datetime | None = None
        if start_str:
            try:
                start = self._parse_dt(start_str)
            except ValueError as e:
                return f"Error: Invalid start date — {e}"
        if end_str:
            try:
                end = self._parse_dt(end_str)
            except ValueError as e:
                return f"Error: Invalid end date — {e}"

        try:
            events = await self._service.search_events(
                query=query,
                calendar=calendar or None,
                start=start,
                end=end,
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Failed to search events: {}", e)
            return f"Error: Failed to search events ({e})"

        if not events:
            return f"No events matching '{query}'."

        return f"Search results for '{query}':\n{self._format_events(events)}"

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_events(events: list[EventInfo]) -> str:
        """Group events by date and format as a readable list."""
        grouped: dict[str, list[EventInfo]] = {}
        for ev in events:
            date_key = ev.start.strftime("%Y-%m-%d (%A)")
            grouped.setdefault(date_key, []).append(ev)

        lines: list[str] = []
        for date_label, day_events in grouped.items():
            lines.append(f"\n{date_label}:")
            for ev in day_events:
                lines.append(CalendarTool._format_event_line(ev))
        return "\n".join(lines).strip()

    @staticmethod
    def _format_event(ev: EventInfo) -> str:
        """Full detail format for a single event."""
        parts = [f"  Summary: {ev.summary}"]
        if ev.all_day:
            parts.append(f"  Date: {ev.start.strftime('%Y-%m-%d')} (all day)")
        else:
            parts.append(f"  Start: {ev.start.strftime('%Y-%m-%d %H:%M %Z')}")
            parts.append(f"  End: {ev.end.strftime('%Y-%m-%d %H:%M %Z')}")
        if ev.location:
            parts.append(f"  Location: {ev.location}")
        if ev.description:
            parts.append(f"  Description: {ev.description}")
        if ev.recurrence:
            r = ev.recurrence
            rule_parts = [f"every {r.interval} {r.freq.lower()}"]
            if r.by_day:
                rule_parts.append(f"on {', '.join(r.by_day)}")
            if r.until:
                rule_parts.append(f"until {r.until.strftime('%Y-%m-%d')}")
            if r.count:
                rule_parts.append(f"{r.count} times")
            parts.append(f"  Recurrence: {' '.join(rule_parts)}")
        if ev.reminders:
            mins = [str(r.minutes_before) for r in ev.reminders]
            parts.append(f"  Reminders: {', '.join(mins)} min before")
        parts.append(f"  Calendar: {ev.calendar_name}")
        parts.append(f"  ID: {ev.uid}")
        return "\n".join(parts)

    @staticmethod
    def _format_event_line(ev: EventInfo) -> str:
        """Compact one-line format for event listings."""
        if ev.all_day:
            time_str = "all day"
        else:
            time_str = f"{ev.start.strftime('%H:%M')}-{ev.end.strftime('%H:%M')}"
        parts = [f"  {time_str}  {ev.summary}"]
        if ev.location:
            parts.append(f" @ {ev.location}")
        parts.append(f"  [ID: {ev.uid}]")
        return "".join(parts)
