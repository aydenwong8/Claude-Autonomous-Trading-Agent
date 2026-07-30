"""Timestamps for an agent that trades a New York-hours market.

One rule, applied everywhere: **store UTC, display Eastern.**

Every timestamp written to SQLite is a timezone-aware UTC ISO string. That
keeps rows lexicographically sortable (`ORDER BY timestamp`) and unambiguous
across DST — both of which break if rows carry mixed offsets. Nothing a human
reads should be UTC, though: "20:30" for a post-close run is a puzzle, "4:30 PM
ET" is the answer. So every display boundary — console, dashboard, report,
and the prompt Claude itself reads — converts through here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# "2026-07-29 4:30 PM ET" — no leading zero on the hour; %-I is fine on
# macOS/Linux, which is everywhere this runs.
_ET_FORMAT = "%Y-%m-%d %-I:%M %p ET"


def now_utc() -> datetime:
    """Current instant, UTC-aware. The one clock the agent writes to storage."""
    return datetime.now(timezone.utc)


def now_eastern() -> datetime:
    """Current instant on market time."""
    return datetime.now(EASTERN)


def to_eastern(value: str | datetime) -> datetime | None:
    """Convert a stored timestamp to Eastern; None if it can't be parsed.

    Naive values are read as UTC — that is what older rows are, written before
    the agent was consistent about attaching a timezone.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EASTERN)


def fmt_et(value: str | datetime) -> str:
    """Stored timestamp -> '2026-07-29 4:30 PM ET'. Unparseable input passes through."""
    eastern = to_eastern(value)
    if eastern is None:
        return str(value)
    return eastern.strftime(_ET_FORMAT)


def fmt_et_date(value: str | datetime) -> str:
    """Stored timestamp -> '2026-07-29', bucketed on the *market's* calendar day.

    A 4:00 PM ET close is 20:00 UTC the same day, but anything after 8:00 PM ET
    rolls over in UTC and would land on tomorrow's date. Daily series must
    bucket on this, not on the UTC date.
    """
    eastern = to_eastern(value)
    if eastern is None:
        return str(value)
    return eastern.date().isoformat()
