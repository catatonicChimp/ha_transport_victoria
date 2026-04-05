"""GTFS time utilities.

GTFS stores stop times as HH:MM:SS strings where HH can be >= 24 for trips
that run past midnight (e.g. "25:30:00" means 1:30 AM the following morning).
Standard datetime.time cannot represent these values.

We convert everything to total seconds from service-day midnight:
  "25:30:00" -> 91800   (25*3600 + 30*60)
  "00:05:00" -> 300
  "23:59:00" -> 86340

This lets the database use simple integer comparisons on an indexed column,
and handles post-midnight trips correctly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def gtfs_time_to_seconds(time_str: str) -> int:
    """Convert a GTFS HH:MM:SS time string to seconds from service-day midnight.

    Handles post-midnight values (HH >= 24) that standard time parsing rejects.

    Args:
        time_str: A GTFS time string like "08:30:00" or "25:15:00".

    Returns:
        Total seconds from service-day midnight (always >= 0).

    Raises:
        ValueError: If the string is not in HH:MM:SS format.
    """
    parts = time_str.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid GTFS time format: {time_str!r}")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_time_str(total_seconds: int) -> str:
    """Convert seconds-from-midnight back to HH:MM:SS string.

    Inverse of gtfs_time_to_seconds. The result may have HH >= 24.
    """
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def service_day_for_time(now: datetime) -> tuple[date, int]:
    """Return the GTFS service date and seconds-from-midnight for a given time.

    GTFS service days run from ~3 AM to ~3 AM the following day.  A trip at
    00:30 on a Tuesday is published under Monday's service date with a
    departure_seconds of 86400 + 1800 = 88200.

    The convention we follow: if the local time is before 03:00, the service
    date is yesterday and now_seconds = seconds_since_midnight + 86400.
    Otherwise, service date is today and now_seconds = seconds_since_midnight.

    Returns:
        (service_date, now_seconds) — the date to query and the seconds value
        to use as the lower bound in the departure query.
    """
    # Work in local (naive) time so we match what's in the GTFS data
    if now.tzinfo is not None:
        local_now = now.astimezone(tz=None).replace(tzinfo=None)
    else:
        local_now = now

    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = int((local_now - midnight).total_seconds())

    # Trips scheduled as "24:xx" / "25:xx" belong to the *previous* calendar day.
    # We treat anything before 03:00 as still being part of the previous service day.
    CUTOVER_SECONDS = 3 * 3600  # 03:00

    if seconds_since_midnight < CUTOVER_SECONDS:
        service_date = local_now.date() - timedelta(days=1)
        now_seconds = seconds_since_midnight + 86400
    else:
        service_date = local_now.date()
        now_seconds = seconds_since_midnight

    return service_date, now_seconds


def service_date_str(d: date) -> str:
    """Format a date as YYYYMMDD for GTFS calendar comparisons."""
    return d.strftime("%Y%m%d")


def day_of_week_column(d: date) -> str:
    """Return the calendar.txt column name for the given date's day of week."""
    return d.strftime("%A").lower()  # 'monday', 'tuesday', ...


def departure_seconds_to_datetime(
    service_date: date, departure_seconds: int
) -> datetime:
    """Convert a (service_date, departure_seconds) pair to an absolute datetime.

    Handles post-midnight departures where departure_seconds >= 86400.
    The result is a naive datetime in local time.
    """
    midnight = datetime(
        service_date.year, service_date.month, service_date.day, 0, 0, 0
    )
    return midnight + timedelta(seconds=departure_seconds)
