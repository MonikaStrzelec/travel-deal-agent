"""Pure configurable local-time window logic, injected into the scheduler.

Kept separate from scheduler.py (which owns due times and sleep) and from
config.py (which owns startup validation) so both can depend on this without
a circular import.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config_types import ActiveHoursConfig


def parse_time_of_day(value: str) -> time:
    """Parse a strict "HH:MM" 24-hour local time."""
    hour_text, separator, minute_text = value.partition(":")
    if not separator:
        raise ValueError(f"Invalid HH:MM time: {value!r}")
    try:
        return time(int(hour_text), int(minute_text))
    except ValueError as exc:
        raise ValueError(f"Invalid HH:MM time: {value!r}") from exc


def validate_active_hours(active_hours: ActiveHoursConfig) -> None:
    """Validate the timezone name and time-of-day formats at startup."""
    try:
        ZoneInfo(active_hours["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {active_hours['timezone']!r}") from exc
    parse_time_of_day(active_hours["active_from"])
    parse_time_of_day(active_hours["active_until"])


def is_within_active_hours(active_hours: ActiveHoursConfig, moment: datetime) -> bool:
    """True when `moment` (any tzinfo, e.g. UTC from an epoch clock) falls in the
    configured local window. See `ActiveHoursConfig` for the boundary and
    wraparound rules; `enabled: false` always returns True.
    """
    if not active_hours["enabled"]:
        return True
    local_time = moment.astimezone(ZoneInfo(active_hours["timezone"])).time()
    start = parse_time_of_day(active_hours["active_from"])
    end = parse_time_of_day(active_hours["active_until"])
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end
