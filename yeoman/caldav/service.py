"""CalDAV service for Apple Calendar integration."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import caldav
import caldav.lib.error
import icalendar
from loguru import logger

from yeoman.caldav.types import CalendarInfo, EventInfo, RecurrenceRule, Reminder


class CalDAVService:
    """Service for interacting with CalDAV calendars (e.g. Apple iCloud)."""

    def __init__(
        self,
        username: str,
        app_password: str,
        url: str = "https://caldav.icloud.com",
    ):
        self._username = username
        self._app_password = app_password
        self._url = url
        self._client: caldav.DAVClient | None = None

    def _connect(self) -> caldav.DAVClient:
        """Lazily connect to the CalDAV server."""
        if self._client is None:
            self._client = caldav.DAVClient(
                url=self._url,
                username=self._username,
                password=self._app_password,
            )
        return self._client

    def _reconnect(self) -> caldav.DAVClient:
        """Force a new connection (e.g. after session expiry)."""
        self._client = None
        return self._connect()

    def _with_retry(self, fn):
        """Run fn(), retrying once with a fresh connection on auth/connection errors."""
        try:
            return fn()
        except Exception as exc:
            err_str = str(exc).lower()
            if any(k in err_str for k in ("401", "403", "unauthorized", "connection", "timeout")):
                logger.warning("CalDAV connection error, reconnecting: {}", exc)
                self._reconnect()
                return fn()
            raise

    def _get_calendar(self, name: str) -> caldav.Calendar:
        """Find a calendar by display name.

        Raises:
            ValueError: If no calendar with the given name is found.
        """
        client = self._connect()
        principal = client.principal()
        calendars = principal.calendars()
        for cal in calendars:
            if cal.name == name:
                return cal
        available = [c.name for c in calendars]
        raise ValueError(f"Calendar '{name}' not found. Available: {available}")

    @staticmethod
    def _parse_event(component: icalendar.cal.Component, calendar_name: str = "") -> EventInfo:
        """Convert an icalendar VEVENT component to an EventInfo dataclass."""
        uid = str(component.get("UID", ""))
        summary = str(component.get("SUMMARY", ""))

        # Handle start time — date vs datetime for all-day events
        dt_start = component.get("DTSTART")
        dt_end = component.get("DTEND")

        start_val = dt_start.dt if dt_start else datetime.now(tz=timezone.utc)
        end_val = dt_end.dt if dt_end else None

        all_day = False
        if isinstance(start_val, date) and not isinstance(start_val, datetime):
            all_day = True
            start = datetime(start_val.year, start_val.month, start_val.day, tzinfo=timezone.utc)
            if end_val is None:
                end_val = start_val + timedelta(days=1)
            if isinstance(end_val, date) and not isinstance(end_val, datetime):
                end = datetime(end_val.year, end_val.month, end_val.day, tzinfo=timezone.utc)
            else:
                end = end_val if end_val.tzinfo else end_val.replace(tzinfo=timezone.utc)
        else:
            if isinstance(start_val, datetime):
                start = start_val if start_val.tzinfo else start_val.replace(tzinfo=timezone.utc)
            else:
                start = datetime.now(tz=timezone.utc)

            if end_val is None:
                end = start + timedelta(hours=1)
            elif isinstance(end_val, date) and not isinstance(end_val, datetime):
                end = datetime(end_val.year, end_val.month, end_val.day, tzinfo=timezone.utc)
            elif isinstance(end_val, datetime):
                end = end_val if end_val.tzinfo else end_val.replace(tzinfo=timezone.utc)
            else:
                end = start + timedelta(hours=1)

        location = str(component.get("LOCATION", "")) or None
        description = str(component.get("DESCRIPTION", "")) or None

        # Parse recurrence rule
        recurrence: RecurrenceRule | None = None
        rrule = component.get("RRULE")
        if rrule:
            freq = str(rrule.get("FREQ", [""])[0]) if isinstance(rrule.get("FREQ"), list) else str(rrule.get("FREQ", ""))
            interval = 1
            interval_val = rrule.get("INTERVAL")
            if interval_val:
                interval = int(interval_val[0]) if isinstance(interval_val, list) else int(interval_val)

            until: datetime | None = None
            until_val = rrule.get("UNTIL")
            if until_val:
                raw_until = until_val[0] if isinstance(until_val, list) else until_val
                if isinstance(raw_until, datetime):
                    until = raw_until if raw_until.tzinfo else raw_until.replace(tzinfo=timezone.utc)
                elif isinstance(raw_until, date):
                    until = datetime(raw_until.year, raw_until.month, raw_until.day, tzinfo=timezone.utc)

            count: int | None = None
            count_val = rrule.get("COUNT")
            if count_val:
                count = int(count_val[0]) if isinstance(count_val, list) else int(count_val)

            by_day: list[str] = []
            byday_val = rrule.get("BYDAY")
            if byday_val:
                if isinstance(byday_val, list):
                    by_day = [str(d) for d in byday_val]
                else:
                    by_day = [str(byday_val)]

            recurrence = RecurrenceRule(
                freq=freq,
                interval=interval,
                until=until,
                count=count,
                by_day=by_day,
            )

        # Parse VALARM subcomponents into reminders
        reminders: list[Reminder] = []
        for subcomp in component.subcomponents:
            if subcomp.name == "VALARM":
                trigger = subcomp.get("TRIGGER")
                if trigger and hasattr(trigger, "dt"):
                    td = trigger.dt
                    if isinstance(td, timedelta):
                        minutes = int(abs(td.total_seconds()) / 60)
                        reminders.append(Reminder(minutes_before=minutes))

        return EventInfo(
            uid=uid,
            summary=summary,
            start=start,
            end=end,
            location=location,
            description=description,
            all_day=all_day,
            recurrence=recurrence,
            reminders=reminders,
            calendar_name=calendar_name,
        )

    async def list_calendars(self) -> list[CalendarInfo]:
        """List all calendars accessible by the user."""
        def _sync() -> list[CalendarInfo]:
            def _do():
                client = self._connect()
                principal = client.principal()
                calendars = principal.calendars()
                result: list[CalendarInfo] = []
                for cal in calendars:
                    cal_id = str(cal.url) if cal.url else ""
                    color: str | None = None
                    try:
                        color_prop = cal.get_property(caldav.elements.ical.CalendarColor())
                        if color_prop:
                            color = str(color_prop)
                    except Exception:
                        pass
                    result.append(CalendarInfo(
                        name=cal.name or "",
                        calendar_id=cal_id,
                        color=color,
                        description=None,
                    ))
                return result
            return self._with_retry(_do)

        return await asyncio.to_thread(_sync)

    async def list_events(
        self,
        calendar: str,
        start: datetime,
        end: datetime,
    ) -> list[EventInfo]:
        """List events in a calendar within a date range."""
        def _sync() -> list[EventInfo]:
            def _do():
                cal = self._get_calendar(calendar)
                events = cal.search(
                    start=start,
                    end=end,
                    event=True,
                    expand=True,
                )
                result: list[EventInfo] = []
                for event in events:
                    try:
                        ical = event.icalendar_instance
                        for component in ical.walk():
                            if component.name == "VEVENT":
                                result.append(self._parse_event(component, calendar_name=calendar))
                    except Exception as e:
                        logger.warning(f"Failed to parse event: {e}")
                result.sort(key=lambda e: e.start)
                return result
            return self._with_retry(_do)

        return await asyncio.to_thread(_sync)

    async def create_event(
        self,
        calendar: str,
        summary: str,
        start: datetime,
        end: datetime,
        location: str | None = None,
        description: str | None = None,
        all_day: bool = False,
        recurrence: RecurrenceRule | None = None,
        reminders: list[Reminder] | None = None,
    ) -> EventInfo:
        """Create a new event in a calendar."""
        def _sync() -> EventInfo:
            def _do():
                cal = self._get_calendar(calendar)

                if all_day:
                    dtstart = start.date() if isinstance(start, datetime) else start
                    dtend = end.date() if isinstance(end, datetime) else end
                else:
                    dtstart = start
                    dtend = end

                kwargs: dict = {
                    "summary": summary,
                    "dtstart": dtstart,
                    "dtend": dtend,
                }
                if location:
                    kwargs["location"] = location
                if description:
                    kwargs["description"] = description

                # Handle recurrence rule
                if recurrence:
                    rrule_dict: dict = {"FREQ": recurrence.freq}
                    if recurrence.interval != 1:
                        rrule_dict["INTERVAL"] = recurrence.interval
                    if recurrence.until:
                        rrule_dict["UNTIL"] = recurrence.until
                    if recurrence.count:
                        rrule_dict["COUNT"] = recurrence.count
                    if recurrence.by_day:
                        rrule_dict["BYDAY"] = recurrence.by_day
                    kwargs["rrule"] = rrule_dict

                # Handle reminders — first one via alarm_trigger/alarm_action kwargs
                if reminders and len(reminders) > 0:
                    kwargs["alarm_trigger"] = timedelta(minutes=-reminders[0].minutes_before)
                    kwargs["alarm_action"] = "DISPLAY"

                event = cal.add_event(**kwargs)

                # Handle additional reminders (beyond the first) by editing the ical
                if reminders and len(reminders) > 1:
                    with event.edit_icalendar_instance() as ical_obj:
                        for component in ical_obj.walk():
                            if component.name == "VEVENT":
                                for reminder in reminders[1:]:
                                    alarm = icalendar.Alarm()
                                    alarm.add("ACTION", "DISPLAY")
                                    alarm.add("TRIGGER", timedelta(minutes=-reminder.minutes_before))
                                    alarm.add("DESCRIPTION", "Reminder")
                                    component.add_component(alarm)
                                break
                    event.save()

                # Parse back the created event
                ical = event.icalendar_instance
                for component in ical.walk():
                    if component.name == "VEVENT":
                        return self._parse_event(component, calendar_name=calendar)

                raise RuntimeError("Created event but failed to parse it back")
            return self._with_retry(_do)

        return await asyncio.to_thread(_sync)

    async def update_event(
        self,
        calendar: str,
        event_id: str,
        apply_to: str = "this",
        **changes: object,
    ) -> EventInfo:
        """Update an existing event.

        Args:
            calendar: Calendar display name.
            event_id: The UID of the event to update.
            apply_to: For recurring events: "this", "all", or "future".
            **changes: Fields to update (summary, start, end, location, description, etc.).
        """
        def _sync() -> EventInfo:
            def _do():
                cal = self._get_calendar(calendar)
                try:
                    target = cal.event_by_uid(event_id)
                except caldav.lib.error.NotFoundError:
                    raise ValueError(f"Event '{event_id}' not found in calendar '{calendar}'")

                # Map field names to iCal property names
                field_map = {
                    "summary": "SUMMARY",
                    "start": "DTSTART",
                    "end": "DTEND",
                    "location": "LOCATION",
                    "description": "DESCRIPTION",
                }

                with target.edit_icalendar_instance() as ical_obj:
                    for component in ical_obj.walk():
                        if component.name == "VEVENT":
                            for field_name, value in changes.items():
                                ical_prop = field_map.get(field_name)
                                if ical_prop and value is not None:
                                    # Remove existing then add new
                                    component.pop(ical_prop, None)
                                    component.add(ical_prop, value)
                            break

                target.save()

                # Parse back the updated event
                ical = target.icalendar_instance
                for component in ical.walk():
                    if component.name == "VEVENT":
                        return self._parse_event(component, calendar_name=calendar)

                raise RuntimeError("Updated event but failed to parse it back")
            return self._with_retry(_do)

        return await asyncio.to_thread(_sync)

    async def delete_event(
        self,
        calendar: str,
        event_id: str,
        apply_to: str = "all",
    ) -> None:
        """Delete an event from a calendar.

        Args:
            calendar: Calendar display name.
            event_id: The UID of the event to delete.
            apply_to: For recurring events: "this", "all", or "future".
        """
        def _sync() -> None:
            def _do():
                cal = self._get_calendar(calendar)
                try:
                    event = cal.event_by_uid(event_id)
                except caldav.lib.error.NotFoundError:
                    raise ValueError(f"Event '{event_id}' not found in calendar '{calendar}'")
                event.delete()
            return self._with_retry(_do)

        await asyncio.to_thread(_sync)

    async def search_events(
        self,
        query: str,
        calendar: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[EventInfo]:
        """Search for events by text across one or all calendars.

        Performs client-side case-insensitive text matching on
        SUMMARY, DESCRIPTION, and LOCATION fields.

        Args:
            query: Text to search for.
            calendar: Optional calendar name to restrict search. If None, searches all.
            start: Start of date range (default: 30 days ago).
            end: End of date range (default: 365 days from now).
        """
        def _sync() -> list[EventInfo]:
            def _do():
                now = datetime.now(tz=timezone.utc)
                search_start = start or (now - timedelta(days=30))
                search_end = end or (now + timedelta(days=365))
                query_lower = query.lower()

                if calendar:
                    calendars = [self._get_calendar(calendar)]
                else:
                    client = self._connect()
                    principal = client.principal()
                    calendars = principal.calendars()

                result: list[EventInfo] = []
                for cal in calendars:
                    cal_name = cal.name or ""
                    try:
                        events = cal.search(
                            start=search_start,
                            end=search_end,
                            event=True,
                            expand=True,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to search calendar '{cal_name}': {e}")
                        continue

                    for event in events:
                        try:
                            ical = event.icalendar_instance
                            for component in ical.walk():
                                if component.name == "VEVENT":
                                    summary = str(component.get("SUMMARY", "")).lower()
                                    desc = str(component.get("DESCRIPTION", "")).lower()
                                    loc = str(component.get("LOCATION", "")).lower()

                                    if query_lower in summary or query_lower in desc or query_lower in loc:
                                        result.append(
                                            self._parse_event(component, calendar_name=cal_name)
                                        )
                        except Exception as e:
                            logger.warning(f"Failed to parse event during search: {e}")

                result.sort(key=lambda e: e.start)
                return result
            return self._with_retry(_do)

        return await asyncio.to_thread(_sync)
