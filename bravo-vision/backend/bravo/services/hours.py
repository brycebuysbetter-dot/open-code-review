from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bravo.config import get_settings

# Dominant zone per state (split-zone states use the zone covering most residents).
STATE_TZ = {
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/New_York", "NH": "America/New_York", "NJ": "America/New_York", "NY": "America/New_York",
    "NC": "America/New_York", "OH": "America/New_York", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "VT": "America/New_York", "VA": "America/New_York", "WV": "America/New_York",
    "IN": "America/New_York", "KY": "America/New_York",
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago", "IA": "America/Chicago",
    "KS": "America/Chicago", "LA": "America/Chicago", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "NE": "America/Chicago", "ND": "America/Chicago", "OK": "America/Chicago",
    "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago", "WI": "America/Chicago",
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Denver", "MT": "America/Denver",
    "NM": "America/Denver", "UT": "America/Denver", "WY": "America/Denver",
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles", "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}


def contact_tz(state: str | None) -> ZoneInfo:
    return ZoneInfo(STATE_TZ.get((state or "").upper(), get_settings().business_tz))


def is_contact_allowed(now: datetime, state: str | None = None) -> bool:
    s = get_settings()
    local = now.astimezone(contact_tz(state))
    return local.weekday() in s.contact_weekdays and s.contact_start_hour <= local.hour < s.contact_end_hour


def next_contact_time(now: datetime, state: str | None = None) -> datetime:
    """Earliest moment at or after `now` that falls inside the contact window (recipient-local)."""
    s = get_settings()
    tz = contact_tz(state)
    local = now.astimezone(tz)
    for _ in range(9):
        if local.weekday() in s.contact_weekdays:
            start = local.replace(hour=s.contact_start_hour, minute=0, second=0, microsecond=0)
            end = local.replace(hour=s.contact_end_hour % 24, minute=0, second=0, microsecond=0)
            if s.contact_end_hour == 24:
                end = start.replace(hour=0) + timedelta(days=1)
            if local < start:
                return start.astimezone(now.tzinfo)
            if local < end:
                return local.astimezone(now.tzinfo)
        local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise RuntimeError("no contact window configured")
